"""Governed remote_call path: principal × device × action × resource scope.

The runtime is the only code path that turns a caller request into a
``RemoteCall`` on the AMC.core broker. It fails closed on every step (input
shape, policy, device availability, in-flight limit) and audits every outcome,
including timeout, device disconnect and caller cancellation.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from collections.abc import Callable
from typing import Any

from amc.core import (
    ActionClass,
    AuditEvent,
    DeviceDisconnected,
    DeviceNotConnected,
    PolicyDenied,
    Principal,
    RelayBroker,
    RelayTimeout,
    RemoteCall,
    RemoteExecutionPolicy,
    RemoteResult,
    classify_action,
)

from .audit import AuditSink

DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


def validate_device_id(device_id: str) -> str:
    if not DEVICE_ID_RE.fullmatch(device_id):
        raise ValueError("device_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    return device_id


def validate_tool_name(tool: str) -> str:
    if not TOOL_NAME_RE.fullmatch(tool):
        raise ValueError("tool must match [A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
    return tool


class RelayError(RuntimeError):
    """Base class for caller-visible relay failures; ``code`` is a stable machine string."""

    code = "relay_error"
    http_status = 500

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.code)
        self.detail = detail or self.code

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}" if self.detail != self.code else self.code


class InvalidRequest(RelayError):
    code = "invalid_request"
    http_status = 422


class Denied(RelayError):
    code = "policy_denied"
    http_status = 403


class DeviceOffline(RelayError):
    code = "device_not_connected"
    http_status = 503


class DeviceBusy(RelayError):
    code = "device_busy"
    http_status = 429


class CallTimeout(RelayError):
    code = "timeout"
    http_status = 504


class DeviceLost(RelayError):
    code = "device_disconnected"
    http_status = 502


class RelayRuntime:
    def __init__(
        self,
        *,
        broker: RelayBroker,
        policy: Callable[[], RemoteExecutionPolicy],
        audit: AuditSink,
        call_timeout_seconds: float = 30.0,
        max_inflight_per_device: int = 16,
        max_arguments_bytes: int = 256 * 1024,
    ) -> None:
        self.broker = broker
        self.policy = policy
        self.audit = audit
        self.call_timeout_seconds = call_timeout_seconds
        self.max_inflight_per_device = max_inflight_per_device
        self.max_arguments_bytes = max_arguments_bytes
        self._inflight: Counter[str] = Counter()

    def inflight(self, device_id: str) -> int:
        return self._inflight[device_id]

    async def invoke(
        self,
        *,
        principal: Principal,
        device_id: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
    ) -> RemoteResult:
        args = dict(arguments or {})
        self._validate_shape(principal, device_id=device_id, tool=tool, arguments=args)
        try:
            action, grant = self.policy().authorize_grant(
                principal=principal,
                device_id=device_id,
                tool=tool,
                arguments=args,
            )
        except PolicyDenied as exc:
            self._audit(principal, device_id, tool, self._safe_action(tool), "deny", reason=str(exc))
            raise Denied(str(exc)) from exc

        call = RemoteCall(device_id=device_id, tool=tool, arguments=args, principal=principal, action=action,
                          scope_paths=list(grant.path_prefixes))
        if self._inflight[device_id] >= self.max_inflight_per_device:
            self._audit(
                principal,
                device_id,
                tool,
                action,
                "reject",
                reason="device in-flight limit",
                call_id=call.call_id,
            )
            raise DeviceBusy(f"device {device_id} has {self.max_inflight_per_device} calls in flight")
        self._audit(principal, device_id, tool, action, "allow", call_id=call.call_id)
        self._inflight[device_id] += 1
        try:
            result = await self.broker.call(call, timeout=self.call_timeout_seconds)
        except DeviceNotConnected as exc:
            self._audit(principal, device_id, tool, action, "device_not_connected", call_id=call.call_id)
            raise DeviceOffline(f"device {device_id} is not connected") from exc
        except RelayTimeout as exc:
            self._audit(principal, device_id, tool, action, "timeout", call_id=call.call_id)
            raise CallTimeout(f"no result within {self.call_timeout_seconds:g}s") from exc
        except DeviceDisconnected as exc:
            self._audit(principal, device_id, tool, action, "device_disconnected", call_id=call.call_id)
            raise DeviceLost(f"device {device_id} disconnected before returning a result") from exc
        except asyncio.CancelledError:
            # Caller went away (MCP client disconnect). That is not a signal to terminate
            # anything on the device; we only record that the relay stopped waiting.
            self._audit(principal, device_id, tool, action, "caller_cancelled", call_id=call.call_id)
            raise
        finally:
            self._inflight[device_id] -= 1
            if self._inflight[device_id] <= 0:
                del self._inflight[device_id]
        # Device-reported error text goes to the caller only; it may echo tool input
        # (e.g. an edit_block snippet), so the audit trail records just the outcome.
        self._audit(
            principal,
            device_id,
            tool,
            action,
            "complete" if result.ok else "error",
            reason=None if result.ok else "device reported error",
            call_id=call.call_id,
        )
        return result

    def _validate_shape(
        self,
        principal: Principal,
        *,
        device_id: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> None:
        try:
            validate_device_id(device_id)
            validate_tool_name(tool)
        except ValueError as exc:
            self._audit(
                principal,
                device_id[:128] or "-",
                tool[:128] or "-",
                ActionClass.ADMIN,
                "reject",
                reason=str(exc),
            )
            raise InvalidRequest(str(exc)) from exc
        if not all(isinstance(key, str) for key in arguments):
            raise InvalidRequest("argument keys must be strings")
        try:
            encoded = json.dumps(arguments, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise InvalidRequest("arguments must be JSON-serialisable") from exc
        if len(encoded.encode("utf-8")) > self.max_arguments_bytes:
            self._audit(
                principal, device_id, tool, self._safe_action(tool), "reject", reason="arguments too large"
            )
            raise InvalidRequest(f"arguments exceed {self.max_arguments_bytes} bytes")

    def _audit(
        self,
        principal: Principal,
        device_id: str,
        tool: str,
        action: ActionClass,
        decision: str,
        *,
        reason: str | None = None,
        call_id: str | None = None,
    ) -> None:
        self.audit.record(
            AuditEvent(
                principal_id=principal.principal_id,
                principal_class=principal.principal_class,
                environment=principal.environment,
                device_id=device_id,
                tool=tool,
                action=action,
                decision=decision,
                reason=reason,
                call_id=call_id,
            )
        )

    @staticmethod
    def _safe_action(tool: str) -> ActionClass:
        try:
            return classify_action(tool)
        except PolicyDenied:
            return ActionClass.ADMIN
