"""Per-user background services for the AMC relay and host.

``amc service install relay|host`` registers a service that starts at login/boot and
restarts on failure:

* Linux   -> systemd ``--user`` unit (``~/.config/systemd/user``); without systemd
             (containers, WSL without systemd) a detached background process + pid file.
* macOS   -> launchd LaunchAgent (``~/Library/LaunchAgents``).
* Windows -> Scheduled Task "at logon" running a generated ``.cmd`` wrapper.

Every external command goes through a :class:`Runner` so tests can check the rendered
files and command sequences without touching the real system.

When ``AMC_HOME`` is set, it is passed to the service and the service name gets a short
suffix derived from it, so a sandbox instance never replaces the default one.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import plistlib
import shutil
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from amc.paths import config_dir

ROLES: dict[str, tuple[str, ...]] = {
    "relay": ("relay", "serve"),
    "host": ("host", "run"),
}

RUNNING = "running"
STOPPED = "stopped"
NOT_INSTALLED = "not installed"


class ServiceError(RuntimeError):
    """A service operation failed; the message says what to do about it."""


# --------------------------------------------------------------------------- runner


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def output(self) -> str:
        return (self.stdout + ("\n" if self.stdout and self.stderr else "") + self.stderr).strip()


class Runner(Protocol):
    def run(self, argv: Sequence[str]) -> CommandResult: ...

    def which(self, name: str) -> str | None: ...

    def spawn(self, argv: Sequence[str], env: Mapping[str, str], log_file: Path) -> int: ...

    def pid_alive(self, pid: int) -> bool: ...

    def terminate(self, pid: int) -> None: ...


class SystemRunner:
    """The real thing: runs commands on this machine."""

    timeout = 120.0

    def run(self, argv: Sequence[str]) -> CommandResult:
        try:
            proc = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return CommandResult(127, "", f"{argv[0]}: command not found")
        except subprocess.TimeoutExpired:
            return CommandResult(124, "", f"{argv[0]}: timed out after {self.timeout:.0f}s")
        return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def spawn(self, argv: Sequence[str], env: Mapping[str, str], log_file: Path) -> int:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "ab") as log:
            proc = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env={**os.environ, **env},
                cwd=str(Path.home()),
                start_new_session=True,
                close_fds=True,
            )
        return proc.pid

    def pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def terminate(self, pid: int) -> None:
        try:
            os.killpg(pid, signal.SIGTERM)  # started with start_new_session -> own group
        except (ProcessLookupError, PermissionError, OSError, AttributeError):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass


_DEFAULT_RUNNER: Runner = SystemRunner()


def _runner(runner: Runner | None) -> Runner:
    return runner if runner is not None else _DEFAULT_RUNNER


# --------------------------------------------------------------------------- naming / paths


def _check_role(role: str) -> str:
    if role not in ROLES:
        raise ValueError(f"unknown service role {role!r}; expected one of: {', '.join(ROLES)}")
    return role


def _amc_home() -> str | None:
    value = os.environ.get("AMC_HOME", "").strip()
    return str(Path(value).expanduser().resolve()) if value else None


def instance_suffix() -> str:
    """Empty for the default instance; a short stable hash of ``AMC_HOME`` otherwise."""
    home = _amc_home()
    if not home:
        return ""
    return hashlib.sha256(home.encode("utf-8")).hexdigest()[:8]


def service_name(role: str, platform: str | None = None) -> str:
    """Platform-specific service identifier (unit name, launchd label or task name)."""
    _check_role(role)
    platform = platform or sys.platform
    suffix = instance_suffix()
    if platform == "darwin":
        return f"com.amc.{role}" + (f".{suffix}" if suffix else "")
    if platform == "win32":
        return f"AMC {role.capitalize()}" + (f" {suffix}" if suffix else "")
    return f"amc-{role}" + (f"-{suffix}" if suffix else "") + ".service"


def log_path(role: str) -> Path:
    return config_dir() / "logs" / f"{_check_role(role)}.log"


def pid_path(role: str) -> Path:
    return config_dir() / "run" / f"{_check_role(role)}.pid"


def systemd_unit_path(role: str) -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / service_name(role, "linux")


def launchd_plist_path(role: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{service_name(role, 'darwin')}.plist"


def windows_wrapper_path(role: str) -> Path:
    return config_dir() / "service" / f"amc-{_check_role(role)}.cmd"


def resolve_executable(amc_executable: str | None = None) -> list[str]:
    """The command prefix that runs ``amc``: explicit path, ``amc`` on PATH, or ``python -m amc``."""
    if amc_executable:
        return [amc_executable]
    found = shutil.which("amc")
    if found:
        return [os.path.abspath(found)]
    return [sys.executable, "-m", "amc"]


def service_command(role: str, amc_executable: str | None = None) -> list[str]:
    return [*resolve_executable(amc_executable), *ROLES[_check_role(role)]]


def service_env(env: Mapping[str, str] | None = None, platform: str | None = None) -> dict[str, str]:
    """Environment for the service: ``AMC_HOME`` pass-through, a useful PATH, caller extras."""
    platform = platform or sys.platform
    result: dict[str, str] = {}
    home = _amc_home()
    if home:
        result["AMC_HOME"] = home
    if platform != "win32":
        # Services start with a minimal PATH; give the host the tools the user has.
        extra = [str(Path.home() / ".local" / "bin")]
        if platform == "darwin":
            extra += ["/opt/homebrew/bin", "/usr/local/bin"]
        extra += (os.environ.get("PATH") or "").split(os.pathsep)
        extra += ["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        seen: list[str] = []
        for item in extra:
            if item and item not in seen:
                seen.append(item)
        result["PATH"] = os.pathsep.join(seen)
    if env:
        result.update(env)
    return result


# --------------------------------------------------------------------------- renderers


def _systemd_quote(arg: str, *, exec_line: bool = True) -> str:
    arg = arg.replace("%", "%%")
    if exec_line:  # ExecStart= expands $VAR; Environment= does not
        arg = arg.replace("$", "$$")
    if arg and not any(ch in arg for ch in " \t\"'\\;"):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_systemd_unit(role: str, argv: Sequence[str], env: Mapping[str, str], log: Path) -> str:
    lines = [
        "[Unit]",
        f"Description=AMC {role} (Agent MCP Commander)",
        "Wants=network-online.target",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={' '.join(_systemd_quote(a) for a in argv)}",
        "WorkingDirectory=%h",
    ]
    for key, value in env.items():
        lines.append(f"Environment={_systemd_quote(f'{key}={value}', exec_line=False)}")
    log_arg = str(log).replace("%", "%%")
    lines += [
        "Restart=always",
        "RestartSec=3",
        # exit status 3 = the relay rejected this device (removed / bad token): restarting cannot help
        "RestartPreventExitStatus=3",
        f"StandardOutput=append:{log_arg}",
        f"StandardError=append:{log_arg}",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def render_launchd_plist(label: str, argv: Sequence[str], env: Mapping[str, str], log: Path) -> bytes:
    data = {
        "Label": label,
        "ProgramArguments": list(argv),
        "RunAtLoad": True,
        # launchd cannot skip restarts for one exit code, so a device the relay rejected
        # (exit 3) retries at most once a minute instead of spinning.
        "KeepAlive": True,
        "ThrottleInterval": 60 if label.split(".")[2:3] == ["host"] else 3,
        "ProcessType": "Background",
        "WorkingDirectory": str(Path.home()),
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": dict(env),
    }
    return plistlib.dumps(data)


def _cmd_quote(arg: str) -> str:
    arg = arg.replace("%", "%%")
    if arg and not any(ch in arg for ch in ' \t&|<>^()"'):
        return arg
    return '"' + arg.replace('"', '""') + '"'


def render_windows_wrapper(title: str, argv: Sequence[str], env: Mapping[str, str], log: Path) -> str:
    """A ``.cmd`` file that runs the service, appends output to the log and restarts it."""
    lines = [
        "@echo off",
        "rem Generated by `amc service install`. Do not edit; re-run the install instead.",
        "chcp 65001 >nul",
        f"title {title} - background service (closing this window stops it until next logon)",
        "setlocal",
    ]
    for key, value in env.items():
        safe = value.replace("%", "%%")
        lines.append(f'set "{key}={safe}"')
    lines += [
        'cd /d "%USERPROFILE%"',
        ":loop",
        f"{' '.join(_cmd_quote(a) for a in argv)} >> {_cmd_quote(str(log))} 2>&1",
        "rem exit code 3 = the relay rejected this device; restarting cannot help",
        "if %errorlevel% equ 3 exit /b 3",
        "timeout /t 3 /nobreak >nul",
        "goto loop",
        "",
    ]
    return "\r\n".join(lines)


# --------------------------------------------------------------------------- helpers


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8", newline="")


def _must(result: CommandResult, what: str, fix: str = "") -> CommandResult:
    if not result.ok:
        detail = result.output() or f"exit code {result.returncode}"
        raise ServiceError(f"{what} failed: {detail}" + (f"\nFix: {fix}" if fix else ""))
    return result


def _gui_domain() -> str:
    getuid = getattr(os, "getuid", None)
    return f"gui/{getuid() if getuid else 0}"


def _read_pid(role: str) -> int | None:
    try:
        return int(pid_path(role).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _systemd_available(run: Runner) -> bool:
    if not run.which("systemctl"):
        return False
    return run.run(["systemctl", "--user", "show-environment"]).ok


def _stop_background(role: str, run: Runner) -> bool:
    pid = _read_pid(role)
    stopped = False
    if pid is not None and run.pid_alive(pid):
        run.terminate(pid)
        stopped = True
    try:
        pid_path(role).unlink()
    except FileNotFoundError:
        pass
    return stopped


# --------------------------------------------------------------------------- Linux


def _install_linux(role: str, argv: list[str], env: dict[str, str], log: Path, run: Runner) -> str:
    if not _systemd_available(run):
        _stop_background(role, run)
        pid = run.spawn(argv, env, log)
        path = pid_path(role)
        _write(path, f"{pid}\n")
        return (
            f"AMC {role} started in the background (pid {pid}); log: {log}\n"
            "Note: systemd --user is not available here (container or WSL without systemd), so it "
            "will NOT restart automatically after a reboot or crash. Run `amc service install "
            f"{role}` again after a restart (or enable systemd)."
        )
    _stop_background(role, run)  # a previous fallback process would hold the port
    unit = service_name(role, "linux")
    unit_path = systemd_unit_path(role)
    _write(unit_path, render_systemd_unit(role, argv, env, log))
    _must(run.run(["systemctl", "--user", "daemon-reload"]), "systemctl --user daemon-reload")
    _must(run.run(["systemctl", "--user", "enable", unit]), f"systemctl --user enable {unit}")
    _must(
        run.run(["systemctl", "--user", "restart", unit]),
        f"systemctl --user restart {unit}",
        f"check the log {log} and `journalctl --user -u {unit}`",
    )
    message = f"AMC {role} installed as systemd user service {unit} and started; log: {log}"
    user = getpass.getuser()
    linger = run.run(["loginctl", "enable-linger", user])
    if linger.ok:
        message += "\nIt starts at boot and keeps running after you log out (linger enabled)."
    else:
        message += (
            "\nWarning: could not enable linger, so the service stops when you log out. "
            f"To keep it running: sudo loginctl enable-linger {user}"
        )
    return message


def _uninstall_linux(role: str, run: Runner) -> str:
    unit = service_name(role, "linux")
    unit_path = systemd_unit_path(role)
    removed = []
    if unit_path.exists():
        run.run(["systemctl", "--user", "disable", "--now", unit])
        unit_path.unlink()
        run.run(["systemctl", "--user", "daemon-reload"])
        removed.append(f"systemd user service {unit}")
    had_pid = pid_path(role).exists()
    if _stop_background(role, run) or had_pid:
        removed.append("background process")
    if not removed:
        return f"AMC {role} service is not installed."
    return f"AMC {role}: removed {' and '.join(removed)} (config and identity kept)."


def _status_linux(role: str, run: Runner) -> tuple[str, str]:
    unit = service_name(role, "linux")
    if systemd_unit_path(role).exists():
        result = run.run(["systemctl", "--user", "is-active", unit])
        state = result.stdout.strip() or result.output() or "unknown"
        if state == "active":
            return RUNNING, f"systemd user service {unit}"
        return STOPPED, f"systemd user service {unit} is {state}; see {log_path(role)}"
    pid = _read_pid(role)
    if pid is not None:
        if run.pid_alive(pid):
            return RUNNING, f"background process pid {pid} (no systemd; not restarted after reboot)"
        return STOPPED, f"background process pid {pid} is gone; see {log_path(role)}"
    return NOT_INSTALLED, f"run: amc service install {role}"


# --------------------------------------------------------------------------- macOS


def _install_darwin(role: str, argv: list[str], env: dict[str, str], log: Path, run: Runner) -> str:
    label = service_name(role, "darwin")
    plist = launchd_plist_path(role)
    _write(plist, render_launchd_plist(label, argv, env, log))
    domain = _gui_domain()
    run.run(["launchctl", "bootout", domain, str(plist)])  # not loaded yet -> error, ignored
    first = run.run(["launchctl", "bootstrap", domain, str(plist)])
    if not first.ok:
        # bootout is asynchronous; a quick retry after enabling usually succeeds.
        run.run(["launchctl", "enable", f"{domain}/{label}"])
        _must(
            run.run(["launchctl", "bootstrap", domain, str(plist)]),
            f"launchctl bootstrap {domain} {plist}",
            f"run `amc service install {role}` again; details in {log}",
        )
    return (
        f"AMC {role} installed as LaunchAgent {label} and started; it starts at login and "
        f"restarts if it stops. Log: {log}"
    )


def _uninstall_darwin(role: str, run: Runner) -> str:
    plist = launchd_plist_path(role)
    if not plist.exists():
        return f"AMC {role} service is not installed."
    run.run(["launchctl", "bootout", _gui_domain(), str(plist)])
    plist.unlink()
    return f"AMC {role}: removed LaunchAgent {service_name(role, 'darwin')} (config and identity kept)."


def _status_darwin(role: str, run: Runner) -> tuple[str, str]:
    label = service_name(role, "darwin")
    if not launchd_plist_path(role).exists():
        return NOT_INSTALLED, f"run: amc service install {role}"
    result = run.run(["launchctl", "print", f"{_gui_domain()}/{label}"])
    if not result.ok:
        return STOPPED, f"LaunchAgent {label} is not loaded; run: amc service install {role}"
    state, pid = "", ""
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if line.startswith("state = ") and not state:
            state = line.split("=", 1)[1].strip()
        elif line.startswith("pid = ") and not pid:
            pid = line.split("=", 1)[1].strip()
    if state == "running":
        return RUNNING, f"LaunchAgent {label}" + (f", pid {pid}" if pid else "")
    return STOPPED, f"LaunchAgent {label} is {state or 'not running'}; see {log_path(role)}"


# --------------------------------------------------------------------------- Windows


def _install_windows(role: str, argv: list[str], env: dict[str, str], log: Path, run: Runner) -> str:
    task = service_name(role, "win32")
    wrapper = windows_wrapper_path(role)
    run.run(["schtasks", "/End", "/TN", task])  # restart semantics; ignore "not running"
    _write(wrapper, render_windows_wrapper(task, argv, env, log))
    _must(
        run.run(
            [
                "schtasks",
                "/Create",
                "/SC",
                "ONLOGON",
                "/TN",
                task,
                "/TR",
                f'"{wrapper}"',
                "/RL",
                "LIMITED",
                "/F",
            ]
        ),
        f'schtasks /Create /TN "{task}"',
        "run the command from a normal (non-restricted) PowerShell for your own user",
    )
    _must(run.run(["schtasks", "/Run", "/TN", task]), f'schtasks /Run /TN "{task}"')
    return (
        f'AMC {role} installed as Scheduled Task "{task}" (runs at logon) and started; log: {log}\n'
        f'A console window titled "{task}" hosts it: minimize it, do not close it.'
    )


def _uninstall_windows(role: str, run: Runner) -> str:
    task = service_name(role, "win32")
    wrapper = windows_wrapper_path(role)
    exists = run.run(["schtasks", "/Query", "/TN", task]).ok
    if not exists and not wrapper.exists():
        return f"AMC {role} service is not installed."
    run.run(["schtasks", "/End", "/TN", task])
    if exists:
        run.run(["schtasks", "/Delete", "/TN", task, "/F"])
    if wrapper.exists():
        wrapper.unlink()
    return f'AMC {role}: removed Scheduled Task "{task}" (config and identity kept).'


def _status_windows(role: str, run: Runner) -> tuple[str, str]:
    task = service_name(role, "win32")
    result = run.run(["schtasks", "/Query", "/TN", task, "/FO", "LIST"])
    if not result.ok:
        return NOT_INSTALLED, f"run: amc service install {role}"
    # schtasks output is localized; ask PowerShell for the (language-independent) state enum.
    state = run.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     f"(Get-ScheduledTask -TaskName '{task}').State"])
    value = state.stdout.strip() if state.ok else ""
    if not value:  # fall back to English schtasks text
        for raw in result.stdout.splitlines():
            key, _, text = raw.partition(":")
            if key.strip().lower() == "status":
                value = text.strip()
    if value.lower() == "running":
        return RUNNING, f'Scheduled Task "{task}"'
    return STOPPED, f'Scheduled Task "{task}" is {value or "not running"}; see {log_path(role)}'


# --------------------------------------------------------------------------- public API


def _platform_key() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def install(
    role: str,
    *,
    amc_executable: str | None = None,
    env: dict[str, str] | None = None,
    runner: Runner | None = None,
) -> str:
    """Register, enable at login/boot and (re)start the service. Idempotent."""
    _check_role(role)
    run = _runner(runner)
    argv = service_command(role, amc_executable)
    log = log_path(role)
    log.parent.mkdir(parents=True, exist_ok=True)
    platform = _platform_key()
    environment = service_env(env, sys.platform)
    if platform == "windows":
        return _install_windows(role, argv, environment, log, run)
    if platform == "darwin":
        return _install_darwin(role, argv, environment, log, run)
    return _install_linux(role, argv, environment, log, run)


def uninstall(role: str, *, runner: Runner | None = None) -> str:
    """Stop and remove the service; configuration and identity are kept."""
    _check_role(role)
    run = _runner(runner)
    platform = _platform_key()
    if platform == "windows":
        return _uninstall_windows(role, run)
    if platform == "darwin":
        return _uninstall_darwin(role, run)
    return _uninstall_linux(role, run)


def state(role: str, *, runner: Runner | None = None) -> tuple[str, str]:
    """``(state, detail)`` where state is ``running``, ``stopped`` or ``not installed``."""
    _check_role(role)
    run = _runner(runner)
    platform = _platform_key()
    if platform == "windows":
        return _status_windows(role, run)
    if platform == "darwin":
        return _status_darwin(role, run)
    return _status_linux(role, run)


def status(role: str, *, runner: Runner | None = None) -> str:
    """Human-readable status line starting with ``running``, ``stopped`` or ``not installed``."""
    word, detail = state(role, runner=runner)
    return f"{word} ({detail})" if detail else word
