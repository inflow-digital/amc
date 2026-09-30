"""Wire and policy contracts shared by relay and host.

The relay <-> host WebSocket protocol (version 1) is:

    host  -> relay   GET /devices/connect?device_id=<id>&agent_version=<v>
                     Authorization: Bearer <device token>
    relay -> host    {"type": "hello_ack", "device_id": ..., "protocol": 1, ...}
    relay -> host    {"type": "call", "call": RemoteCall}
    host  -> relay   {"type": "result", "result": RemoteResult}
    either           {"type": "ping"} / {"type": "pong"}
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

PROTOCOL_VERSION = 1


class PrincipalClass(StrEnum):
    HUMAN = "human"
    DEV_CLI = "dev-cli"
    HUB_AGENT = "hub-agent"
    CI_BOT = "ci-bot"


class Environment(StrEnum):
    DEV = "dev"
    STAGING = "staging"
    PROD_OPS = "prod-ops"


class ActionClass(StrEnum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    ADMIN = "admin"


class Principal(BaseModel):
    """The authenticated caller (an AI agent / MCP client) a call is made on behalf of."""

    principal_id: str = Field(min_length=1)
    principal_class: PrincipalClass = PrincipalClass.HUB_AGENT
    environment: Environment = Environment.DEV


class RemoteCall(BaseModel):
    call_id: str = Field(default_factory=lambda: str(uuid4()))
    device_id: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    principal: Principal
    action: ActionClass
    # Path prefixes the caller's key is limited to; the host enforces them on real paths.
    scope_paths: list[str] = Field(default_factory=list)


class RemoteResult(BaseModel):
    call_id: str
    ok: bool
    result: Any | None = None
    error: str | None = None


class DeviceDescriptor(BaseModel):
    device_id: str
    connected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    agent_version: str | None = None


class AuditEvent(BaseModel):
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    principal_id: str
    principal_class: PrincipalClass
    environment: Environment
    device_id: str
    tool: str
    action: ActionClass
    decision: str
    reason: str | None = None
    call_id: str | None = None
