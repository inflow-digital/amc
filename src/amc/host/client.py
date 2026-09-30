"""Device agent: one outbound WebSocket to the relay, calls executed locally.

The host never listens on any port. It reconnects with bounded exponential
backoff + jitter, runs calls concurrently (bounded), re-checks every path
argument against its own roots, and hands the call to the configured executor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import websockets
from pydantic import ValidationError
from websockets.exceptions import ConnectionClosed, InvalidStatus

from amc import __version__
from amc.core import PROTOCOL_VERSION, RemoteCall, RemoteResult

from .config import HostConfig
from .confine import Confinement, ConfinementError
from .executors.base import Executor, text_result

logger = logging.getLogger("amc.host")

MAX_CONCURRENT_CALLS = 16
FATAL_CLOSE_CODES = {4401, 4403}


def _device_url(base: str, *, device_id: str, agent_version: str) -> str:
    parts = urlsplit(base)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["device_id"] = device_id
    query["agent_version"] = agent_version
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class HostRejected(RuntimeError):
    """The relay refused this device's identity; retrying cannot help."""


class HostClient:
    def __init__(self, config: HostConfig, executor: Executor, *, status_executor: Executor | None = None) -> None:
        self.config = config
        self.executor = executor
        self.status_executor = status_executor
        self.confinement = Confinement(config.roots, config.cwd, config.protected)
        self.running = False
        self.connected = asyncio.Event()
        self._ws: Any | None = None
        self._attempts = 0
        self._send_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_CALLS)
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        self.running = True
        while self.running:
            try:
                await self._run_session()
                self._attempts = 0
            except HostRejected:
                raise
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd else None
                if code in FATAL_CLOSE_CODES:
                    raise HostRejected(
                        f"relay rejected this device (code {code}); it was removed or its token is wrong — "
                        "pair again with: amc host pair <relay-url> <code>"
                    ) from exc
                logger.warning("relay disconnected (code %s)", code)
            except InvalidStatus as exc:
                logger.warning("relay refused the connection: HTTP %s", exc.response.status_code)
            except (OSError, TimeoutError, websockets.WebSocketException) as exc:
                logger.warning("cannot reach relay: %s", exc)
            except Exception as exc:  # noqa: BLE001 - reconnect boundary
                logger.warning("relay session failed: %s: %s", type(exc).__name__, exc)
            finally:
                self.connected.clear()
            if self.running:
                await self._sleep_backoff()

    async def _run_session(self) -> None:
        url = _device_url(self.config.ws_url, device_id=self.config.device_id,
                          agent_version=f"amc-host/{__version__}")
        logger.info("connecting to %s as %s", self.config.ws_url, self.config.device_id)
        async with websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {self.config.device_token}"},
            max_size=64 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            self._ws = ws
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if (
                not isinstance(hello, dict)
                or hello.get("type") != "hello_ack"
                or hello.get("device_id") != self.config.device_id
                or hello.get("protocol") != PROTOCOL_VERSION
            ):
                raise RuntimeError("unexpected relay hello")
            self._attempts = 0
            self.connected.set()
            logger.info("connected as %s", self.config.device_id)
            async for raw in ws:
                if not self.running:
                    break
                self._handle_frame(raw)

    def _handle_frame(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (UnicodeDecodeError, ValueError):
            logger.warning("ignored malformed relay frame")
            return
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if kind == "ping":
            self._spawn(self._send({"type": "pong"}))
            return
        if kind != "call":
            return
        try:
            call = RemoteCall.model_validate(payload.get("call"))
        except ValidationError:
            logger.warning("ignored malformed call")
            return
        if call.device_id != self.config.device_id:
            logger.warning("ignored call addressed to another device")
            return
        self._spawn(self._execute(call))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def execute_call(self, call: RemoteCall) -> RemoteResult:
        try:
            self.confinement.check(call.arguments, call.scope_paths)
        except ConfinementError as exc:
            return text_result(call.call_id, f"denied by device: {exc}", is_error=True)
        executor = self.executor
        if call.tool == "system_status" and self.status_executor is not None:
            executor = self.status_executor
        return await executor.execute(call_id=call.call_id, tool=call.tool, arguments=call.arguments,
                                      principal_id=call.principal.principal_id)

    async def _execute(self, call: RemoteCall) -> None:
        async with self._slots:
            logger.info("call %s %s", call.call_id, call.tool)
            try:
                result = await self.execute_call(call)
            except Exception as exc:  # noqa: BLE001 - never lose a reply
                result = text_result(call.call_id, f"{type(exc).__name__}: {exc}", is_error=True)
        try:
            await self._send({"type": "result", "result": result.model_dump(mode="json")})
        except Exception as exc:  # noqa: BLE001 - connection dropped; relay already timed out the call
            logger.warning("could not deliver result %s: %s", call.call_id, exc)

    async def _send(self, payload: dict[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise RuntimeError("relay not connected")
        async with self._send_lock:
            await ws.send(json.dumps(payload, ensure_ascii=False))

    async def _sleep_backoff(self) -> None:
        delay = min(self.config.reconnect_base_delay * (2 ** min(self._attempts, 6)), self.config.reconnect_max_delay)
        delay *= random.uniform(0.75, 1.25)
        self._attempts += 1
        await asyncio.sleep(delay)

    async def shutdown(self) -> None:
        self.running = False
        for task in list(self._tasks):
            task.cancel()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
