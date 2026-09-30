"""Per-OS locations for AMC configuration and state.

``AMC_HOME`` overrides everything (tests, sandboxes, several instances on one machine).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def config_dir() -> Path:
    override = os.environ.get("AMC_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "amc"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "amc"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "amc"


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.chmod(path, 0o700)
    return path


def write_private_json(path: Path, text: str) -> None:
    """Atomically write a file readable only by the current user."""
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    if sys.platform != "win32":
        os.chmod(path, 0o600)
