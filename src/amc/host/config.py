"""Host (device agent) configuration, stored as ``host.json`` in the AMC config dir."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from amc.paths import config_dir, write_private_json

HOST_FILE = "host.json"


def device_ws_url(relay_url: str) -> str:
    """Turn ``https://relay.example`` (or ``http://127.0.0.1:8790``) into the device WebSocket URL."""
    parts = urlsplit(relay_url.strip())
    scheme = {"https": "wss", "http": "ws"}.get(parts.scheme, parts.scheme)
    if scheme not in {"ws", "wss"}:
        raise ValueError("relay URL must start with http://, https://, ws:// or wss://")
    path = parts.path.rstrip("/")
    if not path.endswith("/devices/connect"):
        path = path + "/devices/connect"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def relay_http_url(relay_url: str) -> str:
    """Inverse of :func:`device_ws_url`: the relay's base HTTP(S) URL."""
    parts = urlsplit(relay_url.strip())
    scheme = {"wss": "https", "ws": "http"}.get(parts.scheme, parts.scheme)
    path = parts.path.rstrip("/")
    for suffix in ("/devices/connect", "/mcp"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    return urlunsplit((scheme, parts.netloc, path, "", ""))


@dataclass
class HostConfig:
    relay_url: str
    device_id: str
    device_token: str
    name: str = ""
    roots: list[str] = field(default_factory=lambda: [str(Path.home())])
    executor: str = "builtin"
    stdio_command: str = ""
    stdio_args: list[str] = field(default_factory=list)
    stdio_env: dict[str, str] = field(default_factory=dict)
    env_passthrough: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)  # extra never-accessible paths (defaults always apply)
    cwd: str = field(default_factory=lambda: str(Path.home()))
    reconnect_base_delay: float = 1.0
    reconnect_max_delay: float = 30.0

    @staticmethod
    def default_path() -> Path:
        return config_dir() / HOST_FILE

    @classmethod
    def load(cls, path: Path | None = None) -> HostConfig:
        path = path or cls.default_path()
        if not path.exists():
            raise FileNotFoundError(
                f"{path} not found — pair this machine first: amc host pair <relay-url> <code>"
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})

    def save(self, path: Path | None = None) -> Path:
        path = path or self.default_path()
        write_private_json(path, json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n")
        return path

    @property
    def ws_url(self) -> str:
        return device_ws_url(self.relay_url)
