"""Outbound device session handling (host <-> relay WebSocket).

Wire protocol (JSON text frames, protocol version 1):

    device -> relay   GET /devices/connect?device_id=<id>&agent_version=<v>
                      Authorization: Bearer <device token>
    relay  -> device  {"type": "hello_ack", "device_id": ..., "protocol": 1, ...}
    relay  -> device  {"type": "call", "call": RemoteCall}
    device -> relay   {"type": "result", "result": RemoteResult}
    device -> relay   {"type": "ping"}      relay -> device {"type": "pong"}

Close codes on rejection: 4401 bad/missing token, 4400 bad device_id,
4403 token not bound to that device_id. After accept: 4409 superseded by a
newer connection for the same device_id, 1008 protocol-violation limit.

Devices never carry a client identity and can only answer calls addressed to
their own ``device_id``; the broker rejects results for other devices and the
session audits the attempt.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from amc.core import PROTOCOL_VERSION, DeviceDisconnected, Environment, RelayBroker

from .audit import AuditSink
from .runtime import validate_device_id

logger = logging.getLogger("amc_relay.devices")

CLOSE_UNAUTHORIZED = 4401
CLOSE_BAD_REQUEST = 4400
CLOSE_FORBIDDEN = 4403
CLOSE_SUPERSEDED = 4409
CLOSE_POLICY_VIOLATION = 1008


class WebSocketTransport:
    """AMC.core ``DeviceTransport`` over a WebSocket; send failures become ``DeviceDisconnected``."""

    def __init__(self, websocket: WebSocket, device_id: str) -> None:
        self.websocket = websocket
        self.device_id = device_id

    async def send_json(self, payload: dict[str, Any]) -> None:
        try:
            await self.websocket.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - any send failure means the device is gone
            raise DeviceDisconnected(self.device_id) from exc


@dataclass
class DeviceSession:
    device_id: str
    transport: WebSocketTransport
    agent_version: str | None
    connected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    violations: int = 0
    superseded: bool = False

    async def close(self, code: int, reason: str) -> None:
        try:
            await self.transport.websocket.close(code=code, reason=reason)
        except Exception:  # noqa: BLE001 - already gone
            pass


class DeviceSessionRegistry:
    """Tracks live device sessions so a reconnect supersedes a stale connection."""

    def __init__(self) -> None:
        self._sessions: dict[str, DeviceSession] = {}

    def get(self, device_id: str) -> DeviceSession | None:
        return self._sessions.get(device_id)

    def attach(self, session: DeviceSession) -> DeviceSession | None:
        previous = self._sessions.get(session.device_id)
        self._sessions[session.device_id] = session
        if previous is not None:
            previous.superseded = True
        return previous

    def detach(self, session: DeviceSession) -> None:
        if self._sessions.get(session.device_id) is session:
            del self._sessions[session.device_id]

    def snapshot(self) -> list[DeviceSession]:
        return list(self._sessions.values())


class DeviceEndpoint:
    def __init__(
        self,
        *,
        broker: RelayBroker,
        audit: AuditSink,
        resolve_device: Callable[[str | None], str | None],
        sessions: DeviceSessionRegistry,
        call_timeout_seconds: float,
        max_result_bytes: int,
        max_protocol_violations: int = 8,
    ) -> None:
        self.broker = broker
        self.audit = audit
        self.resolve_device = resolve_device
        self.sessions = sessions
        self.call_timeout_seconds = call_timeout_seconds
        self.max_result_bytes = max_result_bytes
        self.max_protocol_violations = max_protocol_violations

    async def serve(self, websocket: WebSocket) -> None:
        bound_device = self.resolve_device(websocket.headers.get("authorization"))
        if bound_device is None:
            await self._reject(websocket, CLOSE_UNAUTHORIZED)
            return
        raw_device_id = websocket.query_params.get("device_id") or ""
        try:
            device_id = validate_device_id(raw_device_id)
        except ValueError:
            self._device_event(bound_device, "device_register_rejected", "malformed device_id")
            await self._reject(websocket, CLOSE_BAD_REQUEST)
            return
        if device_id != bound_device:
            # A valid device credential presented for a different device id: spoof attempt.
            self._device_event(
                bound_device, "device_spoof_rejected", "credential not bound to requested device_id"
            )
            await self._reject(websocket, CLOSE_FORBIDDEN)
            return
        agent_version = (websocket.query_params.get("agent_version") or None)
        if agent_version is not None:
            agent_version = agent_version[:64]

        await websocket.accept()
        transport = WebSocketTransport(websocket, device_id)
        session = DeviceSession(device_id=device_id, transport=transport, agent_version=agent_version)
        previous = self.sessions.attach(session)
        if previous is not None:
            self._device_event(device_id, "device_superseded", "newer connection for same device_id")
            await previous.close(CLOSE_SUPERSEDED, "superseded by newer device connection")
        await self.broker.register(device_id=device_id, transport=transport, agent_version=agent_version)
        self._device_event(device_id, "device_connected")
        try:
            await transport.send_json(
                {
                    "type": "hello_ack",
                    "device_id": device_id,
                    "protocol": PROTOCOL_VERSION,
                    "call_timeout_seconds": self.call_timeout_seconds,
                    "max_result_bytes": self.max_result_bytes,
                }
            )
            await self._loop(websocket, session)
        except (WebSocketDisconnect, DeviceDisconnected):
            pass
        finally:
            await self.broker.unregister(device_id, transport)
            self.sessions.detach(session)
            self._device_event(
                device_id,
                "device_disconnected",
                "superseded" if session.superseded else None,
            )

    @staticmethod
    async def _reject(websocket: WebSocket, code: int) -> None:
        # Accept first so the agent receives the close code (a pre-accept close is a bare HTTP 403)
        # and can tell "bad credentials, stop retrying" from a transient failure.
        await websocket.accept()
        await websocket.close(code=code)

    async def _loop(self, websocket: WebSocket, session: DeviceSession) -> None:
        device_id = session.device_id
        while True:
            raw = await websocket.receive_text()
            if len(raw.encode("utf-8")) > self.max_result_bytes:
                if await self._violation(session, "frame exceeds max_result_bytes"):
                    return
                continue
            try:
                payload = json.loads(raw)
            except ValueError:
                if await self._violation(session, "frame is not JSON"):
                    return
                continue
            if not isinstance(payload, dict):
                if await self._violation(session, "frame is not a JSON object"):
                    return
                continue
            kind = payload.get("type")
            if kind == "ping":
                await session.transport.send_json({"type": "pong"})
                continue
            if kind == "pong":
                continue
            if kind != "result":
                if await self._violation(session, "unknown frame type"):
                    return
                continue
            try:
                accepted = await self.broker.handle_result(device_id=device_id, payload=payload)
            except ValidationError:
                if await self._violation(session, "malformed result"):
                    return
                continue
            if not accepted:
                # Unknown/expired call_id or a call addressed to another device.
                result = payload.get("result")
                call_id = result.get("call_id") if isinstance(result, dict) else None
                self._device_event(
                    device_id,
                    "result_rejected",
                    "no pending call for this device",
                    call_id=str(call_id)[:128] if call_id is not None else None,
                )

    async def _violation(self, session: DeviceSession, reason: str) -> bool:
        """Record a protocol violation; returns True when the session must be closed."""
        session.violations += 1
        self._device_event(session.device_id, "device_protocol_violation", reason)
        if session.violations >= self.max_protocol_violations:
            self._device_event(session.device_id, "device_protocol_limit", "closing session")
            await session.close(CLOSE_POLICY_VIOLATION, "protocol violation limit")
            return True
        return False

    def _device_event(
        self,
        device_id: str,
        decision: str,
        reason: str | None = None,
        *,
        call_id: str | None = None,
    ) -> None:
        self.audit.record_device_event(
            device_id=device_id,
            decision=decision,
            reason=reason,
            call_id=call_id,
            environment=Environment.DEV,
        )
