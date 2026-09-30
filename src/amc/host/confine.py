"""Host-side filesystem confinement for file tools.

The relay's path rules are lexical; the host is the only place that can resolve
``~``, relative paths and symlinks for real, so it re-checks every path argument
before any executor sees the call:

1. **Protected paths are always refused**, whatever the roots or the key: AMC's own
   config dir (device token, relay keys) and the credential files of agents/tools
   that AMC or the user configured (Claude/Codex configs, SSH, cloud CLIs...). A file
   grant must never be a way to steal a stronger credential or rewrite grants.
2. The resolved path must lie inside one of the device's roots.
3. If the caller's key is scoped to path prefixes, the *resolved* path must lie inside
   one of them (so a symlink inside the scope cannot point outside it).

Process tools (``start_process`` ...) are *not* confined by this — they are "admin"
and run as the user; grant them only to agents you trust with your account.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from amc.core import path_arguments
from amc.paths import config_dir

# Relative to the home directory.
DEFAULT_PROTECTED = (
    ".claude.json",
    ".claude/.credentials.json",
    ".codex",
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".config/gcloud",
    ".config/gh",
    ".kube",
    ".docker/config.json",
    ".netrc",
    ".git-credentials",
    ".pypirc",
    ".npmrc",
)


class ConfinementError(PermissionError):
    pass


def _resolve(raw: str, cwd: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not path.is_absolute():
        path = cwd / path
    # resolve(strict=False) follows symlinks for the existing part of the path.
    return path.resolve(strict=False)


# macOS and Windows file systems are case-insensitive by default: compare case-folded there,
# otherwise ".CONFIG/amc" would reach the protected ".config/amc".
_CASE_INSENSITIVE = sys.platform in {"darwin", "win32"}


def _key(path: Path) -> tuple[str, ...]:
    return tuple(part.casefold() for part in path.parts) if _CASE_INSENSITIVE else path.parts


def _inside(target: Path, bases: Iterable[Path]) -> bool:
    parts = _key(target)
    return any(parts[: len(base_parts)] == base_parts for base_parts in (_key(base) for base in bases))


def protected_paths(extra: Iterable[str] = ()) -> list[Path]:
    home = Path.home()
    paths = [config_dir()]
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        paths.append(Path(codex_home))
    paths += [home / rel for rel in DEFAULT_PROTECTED]
    paths += [Path(os.path.expanduser(p)) for p in extra]
    return [p.resolve(strict=False) for p in paths]


class Confinement:
    def __init__(self, roots: list[str], cwd: str | None = None, protected: Iterable[str] = ()) -> None:
        self.cwd = Path(cwd or Path.home()).expanduser()
        self.unrestricted = not roots or any(root.strip() in {"*", ""} for root in roots)
        self.roots = [] if self.unrestricted else [_resolve(root, self.cwd) for root in roots]
        self.protected = protected_paths(protected)

    def is_protected(self, path: str | Path) -> bool:
        return _inside(_resolve(str(path), self.cwd), self.protected)

    def allows(self, raw: str) -> bool:
        target = _resolve(raw, self.cwd)
        if _inside(target, self.protected):
            return False
        return self.unrestricted or _inside(target, self.roots)

    def check(self, arguments: dict[str, Any], scope: list[str] | None = None) -> None:
        scope_bases = [_resolve(prefix, self.cwd) for prefix in scope or []]
        for raw in path_arguments(arguments):
            target = _resolve(raw, self.cwd)
            if _inside(target, self.protected):
                raise ConfinementError(f"protected path (credentials/AMC config) is never accessible: {raw}")
            if not self.unrestricted and not _inside(target, self.roots):
                raise ConfinementError(f"path outside this device's allowed roots: {raw}")
            if scope_bases and not _inside(target, scope_bases):
                raise ConfinementError(f"path outside the paths this key may use: {raw}")
