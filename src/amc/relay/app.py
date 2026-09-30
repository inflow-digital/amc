"""AMC relay: one MCP endpoint for every AI client, outbound WebSockets for every device.

Surfaces:

- ``POST /mcp``                 MCP Streamable HTTP (agent key or OAuth token)
- ``/.well-known/...``, ``/authorize``, ``/token``, ``/register``   OAuth 2.1 (when enabled)
- ``GET|POST /oauth/approve``    consent page (asks for an AMC agent key once)
- ``POST /api/v1/pair``          redeem a one-time pairing code -> device identity
- ``GET  /api/v1/devices``       devices visible to the caller
- ``POST /api/v1/execute``       governed call (same path as MCP tools)
- ``GET  /api/v1/audit``         the caller's own audit events
- ``WS   /devices/connect``      device agents (device token)
- ``GET  /livez``                liveness, unauthenticated, reveals nothing

The relay never touches files or runs commands itself; it authenticates,
authorizes, routes and audits. Devices do the work.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from mcp.server.auth.provider import AuthorizeError
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from amc import __version__
from amc.core import Principal, RelayBroker, RemoteExecutionPolicy

from .audit import AuditSink
from .devices import DeviceEndpoint, DeviceSessionRegistry
from .oauth import AmcAuthProvider
from .runtime import RelayError, RelayRuntime, validate_device_id
from .store import RelayStore, StoreError

logger = logging.getLogger("amc.relay")

MCP_PATH = "/mcp"
MAX_AUDIT_PAGE = 1000
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_SIZE_RE = re.compile(r"size:\s*(\d+)")
PAIR_FAILURES_PER_MINUTE = 20
APPROVE_ATTEMPTS = 5
OAUTH_REQUESTS_PER_MINUTE = 60
LEGACY_STATUS_COMMAND = (
    "printf '=== system ===\\n'; uname -a; printf '\\n=== uptime ===\\n'; uptime; "
    "printf '\\n=== memory ===\\n'; (free -h 2>/dev/null || vm_stat 2>/dev/null || true); "
    "printf '\\n=== disk ===\\n'; df -h /; printf '\\n=== gpu ===\\n'; "
    "(nvidia-smi --query-gpu=name,memory.total,memory.used,utilization.gpu --format=csv,noheader 2>/dev/null || true)"
)

INSTRUCTIONS = (
    "AMC lets you work on the user's registered machines (devices). Call remote_devices first "
    "to get a device_id, then use the amc_* tools with that device_id. Everything runs on the "
    "device, never on the relay; access is limited by the key the user gave you."
)


def bearer_value(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    value = value.strip()
    return value if scheme.lower() == "bearer" and value else None


def oauth_issuer(public_url: str | None, port: int) -> str | None:
    """OAuth needs an HTTPS issuer (or loopback for local clients)."""
    if public_url:
        url = public_url.rstrip("/")
        return url if url.startswith("https://") or _is_loopback_url(url) else None
    return f"http://127.0.0.1:{port}"


def _is_loopback_url(url: str) -> bool:
    return bool(re.match(r"^http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(/|$)", url))


class ExecuteBody(BaseModel):
    device_id: str = Field(min_length=1, max_length=128)
    tool: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)


class PairBody(BaseModel):
    code: str = Field(min_length=4, max_length=32)
    name: str = Field(default="", max_length=64)
    agent_version: str | None = Field(default=None, max_length=64)


class BodyLimit:
    """Abort HTTP requests whose body exceeds ``limit`` bytes (declared or streamed)."""

    def __init__(self, app: Any, *, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                if int(raw_length) > self.limit:
                    await JSONResponse({"detail": "request body too large"}, status_code=413)(
                        scope, receive, send
                    )
                    return
            except ValueError:
                await JSONResponse({"detail": "invalid Content-Length"}, status_code=400)(scope, receive, send)
                return
        seen = 0

        async def limited_receive() -> dict[str, Any]:
            nonlocal seen
            message = await receive()
            if message.get("type") == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.limit:
                    raise HTTPException(status_code=413, detail="request body too large")
            return message

        await self.app(scope, limited_receive, send)


class RelayServices:
    """Shared state of one relay process (``app.state.relay``)."""

    def __init__(self, store: RelayStore, *, audit_path: Path | None, oauth_path: Path) -> None:
        self.store = store
        state = store.state
        self.broker = RelayBroker()
        self.audit = AuditSink(log_path=audit_path)
        self.runtime = RelayRuntime(
            broker=self.broker,
            policy=self.policy,
            audit=self.audit,
            call_timeout_seconds=state.call_timeout_seconds,
            max_inflight_per_device=state.max_inflight_per_device,
            max_arguments_bytes=state.max_arguments_bytes,
        )
        self.issuer = oauth_issuer(state.public_url, state.port)
        self.auth = AmcAuthProvider(store, oauth_path, issuer_url=self.issuer or f"http://127.0.0.1:{state.port}")
        self.sessions = DeviceSessionRegistry()
        self.device_endpoint = DeviceEndpoint(
            broker=self.broker,
            audit=self.audit,
            resolve_device=self.resolve_device,
            sessions=self.sessions,
            call_timeout_seconds=state.call_timeout_seconds,
            max_result_bytes=state.max_result_bytes,
        )
        self.sweep_interval = 15.0
        self._pair_failures: dict[str, list[float]] = {}
        self._approve_failures: dict[str, int] = {}

    def policy(self) -> RemoteExecutionPolicy:
        return RemoteExecutionPolicy([client.grant() for client in self.store.state.clients])

    def resolve_device(self, header: str | None) -> str | None:
        token = bearer_value(header)
        if token is None:
            return None
        device = self.store.state.device_by_token(token)
        return device.device_id if device else None

    async def principal_for_token(self, token: str | None) -> Principal | None:
        if not token:
            return None
        access = await self.auth.load_access_token(token)
        if access is None or not access.subject:
            return None
        return Principal(principal_id=f"key:{access.subject}")

    async def device_status(self, principal: Principal, device_id: str | None = None) -> list[dict[str, Any]]:
        policy = self.policy()
        online = {session.device_id: session for session in self.sessions.snapshot()}
        rows: list[dict[str, Any]] = []
        for device in self.store.state.devices:
            if device_id is not None and device.device_id != device_id:
                continue
            if not policy.can_see_device(principal, device.device_id):
                continue
            session = online.get(device.device_id)
            rows.append(
                {
                    "device_id": device.device_id,
                    "name": device.name,
                    "connected": session is not None,
                    "connected_at": session.connected_at.isoformat() if session else None,
                    "agent_version": session.agent_version if session else None,
                    "inflight": self.runtime.inflight(device.device_id),
                }
            )
        return rows

    def pair_allowed(self, client: str) -> bool:
        """Per-address limit on failed pairing attempts (a global limit would let anyone block pairing)."""
        now = time.monotonic()
        for address in list(self._pair_failures):
            recent = [t for t in self._pair_failures[address] if now - t < 60]
            if recent:
                self._pair_failures[address] = recent
            else:
                del self._pair_failures[address]
        return len(self._pair_failures.get(client, [])) < PAIR_FAILURES_PER_MINUTE

    def pair_failed(self, client: str) -> None:
        self._pair_failures.setdefault(client, []).append(time.monotonic())

    async def close_revoked_devices(self) -> None:
        known = {device.device_id for device in self.store.state.devices}
        for session in self.sessions.snapshot():
            if session.device_id not in known:
                self.audit.record_device_event(device_id=session.device_id, decision="device_revoked")
                await session.close(4401, "device removed")


def _content_blocks(remote: dict[str, Any]) -> list[dict[str, Any]]:
    result = remote.get("result")
    content = result.get("content") if isinstance(result, dict) else None
    return [block for block in content or [] if isinstance(block, dict)]


def _reported_size(remote: dict[str, Any]) -> int | None:
    for block in _content_blocks(remote):
        match = _SIZE_RE.search(str(block.get("text", "")))
        if match:
            return int(match.group(1))
    return None


def _suffix(path: str) -> str:
    pure = PureWindowsPath(path) if "\\" in path else PurePosixPath(path)
    return pure.suffix.lower()


# Tool annotations tell clients (ChatGPT, Claude) what each tool really does, so read-only
# tools are not gated like destructive ones. They are hints only: AMC policy still decides.
_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
_OVERWRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)
_EXECUTE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)
_TERMINATE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def _build_mcp(services: RelayServices) -> FastMCP:
    state = services.store.state
    issuer = services.issuer
    common: dict[str, Any] = {
        "instructions": INSTRUCTIONS,
        "stateless_http": True,
        "json_response": True,
        "streamable_http_path": MCP_PATH,
        # Every request must authenticate, so DNS-rebinding protection (meant for
        # unauthenticated local servers) would only break access through proxies/tunnels.
        "transport_security": TransportSecuritySettings(enable_dns_rebinding_protection=False),
    }
    if issuer:
        mcp = FastMCP(
            "amc",
            auth_server_provider=services.auth,
            auth=AuthSettings(
                issuer_url=issuer,
                resource_server_url=f"{issuer}{MCP_PATH}",
                client_registration_options=ClientRegistrationOptions(enabled=True),
                revocation_options=RevocationOptions(enabled=True),
                # agent keys are not audience-bound; OAuth tokens are checked by AmcAuthProvider
                validate_token_resource=False,
            ),
            **common,
        )
    else:
        mcp = FastMCP(
            "amc",
            token_verifier=services.auth,
            auth=AuthSettings(issuer_url=f"http://127.0.0.1:{state.port}", resource_server_url=None,
                              validate_token_resource=False),
            **common,
        )

    def _principal(ctx: Context) -> Principal:
        request = ctx.request_context.request
        user = request.scope.get("user") if request is not None else None
        access = getattr(user, "access_token", None)
        subject = getattr(access, "subject", None)
        if not subject:
            raise ToolError("unauthenticated")
        return Principal(principal_id=f"key:{subject}")

    async def _invoke(ctx: Context, *, device_id: str, tool: str, arguments: dict[str, Any] | None = None) -> dict:
        principal = _principal(ctx)
        try:
            result = await services.runtime.invoke(
                principal=principal, device_id=device_id, tool=tool, arguments=arguments
            )
        except RelayError as exc:
            if exc.code == "policy_denied":
                allowed = ", ".join(sorted(services.policy().allowed_tools(principal))) or "none"
                raise ToolError(f"{exc}; tools this key may use: {allowed}") from exc
            raise ToolError(str(exc)) from exc
        return result.model_dump(mode="json")

    @mcp.tool(name="amc_read_file", annotations=_READ_ONLY)
    async def read_file(ctx: Context, device_id: str, path: str, offset: int = 0, length: int = 1000) -> dict:
        """Read a text file (offset/length in lines; negative offset reads the last lines) or an image."""
        return await _invoke(ctx, device_id=device_id, tool="read_file",
                             arguments={"path": path, "offset": offset, "length": length})

    @mcp.tool(name="amc_read_multiple_files", annotations=_READ_ONLY)
    async def read_multiple_files(ctx: Context, device_id: str, paths: list[str]) -> dict:
        """Read several files in one call."""
        return await _invoke(ctx, device_id=device_id, tool="read_multiple_files", arguments={"paths": paths})

    @mcp.tool(name="amc_list_directory", annotations=_READ_ONLY)
    async def list_directory(ctx: Context, device_id: str, path: str, depth: int = 2) -> dict:
        """List a directory ([DIR]/[FILE] entries) up to ``depth`` levels."""
        return await _invoke(ctx, device_id=device_id, tool="list_directory",
                             arguments={"path": path, "depth": depth})

    @mcp.tool(name="amc_get_file_info", annotations=_READ_ONLY)
    async def get_file_info(ctx: Context, device_id: str, path: str) -> dict:
        """File or directory metadata (size, times, type, permissions)."""
        return await _invoke(ctx, device_id=device_id, tool="get_file_info", arguments={"path": path})

    @mcp.tool(name="amc_start_search", annotations=_READ_ONLY)
    async def start_search(
        ctx: Context,
        device_id: str,
        path: str,
        pattern: str,
        search_type: str = "files",
        file_pattern: str | None = None,
        ignore_case: bool = True,
        literal_search: bool = False,
        max_results: int | None = None,
        context_lines: int = 5,
        include_hidden: bool = False,
    ) -> dict:
        """Search file names (search_type=files) or contents (search_type=content) under ``path``.

        Returns a sessionId; page with amc_get_more_search_results.
        """
        if search_type not in {"files", "content"}:
            raise ToolError("invalid_request: search_type must be files or content")
        arguments: dict[str, Any] = {
            "path": path, "pattern": pattern, "searchType": search_type, "ignoreCase": ignore_case,
            "literalSearch": literal_search, "contextLines": context_lines, "includeHidden": include_hidden,
        }
        if file_pattern is not None:
            arguments["filePattern"] = file_pattern
        if max_results is not None:
            arguments["maxResults"] = max_results
        return await _invoke(ctx, device_id=device_id, tool="start_search", arguments=arguments)

    @mcp.tool(name="amc_get_more_search_results", annotations=_READ_ONLY)
    async def get_more_search_results(ctx: Context, device_id: str, session_id: str, offset: int = 0,
                                      length: int = 100) -> dict:
        """Page results of a search started with amc_start_search."""
        return await _invoke(ctx, device_id=device_id, tool="get_more_search_results",
                             arguments={"sessionId": session_id, "offset": offset, "length": length})

    @mcp.tool(name="amc_stop_search", annotations=_READ_ONLY)
    async def stop_search(ctx: Context, device_id: str, session_id: str) -> dict:
        """Stop a running search."""
        return await _invoke(ctx, device_id=device_id, tool="stop_search", arguments={"sessionId": session_id})

    @mcp.tool(name="amc_list_searches", annotations=_READ_ONLY)
    async def list_searches(ctx: Context, device_id: str) -> dict:
        """List your active searches."""
        return await _invoke(ctx, device_id=device_id, tool="list_searches")

    max_image_bytes = max(0, state.max_result_bytes - 64 * 1024) * 3 // 4

    @mcp.tool(name="amc_view_image", annotations=_READ_ONLY, structured_output=False)
    async def view_image(ctx: Context, device_id: str, path: str) -> list[TextContent | ImageContent]:
        """Show an image file (png/jpg/jpeg/webp/gif) from a device so you can see it."""
        if _suffix(path) not in IMAGE_SUFFIXES:
            raise ToolError("invalid_request: path must be a .png/.jpg/.jpeg/.webp/.gif image")
        info = await _invoke(ctx, device_id=device_id, tool="get_file_info", arguments={"path": path})
        size = _reported_size(info)
        if size is not None and size > max_image_bytes:
            raise ToolError(f"image_too_large: {size} bytes > {max_image_bytes}; downscale it first")
        raw = await _invoke(ctx, device_id=device_id, tool="read_file", arguments={"path": path})
        if not raw.get("ok"):
            raise ToolError(f"device_error: {raw.get('error') or 'read_file failed'}")
        images = [
            ImageContent(type="image", data=block["data"], mimeType=block["mimeType"])
            for block in _content_blocks(raw)
            if block.get("type") == "image" and block.get("data") and block.get("mimeType")
        ]
        if not images:
            raise ToolError("device_error: the device returned no image content for this file")
        label = f"{path} ({images[0].mimeType}" + (f", {size} bytes)" if size is not None else ")")
        return [TextContent(type="text", text=label), *images]

    @mcp.tool(name="amc_system_status", annotations=_READ_ONLY)
    async def system_status(ctx: Context, device_id: str) -> dict:
        """OS, uptime, memory, disk and GPU snapshot of a device."""
        result = await _invoke(ctx, device_id=device_id, tool="system_status")
        if not result.get("ok") and "unknown tool" in json.dumps(result).lower():
            # Legacy (DesktopCommander-only) hosts have no built-in status; fall back to a shell probe
            # (still a governed start_process call, so it needs a key that may run commands).
            return await _invoke(ctx, device_id=device_id, tool="start_process",
                                 arguments={"command": LEGACY_STATUS_COMMAND, "timeout_ms": 10000})
        return result

    @mcp.tool(name="amc_list_processes", annotations=_READ_ONLY)
    async def list_processes(ctx: Context, device_id: str) -> dict:
        """List running processes on a device."""
        return await _invoke(ctx, device_id=device_id, tool="list_processes")

    @mcp.tool(name="amc_list_sessions", annotations=_READ_ONLY)
    async def list_sessions(ctx: Context, device_id: str) -> dict:
        """List processes you started with amc_start_process."""
        return await _invoke(ctx, device_id=device_id, tool="list_sessions")

    @mcp.tool(name="amc_read_process_output", annotations=_READ_ONLY)
    async def read_process_output(ctx: Context, device_id: str, pid: int, offset: int = 0, length: int = 1000,
                                  timeout_ms: int = 1000) -> dict:
        """Read output of a process you started with amc_start_process."""
        return await _invoke(ctx, device_id=device_id, tool="read_process_output",
                             arguments={"pid": pid, "offset": offset, "length": length, "timeout_ms": timeout_ms})

    @mcp.tool(name="amc_start_process", annotations=_EXECUTE)
    async def start_process(ctx: Context, device_id: str, command: str, timeout_ms: int = 10000,
                            shell: str | None = None) -> dict:
        """Run a shell command on a device; returns its PID and output so far."""
        arguments: dict[str, Any] = {"command": command, "timeout_ms": timeout_ms}
        if shell is not None:
            arguments["shell"] = shell
        return await _invoke(ctx, device_id=device_id, tool="start_process", arguments=arguments)

    @mcp.tool(name="amc_interact_with_process", annotations=_EXECUTE)
    async def interact_with_process(ctx: Context, device_id: str, pid: int, input: str, timeout_ms: int = 8000,
                                    wait_for_prompt: bool = True) -> dict:
        """Send a line of input to a process you started and return its response."""
        return await _invoke(ctx, device_id=device_id, tool="interact_with_process",
                             arguments={"pid": pid, "input": input, "timeout_ms": timeout_ms,
                                        "wait_for_prompt": wait_for_prompt})

    @mcp.tool(name="amc_force_terminate", annotations=_TERMINATE)
    async def force_terminate(ctx: Context, device_id: str, pid: int) -> dict:
        """Terminate a process you started with amc_start_process."""
        return await _invoke(ctx, device_id=device_id, tool="force_terminate", arguments={"pid": pid})

    @mcp.tool(name="amc_kill_process", annotations=_TERMINATE)
    async def kill_process(ctx: Context, device_id: str, pid: int) -> dict:
        """Kill any process on a device by PID."""
        return await _invoke(ctx, device_id=device_id, tool="kill_process", arguments={"pid": pid})

    @mcp.tool(name="amc_write_file", annotations=_OVERWRITE)
    async def write_file(ctx: Context, device_id: str, path: str, content: str, mode: str = "rewrite") -> dict:
        """Write (mode=rewrite) or append (mode=append) a text file."""
        if mode not in {"rewrite", "append"}:
            raise ToolError("invalid_request: mode must be rewrite or append")
        return await _invoke(ctx, device_id=device_id, tool="write_file",
                             arguments={"path": path, "content": content, "mode": mode})

    @mcp.tool(name="amc_edit_block", annotations=_WRITE)
    async def edit_block(ctx: Context, device_id: str, file_path: str, old_string: str, new_string: str,
                         expected_replacements: int = 1) -> dict:
        """Replace exact text in a file (fails without changes if the match count differs)."""
        return await _invoke(ctx, device_id=device_id, tool="edit_block",
                             arguments={"file_path": file_path, "old_string": old_string,
                                        "new_string": new_string, "expected_replacements": expected_replacements})

    @mcp.tool(name="amc_create_directory", annotations=_WRITE)
    async def create_directory(ctx: Context, device_id: str, path: str) -> dict:
        """Create a directory (and parents)."""
        return await _invoke(ctx, device_id=device_id, tool="create_directory", arguments={"path": path})

    @mcp.tool(name="amc_move_file", annotations=_WRITE)
    async def move_file(ctx: Context, device_id: str, source: str, destination: str) -> dict:
        """Move or rename a file or directory."""
        return await _invoke(ctx, device_id=device_id, tool="move_file",
                             arguments={"source": source, "destination": destination})

    @mcp.tool(annotations=_READ_ONLY)
    async def remote_devices(ctx: Context) -> list[dict[str, Any]]:
        """Devices you may use, with online status. Use their device_id with the amc_* tools."""
        return await services.device_status(_principal(ctx))

    @mcp.tool(annotations=_READ_ONLY)
    async def remote_device_status(ctx: Context, device_id: str) -> dict[str, Any]:
        """Status of one device: connected, agent_version, in-flight calls."""
        try:
            validate_device_id(device_id)
        except ValueError as exc:
            raise ToolError(f"invalid_request: {exc}") from exc
        rows = await services.device_status(_principal(ctx), device_id)
        return rows[0] if rows else {"device_id": device_id, "connected": False, "inflight": 0}

    @mcp.tool(annotations=_EXECUTE)
    async def remote_call(ctx: Context, device_id: str, tool: str, arguments: dict[str, Any] | None = None) -> dict:
        """Invoke any device tool by name (e.g. read_file, start_process) with raw arguments."""
        return await _invoke(ctx, device_id=device_id, tool=tool.removeprefix("amc_"), arguments=arguments)

    @mcp.tool(annotations=_READ_ONLY)
    async def remote_audit(ctx: Context, limit: int = 50) -> list[dict[str, Any]]:
        """Your recent audit events (tool, action, decision). Never contains arguments or results."""
        limit = max(0, min(limit, MAX_AUDIT_PAGE))
        principal = _principal(ctx)
        return [e.model_dump(mode="json") for e in services.audit.recent(limit, principal_id=principal.principal_id)]

    return mcp


_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>AMC</title>
<style>body{{font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;color:#222}}
input{{width:100%;padding:.6rem;font-size:1rem;box-sizing:border-box}}
button{{padding:.6rem 1.2rem;font-size:1rem;margin:.8rem .5rem 0 0}}
.err{{color:#b00}}code{{background:#f3f3f3;padding:.1rem .3rem}}</style></head><body>{body}</body></html>"""


KNOWN_REDIRECT_HOSTS = {
    "claude.ai": "Claude",
    "claude.com": "Claude",
    "chatgpt.com": "ChatGPT",
    "chat.openai.com": "ChatGPT",
    "localhost": "an app on this computer",
    "127.0.0.1": "an app on this computer",
}


def _redirect_label(redirect_uri: str) -> tuple[str, str | None]:
    from urllib.parse import urlsplit

    host = (urlsplit(redirect_uri).hostname or "").lower()
    for known, label in KNOWN_REDIRECT_HOSTS.items():
        if host == known or host.endswith("." + known):
            return host, label
    return host, None


def _approve_page(client_name: str, request_id: str, redirect_uri: str, error: str = "") -> HTMLResponse:
    host, known = _redirect_label(redirect_uri)
    if known:
        where = f"<p>After you approve, access is handed to <b>{html.escape(host)}</b> ({html.escape(known)}).</p>"
    else:
        where = (f'<p class="err"><b>Warning:</b> access will be handed to <b>{html.escape(host)}</b>, which is not '
                 "a known AI app. The name above is chosen by the app itself and can be fake. Only continue if "
                 "you started this connection yourself and recognise this address.</p>")
    body = f"""<h2>Allow &ldquo;{html.escape(client_name)}&rdquo; to use AMC?</h2>
{where}
<p>It will be able to use your machines with the permissions of the AMC key you enter.</p>
<p>Create a key on the relay machine with <code>amc key add NAME</code> (or use the one
<code>amc connect claude-web</code> printed).</p>
{f'<p class="err">{html.escape(error)}</p>' if error else ''}
<form method="post" action="/oauth/approve">
<input type="hidden" name="request" value="{html.escape(request_id)}">
<input type="password" name="key" placeholder="amck_..." autocomplete="off" autofocus required>
<button type="submit" name="decision" value="allow">Allow</button>
<button type="submit" name="decision" value="deny" formnovalidate>Deny</button>
</form>"""
    return HTMLResponse(_PAGE.format(body=body), status_code=400 if error else 200,
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


def create_app(store: RelayStore, *, audit_path: Path | None = None, oauth_path: Path | None = None) -> FastAPI:
    base = store.path.parent
    services = RelayServices(
        store,
        audit_path=audit_path if audit_path is not None else base / "audit.jsonl",
        oauth_path=oauth_path or base / "relay-oauth.json",
    )
    mcp = _build_mcp(services)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async def sweeper() -> None:
            while True:
                await asyncio.sleep(services.sweep_interval)
                with suppress(Exception):
                    await services.close_revoked_devices()

        task = asyncio.create_task(sweeper())
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            task.cancel()

    app = FastAPI(title="AMC relay", version=__version__, lifespan=lifespan,
                  openapi_url=None, docs_url=None, redoc_url=None)
    app.state.relay = services

    async def require_principal(authorization: str | None = Header(default=None)) -> Principal:
        principal = await services.principal_for_token(bearer_value(authorization))
        if principal is None:
            raise HTTPException(status_code=401, detail="invalid or missing bearer token",
                                headers={"WWW-Authenticate": "Bearer"})
        return principal

    client_principal = Depends(require_principal)

    @app.exception_handler(RelayError)
    async def relay_error_handler(_: Request, exc: RelayError) -> JSONResponse:
        return JSONResponse({"error": exc.code, "detail": exc.detail}, status_code=exc.http_status)

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_PAGE.format(body=f"<h2>AMC relay {__version__}</h2><p>Running. MCP endpoint: "
                                             f"<code>{MCP_PATH}</code></p>"))

    @app.get("/api/v1/devices")
    async def devices(principal: Principal = client_principal) -> list[dict[str, Any]]:
        return await services.device_status(principal)

    @app.get("/api/v1/devices/{device_id}")
    async def device(device_id: str, principal: Principal = client_principal) -> dict[str, Any]:
        rows = await services.device_status(principal, device_id)
        if not rows:
            raise HTTPException(status_code=404, detail="unknown device")
        return rows[0]

    @app.post("/api/v1/execute")
    async def execute(body: ExecuteBody, principal: Principal = client_principal) -> dict[str, Any]:
        result = await services.runtime.invoke(principal=principal, device_id=body.device_id,
                                               tool=body.tool, arguments=body.arguments)
        return result.model_dump(mode="json")

    @app.get("/api/v1/audit")
    async def audit(limit: int = Query(default=100, ge=0, le=MAX_AUDIT_PAGE),
                    principal: Principal = client_principal) -> list[dict[str, Any]]:
        return [e.model_dump(mode="json") for e in services.audit.recent(limit, principal_id=principal.principal_id)]

    @app.post("/api/v1/pair")
    async def pair(body: PairBody, request: Request) -> dict[str, Any]:
        client = request.client.host if request.client else "-"
        if not services.pair_allowed(client):
            raise HTTPException(status_code=429, detail="too many failed pairing attempts; wait a minute")
        try:
            device, token = await asyncio.to_thread(store.redeem_pairing, body.code, body.name)
        except StoreError as exc:
            services.pair_failed(client)
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        services.audit.record_device_event(device_id=device.device_id, decision="device_paired")
        return {"device_id": device.device_id, "device_token": token, "name": device.name}

    @app.get("/oauth/approve")
    async def approve_form(request: str = "") -> HTMLResponse:
        pending = services.auth.pending(request)
        if pending is None:
            return HTMLResponse(_PAGE.format(body="<h2>This login link expired.</h2><p>Start the connection "
                                                  "again from your AI app.</p>"), status_code=400)
        return _approve_page(pending.client_name, request, str(pending.params.redirect_uri))

    @app.post("/oauth/approve")
    async def approve_submit(req: Request) -> Any:
        form = await req.form()
        request_id = str(form.get("request", ""))
        pending = services.auth.pending(request_id)
        if pending is None:
            return HTMLResponse(_PAGE.format(body="<h2>This login link expired.</h2>"), status_code=400)
        if form.get("decision") == "deny":
            target = services.auth.deny(request_id)
            return RedirectResponse(target, status_code=302) if target else HTMLResponse("denied", 400)
        try:
            target = services.auth.approve(request_id, str(form.get("key", "")))
        except ValueError:
            attempts = services._approve_failures.get(request_id, 0) + 1
            services._approve_failures[request_id] = attempts
            if attempts >= APPROVE_ATTEMPTS:
                services.auth.deny(request_id)
                return HTMLResponse(_PAGE.format(body="<h2>Too many wrong keys.</h2><p>Start again.</p>"), 403)
            return _approve_page(pending.client_name, request_id, str(pending.params.redirect_uri),
                                 "That is not a valid AMC key.")
        except AuthorizeError as exc:
            return HTMLResponse(_PAGE.format(body=f"<h2>{html.escape(exc.error_description or 'error')}</h2>"), 400)
        services._approve_failures.pop(request_id, None)
        return RedirectResponse(target, status_code=302)

    @app.websocket("/devices/connect")
    async def device_connect(websocket: WebSocket) -> None:
        await services.device_endpoint.serve(websocket)

    oauth_hits: dict[str, list[float]] = {}

    @app.middleware("http")
    async def oauth_rate_limit(request: Request, call_next: Any) -> Any:
        # Unauthenticated OAuth entry points: bound them per address so nobody can flood out
        # other people's in-progress logins or registrations.
        if request.url.path in {"/authorize", "/register"}:
            address = request.client.host if request.client else "-"
            now = time.monotonic()
            hits = [t for t in oauth_hits.get(address, []) if now - t < 60] + [now]
            oauth_hits[address] = hits
            if len(oauth_hits) > 10_000:
                oauth_hits.clear()
            if len(hits) > OAUTH_REQUESTS_PER_MINUTE:
                return JSONResponse({"error": "slow_down", "error_description": "too many requests"}, 429)
        return await call_next(request)

    app.mount("/", mcp_app)
    app.add_middleware(BodyLimit, limit=state_limit(store))
    return app


def state_limit(store: RelayStore) -> int:
    return store.state.max_arguments_bytes + 64 * 1024
