"""Environment isolation for executors and the commands they run.

The executor never inherits the host's full environment: only an allowlist of
variables needed for a normal shell, plus names the owner explicitly passes
through. Host credentials (``AMC_*``) are never passed on.
"""

from __future__ import annotations

import os

_ALLOW = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TZ",
    "TMPDIR", "TEMP", "TMP", "EDITOR", "PAGER", "COLORTERM",
    "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "WINDIR", "USERPROFILE", "USERNAME",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "PROGRAMW6432", "COMMONPROGRAMFILES", "PUBLIC", "HOMEDRIVE", "HOMEPATH",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
    "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
    "DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS",
    "NVM_DIR", "NODE_PATH",
}
_ALLOW_PREFIXES = ("LC_",)


def safe_environment(
    passthrough: list[str] | tuple[str, ...] = (),
    extra: dict[str, str] | None = None,
    source: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return the allowlisted subset of ``source`` (default: ``os.environ``).

    ``AMC_*`` variables are never passed on, even when named in ``passthrough``.
    """
    env_source = dict(os.environ if source is None else source)
    wanted = {name.upper() for name in passthrough}
    result: dict[str, str] = {}
    for key, value in env_source.items():
        upper = key.upper()
        if upper.startswith("AMC_"):
            continue
        if upper in wanted or upper in _ALLOW or upper.startswith(_ALLOW_PREFIXES):
            result[key] = value
    result.update(extra or {})
    return result
