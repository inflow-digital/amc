from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, Protocol

from .contracts import DeviceDescriptor, RemoteCall, RemoteResult


class DeviceNotConnected(RuntimeError):
    pass


class DeviceDisconnected(RuntimeError):
    pass


class RelayTimeout(TimeoutError):
    pass


class DeviceTransport(Protocol):
    def send_json(self, payload: dict[str, Any]) -> Awaitable[None]: ...


class RelayBroker:
    def __init__(self) -> None:
        self._devices: dict[str, tuple[DeviceDescriptor, DeviceTransport]] = {}
        self._pending: dict[str, tuple[str, asyncio.Future[RemoteResult]]] = {}
        self._lock = asyncio.Lock()
    async def register(
        self,
        *,
        device_id: str,
        transport: DeviceTransport,
        agent_version: str | None = None,
    ) -> DeviceDescriptor:
        descriptor = DeviceDescriptor(
            device_id=device_id,
            agent_version=agent_version,
        )
        async with self._lock:
            self._devices[device_id] = (descriptor, transport)
        return descriptor

    async def unregister(self, device_id: str, transport: DeviceTransport) -> None:
        async with self._lock:
            current = self._devices.get(device_id)
            if current is None or current[1] is not transport:
                return
            del self._devices[device_id]
            doomed = [
                (call_id, future)
                for call_id, (pending_device, future) in self._pending.items()
                if pending_device == device_id
            ]
            for call_id, future in doomed:
                self._pending.pop(call_id, None)
                if not future.done():
                    future.set_exception(DeviceDisconnected(device_id))
    async def list_devices(self) -> list[DeviceDescriptor]:
        async with self._lock:
            return [descriptor.model_copy() for descriptor, _ in self._devices.values()]

    async def call(self, call: RemoteCall, *, timeout: float = 30.0) -> RemoteResult:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[RemoteResult] = loop.create_future()
        async with self._lock:
            current = self._devices.get(call.device_id)
            if current is None:
                raise DeviceNotConnected(call.device_id)
            transport = current[1]
            self._pending[call.call_id] = (call.device_id, future)
        try:
            await transport.send_json(
                {
                    "type": "call",
                    "call": call.model_dump(mode="json"),
                }
            )
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except TimeoutError as exc:
                raise RelayTimeout(call.call_id) from exc
        finally:
            async with self._lock:
                self._pending.pop(call.call_id, None)
    async def handle_result(
        self,
        *,
        device_id: str,
        payload: dict[str, Any],
    ) -> bool:
        if payload.get("type") != "result":
            return False
        result = RemoteResult.model_validate(payload.get("result"))
        async with self._lock:
            pending = self._pending.get(result.call_id)
            if pending is None:
                return False
            expected_device, future = pending
            if expected_device != device_id:
                return False
            if not future.done():
                future.set_result(result)
        return True
