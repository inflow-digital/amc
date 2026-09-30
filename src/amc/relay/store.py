"""Relay state: ``relay.json`` in the AMC config dir.

Secrets are never stored: agent keys, device tokens and pairing codes are kept as
SHA-256 hashes, and the plaintext is shown exactly once when created. The relay
re-reads the file when it changes on disk, so ``amc key add`` / ``amc relay pair``
run from another terminal take effect without a restart.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from amc.core import ACCESS_LEVELS, ActionClass, PolicyGrant
from amc.paths import config_dir, write_private_json

RELAY_FILE = "relay.json"
DEFAULT_PORT = 8790
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class StoreError(RuntimeError):
    pass


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def new_client_key() -> str:
    return "amck_" + secrets.token_urlsafe(32)


def new_device_token() -> str:
    return "amcd_" + secrets.token_urlsafe(32)


def new_pairing_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_code(code: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", code).upper()


def slug(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-._").lower()
    return cleaned[:40] or "device"


def validate_name(name: str, what: str = "name") -> str:
    if not NAME_RE.fullmatch(name):
        raise StoreError(f"{what} must match {NAME_RE.pattern} (letters, digits, . _ -)")
    return name


@dataclass
class ClientKey:
    name: str
    key_sha256: str
    access: str = "full"
    devices: list[str] = field(default_factory=lambda: ["*"])
    tools: list[str] = field(default_factory=lambda: ["*"])
    paths: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    actions: list[str] | None = None  # explicit action classes; overrides ``access``

    @property
    def principal_id(self) -> str:
        return f"key:{self.name}"

    def action_set(self) -> frozenset[ActionClass]:
        if self.actions is not None:
            return frozenset(ActionClass(value) for value in self.actions)
        try:
            return ACCESS_LEVELS[self.access]
        except KeyError:
            raise StoreError(f"key {self.name}: unknown access level {self.access!r}") from None

    def grant(self) -> PolicyGrant:
        return PolicyGrant(
            principal_id=self.principal_id,
            device_ids=frozenset(self.devices),
            actions=self.action_set(),
            tools=frozenset(self.tools),
            path_prefixes=tuple(self.paths),
        )


@dataclass
class Device:
    device_id: str
    token_sha256: str
    name: str = ""
    created_at: str = field(default_factory=now_iso)


@dataclass
class Pairing:
    code_sha256: str
    name: str
    expires_at: float


@dataclass
class RelayState:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    public_url: str | None = None
    clients: list[ClientKey] = field(default_factory=list)
    devices: list[Device] = field(default_factory=list)
    pairings: list[Pairing] = field(default_factory=list)
    call_timeout_seconds: float = 60.0
    max_inflight_per_device: int = 16
    max_arguments_bytes: int = 256 * 1024
    max_result_bytes: int = 16 * 1024 * 1024
    trust_proxy_headers: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "listen": {"host": self.host, "port": self.port},
            "public_url": self.public_url,
            "limits": {
                "call_timeout_seconds": self.call_timeout_seconds,
                "max_inflight_per_device": self.max_inflight_per_device,
                "max_arguments_bytes": self.max_arguments_bytes,
                "max_result_bytes": self.max_result_bytes,
            },
            "trust_proxy_headers": self.trust_proxy_headers,
            "clients": [vars(client) for client in self.clients],
            "devices": [vars(device) for device in self.devices],
            "pairings": [vars(pairing) for pairing in self.pairings],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RelayState:
        if data.get("version") != 1:
            raise StoreError(f"unsupported relay.json version: {data.get('version')!r}")
        listen = data.get("listen") or {}
        limits = data.get("limits") or {}
        state = cls(
            host=str(listen.get("host", "127.0.0.1")),
            port=int(listen.get("port", DEFAULT_PORT)),
            public_url=(data.get("public_url") or None),
            clients=[ClientKey(**item) for item in data.get("clients", [])],
            devices=[Device(**item) for item in data.get("devices", [])],
            pairings=[Pairing(**item) for item in data.get("pairings", [])],
            trust_proxy_headers=bool(data.get("trust_proxy_headers", False)),
        )
        for key in ("call_timeout_seconds", "max_arguments_bytes", "max_result_bytes",
                    "max_inflight_per_device"):
            if key in limits:
                setattr(state, key, type(getattr(state, key))(limits[key]))
        state.validate()
        return state

    def validate(self) -> None:
        names = [client.name for client in self.clients]
        if len(names) != len(set(names)):
            raise StoreError("duplicate agent key name in relay.json")
        ids = [device.device_id for device in self.devices]
        if len(ids) != len(set(ids)):
            raise StoreError("duplicate device_id in relay.json")
        hashes = [c.key_sha256 for c in self.clients] + [d.token_sha256 for d in self.devices]
        if len(hashes) != len(set(hashes)):
            raise StoreError("the same secret is registered twice (agent keys and device tokens must differ)")
        for client in self.clients:
            validate_name(client.name, "key name")
            client.action_set()
        for device in self.devices:
            if not DEVICE_ID_RE.fullmatch(device.device_id):
                raise StoreError(f"invalid device_id {device.device_id!r}")

    def client_by_name(self, name: str) -> ClientKey | None:
        return next((client for client in self.clients if client.name == name), None)

    def client_by_key(self, key: str) -> ClientKey | None:
        digest = sha256(key)
        return next((client for client in self.clients if secrets.compare_digest(client.key_sha256, digest)),
                    None)

    def device_by_token(self, token: str) -> Device | None:
        digest = sha256(token)
        return next((d for d in self.devices if secrets.compare_digest(d.token_sha256, digest)), None)


class RelayStore:
    """File-backed ``RelayState`` with reload-on-change and a cross-process write lock."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_dir() / RELAY_FILE
        self._mtime: float | None = None
        self._state: RelayState | None = None

    def exists(self) -> bool:
        return self.path.exists()

    @property
    def state(self) -> RelayState:
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            raise StoreError(f"{self.path} not found — run: amc relay init") from None
        if self._state is None or mtime != self._mtime:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._state = RelayState.from_json(data)
            self._mtime = mtime
        return self._state

    def save(self, state: RelayState) -> None:
        state.validate()
        write_private_json(self.path, json.dumps(state.to_json(), indent=2, ensure_ascii=False) + "\n")
        self._state = state
        self._mtime = self.path.stat().st_mtime_ns

    @contextmanager
    def edit(self):
        """Lock, load fresh, yield the state for mutation, save."""
        with self._lock():
            self._state = None
            state = self.state if self.exists() else RelayState()
            yield state
            self.save(state)

    @contextmanager
    def _lock(self, timeout: float = 10.0):
        lock = self.path.with_name(self.path.name + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > 30:
                        lock.unlink(missing_ok=True)  # stale lock from a crashed writer
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise StoreError(f"relay.json is locked by another process ({lock})") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            lock.unlink(missing_ok=True)

    # --- operations used by the CLI and the relay -----------------------------------------------

    def init(self, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT, public_url: str | None = None,
             force: bool = False) -> RelayState:
        if self.exists() and not force:
            raise StoreError(f"{self.path} already exists (use --force to reset it)")
        state = RelayState(host=host, port=port, public_url=public_url)
        with self._lock():
            self.save(state)
        return state

    def add_client(self, name: str, *, access: str = "full", devices: list[str] | None = None,
                   paths: list[str] | None = None, tools: list[str] | None = None,
                   replace: bool = False, key: str | None = None) -> str:
        """Create an agent key (or register an existing secret ``key``) and return the plaintext."""
        validate_name(name, "key name")
        if access not in ACCESS_LEVELS:
            raise StoreError(f"access must be one of {', '.join(ACCESS_LEVELS)}")
        if key is not None and len(key) < 16:
            raise StoreError("an imported key must be at least 16 characters")
        key = key or new_client_key()
        with self.edit() as state:
            existing = state.client_by_name(name)
            if existing is not None and not replace:
                raise StoreError(f"agent key {name!r} already exists (remove it or use --replace)")
            state.clients = [client for client in state.clients if client.name != name]
            state.clients.append(ClientKey(name=name, key_sha256=sha256(key), access=access,
                                           devices=devices or ["*"], paths=paths or [],
                                           tools=tools or ["*"]))
        return key

    def remove_client(self, name: str) -> bool:
        with self.edit() as state:
            before = len(state.clients)
            state.clients = [client for client in state.clients if client.name != name]
            return len(state.clients) != before

    def create_pairing(self, name: str = "", ttl_seconds: int = 900) -> str:
        code = new_pairing_code()
        with self.edit() as state:
            now = time.time()
            state.pairings = [p for p in state.pairings if p.expires_at > now]
            state.pairings.append(Pairing(code_sha256=sha256(normalize_code(code)), name=name,
                                          expires_at=now + ttl_seconds))
        return code

    def redeem_pairing(self, code: str, name: str = "") -> tuple[Device, str]:
        digest = sha256(normalize_code(code))
        with self.edit() as state:
            now = time.time()
            state.pairings = [p for p in state.pairings if p.expires_at > now]
            match = next((p for p in state.pairings if secrets.compare_digest(p.code_sha256, digest)), None)
            if match is None:
                raise StoreError("pairing code is invalid or expired")
            state.pairings.remove(match)
            label = match.name or name or "device"
            existing = {device.device_id for device in state.devices}
            device_id = f"{slug(label)}-{secrets.token_hex(3)}"
            while device_id in existing:
                device_id = f"{slug(label)}-{secrets.token_hex(3)}"
            token = new_device_token()
            device = Device(device_id=device_id, token_sha256=sha256(token), name=label)
            state.devices.append(device)
        return device, token

    def add_device(self, device_id: str, token: str, name: str = "") -> Device:
        """Register a device with an existing token (migration / scripted installs)."""
        if not DEVICE_ID_RE.fullmatch(device_id):
            raise StoreError(f"invalid device_id {device_id!r}")
        if len(token) < 16:
            raise StoreError("a device token must be at least 16 characters")
        with self.edit() as state:
            state.devices = [d for d in state.devices if d.device_id != device_id]
            device = Device(device_id=device_id, token_sha256=sha256(token), name=name or device_id)
            state.devices.append(device)
        return device

    def remove_device(self, device_id: str) -> bool:
        with self.edit() as state:
            before = len(state.devices)
            state.devices = [d for d in state.devices if d.device_id != device_id]
            return len(state.devices) != before
