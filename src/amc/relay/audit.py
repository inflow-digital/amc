"""Audit sink for AMC.relay.

Every remote call and every device-session security event produces an
``AuditEvent``. Events carry identities, tool name, action class and decision —
never call arguments, tool results, file contents or bearer tokens (
secrets never enter ordinary logs).
"""

from __future__ import annotations

import json
import logging
from collections import deque
from pathlib import Path

from amc.core import ActionClass, AuditEvent, Environment, PrincipalClass

logger = logging.getLogger("amc_relay.audit")

REASON_MAX_CHARS = 256
"""Reasons are operator-facing status strings; truncate so a device cannot smuggle bulk content."""

DEVICE_EVENT_PRINCIPAL = "device"
"""Synthetic principal id for device-session events (not a client identity)."""


def safe_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    reason = str(reason).replace("\n", " ").replace("\r", " ")
    if len(reason) > REASON_MAX_CHARS:
        return reason[:REASON_MAX_CHARS] + "…"
    return reason


class AuditSink:
    """Bounded in-memory ring plus optional append-only JSONL file."""

    def __init__(self, *, max_events: int = 10_000, log_path: str | Path | None = None) -> None:
        self._events: deque[AuditEvent] = deque(maxlen=max_events)
        self._path = Path(log_path) if log_path else None
        self.dropped = 0

    def record(self, event: AuditEvent) -> AuditEvent:
        event = event.model_copy(update={"reason": safe_reason(event.reason)})
        if len(self._events) == self._events.maxlen:
            self.dropped += 1
        self._events.append(event)
        line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        logger.info("audit %s", line)
        if self._path is not None:
            try:
                self._rotate_if_large()
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError as exc:  # never fail a call because the audit file is unwritable
                logger.warning("audit file write failed: %s", exc)
        return event

    def _rotate_if_large(self, limit: int = 50 * 1024 * 1024, keep: int = 3) -> None:
        assert self._path is not None
        try:
            if self._path.stat().st_size < limit:
                return
        except FileNotFoundError:
            return
        for index in range(keep - 1, 0, -1):
            older = self._path.with_name(f"{self._path.name}.{index}")
            if older.exists():
                older.replace(self._path.with_name(f"{self._path.name}.{index + 1}"))
        self._path.replace(self._path.with_name(f"{self._path.name}.1"))

    def record_device_event(
        self,
        *,
        device_id: str,
        decision: str,
        reason: str | None = None,
        call_id: str | None = None,
        environment: Environment = Environment.DEV,
    ) -> AuditEvent:
        """Record a device-session event (register, supersede, spoof attempt, protocol error)."""
        return self.record(
            AuditEvent(
                principal_id=DEVICE_EVENT_PRINCIPAL,
                principal_class=PrincipalClass.HUB_AGENT,
                environment=environment,
                device_id=device_id,
                tool="-",
                action=ActionClass.ADMIN,
                decision=decision,
                reason=reason,
                call_id=call_id,
            )
        )

    def recent(self, limit: int | None = None, *, principal_id: str | None = None) -> list[AuditEvent]:
        events = list(self._events)
        if principal_id is not None:
            events = [event for event in events if event.principal_id == principal_id]
        if limit is not None and limit >= 0:
            events = events[-limit:] if limit else []
        return events

    def __len__(self) -> int:
        return len(self._events)
