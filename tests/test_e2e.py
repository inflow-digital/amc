"""End-to-end: real relay (uvicorn), real host over WebSocket, real MCP client, OAuth flow."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from amc.host.client import HostClient, HostRejected
from amc.host.config import HostConfig
from amc.host.runner import build_executors
from amc.relay.app import create_app
from amc.relay.store import RelayStore


def _python(code: str) -> str:
    """A shell command running ``python -c code`` (the device shell is PowerShell on Windows)."""
    call = "& " if sys.platform == "win32" else ""
    return f'{call}"{sys.executable}" -c "{code}"'


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RelayServer:
    def __init__(self, store: RelayStore, port: int) -> None:
        self.store = store
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        app = create_app(self.store)
        app.state.relay.sweep_interval = 0.2
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning",
                                ws_max_size=16 * 1024 * 1024)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.url}/livez", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.1)
        raise RuntimeError("relay did not start")

    def stop(self) -> None:
        assert self.server and self.thread
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "amc-home"
    monkeypatch.setenv("AMC_HOME", str(home))
    monkeypatch.setenv("AMC_TEST_SECRET_TOKEN", "must-not-leak")
    work = tmp_path / "work"
    work.mkdir()
    (work / "hello.txt").write_text("hello from the device\nline2\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope", encoding="utf-8")
    store = RelayStore(home / "relay.json")
    port = _free_port()
    store.init(port=port)
    relay = RelayServer(store, port)
    relay.start()
    yield {"store": store, "relay": relay, "work": work, "outside": outside, "tmp": tmp_path}
    relay.stop()


def _pair(env, name="laptop") -> HostConfig:
    code = env["store"].create_pairing(name)
    response = httpx.post(f"{env['relay'].url}/api/v1/pair", json={"code": code, "name": name})
    assert response.status_code == 200, response.text
    data = response.json()
    return HostConfig(relay_url=env["relay"].url, device_id=data["device_id"], device_token=data["device_token"],
                      name=name, roots=[str(env["work"])], cwd=str(env["work"]))


@asynccontextmanager
async def running_host(config: HostConfig):
    executor, status_executor = build_executors(config)
    client = HostClient(config, executor, status_executor=status_executor)
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(client.connected.wait(), 10)
    try:
        yield client
    finally:
        await client.shutdown()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, HostRejected):
            pass
        await executor.shutdown()


@asynccontextmanager
async def mcp_session(url: str, token: str):
    async with streamablehttp_client(f"{url}/mcp", headers={"Authorization": f"Bearer {token}"}) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            yield session


def _text(result) -> str:
    return "\n".join(getattr(block, "text", "") for block in result.content)


def _items(result) -> list:
    assert not result.isError, _text(result)
    structured = result.structuredContent
    if isinstance(structured, dict) and isinstance(structured.get("result"), list):
        return structured["result"]
    return [json.loads(block.text) for block in result.content]


def _payload(result) -> dict:
    assert not result.isError, _text(result)
    return json.loads(result.content[0].text)


async def test_full_flow_with_agent_key(env):
    config = _pair(env)
    key = env["store"].add_client("claude-code", access="full")
    async with running_host(config), mcp_session(env["relay"].url, key) as session:
        tools = {tool.name for tool in (await session.list_tools()).tools}
        assert {"amc_read_file", "amc_write_file", "amc_start_process", "remote_devices", "amc_view_image"} <= tools

        rows = _items(await session.call_tool("remote_devices", {}))
        assert any(row["device_id"] == config.device_id and row["connected"] for row in rows)

        read = _payload(await session.call_tool("amc_read_file", {"device_id": config.device_id,
                                                                  "path": "hello.txt"}))
        assert read["ok"] and "hello from the device" in json.dumps(read)

        target = str(env["work"] / "new" / "out.txt")
        wrote = _payload(await session.call_tool("amc_write_file", {"device_id": config.device_id, "path": target,
                                                                    "content": "written by agent"}))
        assert wrote["ok"]
        assert Path(target).read_text() == "written by agent"

        edited = _payload(await session.call_tool("amc_edit_block", {
            "device_id": config.device_id, "file_path": target, "old_string": "agent", "new_string": "AMC"}))
        assert edited["ok"] and Path(target).read_text() == "written by AMC"

        cmd = _python("import os;print('ENV', sorted(k for k in os.environ if 'TOKEN' in k))")
        proc = _payload(await session.call_tool("amc_start_process", {"device_id": config.device_id,
                                                                      "command": cmd, "timeout_ms": 15000}))
        output = json.dumps(proc)
        assert "ENV" in output
        assert "AMC_TEST_SECRET_TOKEN" not in output and "must-not-leak" not in output  # executor env isolation

        status = _payload(await session.call_tool("amc_system_status", {"device_id": config.device_id}))
        assert status["ok"]

        # host-side confinement: outside the device roots is refused even for a full-access key
        denied = _payload(await session.call_tool("amc_read_file", {
            "device_id": config.device_id, "path": str(env["outside"] / "secret.txt")}))
        assert not denied["ok"] and "outside" in json.dumps(denied)
        escape = _payload(await session.call_tool("amc_read_file", {
            "device_id": config.device_id, "path": str(env["work"] / ".." / "outside" / "secret.txt")}))
        assert not escape["ok"]

        events = _items(await session.call_tool("remote_audit", {"limit": 100}))
        assert events and all(event["principal_id"] == "key:claude-code" for event in events)
        assert "must-not-leak" not in json.dumps(events) and "written by" not in json.dumps(events)


async def test_image_view(env):
    config = _pair(env)
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    (env["work"] / "shot.png").write_bytes(png)
    key = env["store"].add_client("viewer", access="read")
    async with running_host(config), mcp_session(env["relay"].url, key) as session:
        result = await session.call_tool("amc_view_image", {"device_id": config.device_id, "path": "shot.png"})
        assert not result.isError, _text(result)
        assert any(block.type == "image" and block.mimeType == "image/png" for block in result.content)


async def test_access_levels_and_device_scoping(env):
    first = _pair(env, "first")
    second = _pair(env, "second")
    reader = env["store"].add_client("reader", access="read", devices=[first.device_id])
    async with running_host(first), running_host(second), mcp_session(env["relay"].url, reader) as session:
        rows = _items(await session.call_tool("remote_devices", {}))
        assert [row["device_id"] for row in rows] == [first.device_id]  # cannot even see the other device

        ok = _payload(await session.call_tool("amc_read_file", {"device_id": first.device_id, "path": "hello.txt"}))
        assert ok["ok"]
        write = await session.call_tool("amc_write_file", {"device_id": first.device_id, "path": "x.txt",
                                                           "content": "x"})
        assert write.isError and "policy_denied" in _text(write)
        other = await session.call_tool("amc_read_file", {"device_id": second.device_id, "path": "hello.txt"})
        assert other.isError and "policy_denied" in _text(other)
        run = await session.call_tool("amc_start_process", {"device_id": first.device_id, "command": "echo hi"})
        assert run.isError and "policy_denied" in _text(run)


async def test_process_handles_are_private_per_key(env):
    config = _pair(env)
    alice = env["store"].add_client("alice", access="full")
    bob = env["store"].add_client("bob", access="full")
    script = _python("import time;print('ready',flush=True);time.sleep(30)")
    async with running_host(config):
        async with mcp_session(env["relay"].url, alice) as session:
            started = json.dumps(_payload(await session.call_tool("amc_start_process", {
                "device_id": config.device_id, "command": script, "timeout_ms": 3000})))
            pid = int(re.search(r"PID (\d+)", started).group(1))
        async with mcp_session(env["relay"].url, bob) as session:
            peek = _payload(await session.call_tool("amc_read_process_output",
                                                    {"device_id": config.device_id, "pid": pid}))
            assert not peek["ok"]
            kill = _payload(await session.call_tool("amc_force_terminate", {"device_id": config.device_id, "pid": pid}))
            assert not kill["ok"]
        async with mcp_session(env["relay"].url, alice) as session:
            done = _payload(await session.call_tool("amc_force_terminate", {"device_id": config.device_id, "pid": pid}))
            assert done["ok"]


async def test_auth_failures(env):
    config = _pair(env)
    async with running_host(config):
        assert httpx.post(f"{env['relay'].url}/mcp", json={}).status_code == 401
        bad = httpx.post(f"{env['relay'].url}/mcp", json={}, headers={"Authorization": "Bearer amck_wrong"})
        assert bad.status_code == 401
        assert httpx.get(f"{env['relay'].url}/api/v1/devices").status_code == 401
        # a device token is not an agent key
        as_device = httpx.get(f"{env['relay'].url}/api/v1/devices",
                              headers={"Authorization": f"Bearer {config.device_token}"})
        assert as_device.status_code == 401
        # pairing codes are single use
        code = env["store"].create_pairing("x")
        assert httpx.post(f"{env['relay'].url}/api/v1/pair", json={"code": code}).status_code == 200
        assert httpx.post(f"{env['relay'].url}/api/v1/pair", json={"code": code}).status_code == 403


async def test_wrong_device_token_is_rejected_permanently(env):
    config = _pair(env)
    config.device_token = "amcd_" + "x" * 40
    executor, _ = build_executors(config)
    client = HostClient(config, executor)
    with pytest.raises(HostRejected):
        await asyncio.wait_for(client.run(), 10)


async def test_removed_key_and_device_stop_working(env):
    config = _pair(env)
    key = env["store"].add_client("temp", access="read")
    async with running_host(config) as host:
        headers = {"Authorization": f"Bearer {key}"}
        assert httpx.get(f"{env['relay'].url}/api/v1/devices", headers=headers).status_code == 200
        env["store"].remove_client("temp")
        assert httpx.get(f"{env['relay'].url}/api/v1/devices", headers=headers).status_code == 401
        env["store"].remove_device(config.device_id)
        task = asyncio.current_task()
        assert task is not None
        deadline = time.monotonic() + 10
        while host.connected.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert not host.connected.is_set()  # relay closed the removed device's session


async def test_host_reconnects_after_relay_restart(env):
    config = _pair(env)
    config.reconnect_base_delay = 0.1
    config.reconnect_max_delay = 0.5
    key = env["store"].add_client("k", access="read")
    async with running_host(config) as host:
        env["relay"].stop()
        await asyncio.sleep(0.5)
        env["relay"].start()
        host.connected.clear()
        await asyncio.wait_for(host.connected.wait(), 15)
        rows = httpx.get(f"{env['relay'].url}/api/v1/devices", headers={"Authorization": f"Bearer {key}"}).json()
        assert rows[0]["connected"]


async def test_oauth_flow(env):
    """Claude.ai / ChatGPT style: discovery -> dynamic registration -> PKCE -> consent with AMC key -> MCP."""
    config = _pair(env)
    key = env["store"].add_client("claude-web", access="read")
    base = env["relay"].url
    async with running_host(config), httpx.AsyncClient(base_url=base) as http:
        unauth = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert unauth.status_code == 401 and "resource_metadata" in unauth.headers.get("www-authenticate", "")
        meta = (await http.get("/.well-known/oauth-authorization-server")).json()
        redirect_uri = "http://127.0.0.1:9/callback"
        reg = (await http.post(meta["registration_endpoint"].replace(base, ""), json={
            "client_name": "Test Claude", "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none"})).json()
        verifier = "v" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        auth = await http.get("/authorize", params={
            "response_type": "code", "client_id": reg["client_id"], "redirect_uri": redirect_uri,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "st"})
        assert auth.status_code == 302
        approve_url = auth.headers["location"]
        request_id = parse_qs(urlsplit(approve_url).query)["request"][0]
        page = await http.get(f"/oauth/approve?request={request_id}")
        assert "Test Claude" in page.text
        wrong = await http.post("/oauth/approve", data={"request": request_id, "key": "amck_bad", "decision": "allow"})
        assert wrong.status_code == 400
        ok = await http.post("/oauth/approve", data={"request": request_id, "key": key, "decision": "allow"})
        assert ok.status_code == 302
        query = parse_qs(urlsplit(ok.headers["location"]).query)
        assert query["state"] == ["st"]
        token = (await http.post("/token", data={
            "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": redirect_uri,
            "client_id": reg["client_id"], "code_verifier": verifier})).json()
        assert token["access_token"].startswith("amco_")
    async with running_host(config), mcp_session(base, token["access_token"]) as session:
        read = _payload(await session.call_tool("amc_read_file", {"device_id": config.device_id, "path": "hello.txt"}))
        assert read["ok"]
    async with httpx.AsyncClient(base_url=base) as http:
        refreshed = (await http.post("/token", data={
            "grant_type": "refresh_token", "refresh_token": token["refresh_token"],
            "client_id": reg["client_id"]})).json()
        assert refreshed["access_token"].startswith("amco_")
        # removing the approving key revokes every OAuth token it approved
        env["store"].remove_client("claude-web")
        denied = await http.get("/api/v1/devices", headers={"Authorization": f"Bearer {refreshed['access_token']}"})
        assert denied.status_code == 401


async def test_system_status_falls_back_for_legacy_hosts(env):
    """Legacy hosts (DesktopCommander only) do not know system_status; the relay falls back."""
    from amc.host.executors.base import text_result

    config = _pair(env)
    executor, _ = build_executors(config)

    class LegacyExecutor:
        async def start(self):
            pass

        async def execute(self, *, call_id, tool, arguments, principal_id):
            if tool == "system_status":
                return text_result(call_id, "Error: Unknown tool: system_status", is_error=True)
            return await executor.execute(call_id=call_id, tool=tool, arguments=arguments, principal_id=principal_id)

        async def shutdown(self):
            await executor.shutdown()

    client = HostClient(config, LegacyExecutor())
    task = asyncio.create_task(client.run())
    await asyncio.wait_for(client.connected.wait(), 10)
    try:
        full = env["store"].add_client("full", access="full")
        async with mcp_session(env["relay"].url, full) as session:
            result = _payload(await session.call_tool("amc_system_status", {"device_id": config.device_id}))
            assert "Process started" in json.dumps(result)
    finally:
        await client.shutdown()
        task.cancel()
