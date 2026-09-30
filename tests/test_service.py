"""Service installer tests: rendered files and command sequences, never real services."""

from __future__ import annotations

import plistlib
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from amc import service
from amc.service import CommandResult

AMC = "/opt/amc/bin/amc"

# The systemd / launchd tests fake sys.platform but still run on the real host, whose
# os.pathsep and path flavour (backslashes, drive letters) differ on Windows.
posix_host_only = pytest.mark.skipif(
    sys.platform == "win32", reason="simulates a POSIX service manager; needs POSIX paths and os.pathsep"
)


class FakeRunner:
    def __init__(self, *, systemctl: bool = True, results: dict[str, CommandResult] | None = None):
        self.calls: list[list[str]] = []
        self.spawned: list[tuple[list[str], dict[str, str], Path]] = []
        self.terminated: list[int] = []
        self.alive: set[int] = set()
        self.systemctl = systemctl
        self.results = results or {}

    def run(self, argv: Sequence[str]) -> CommandResult:
        argv = list(argv)
        self.calls.append(argv)
        key = " ".join(argv)
        for prefix, result in self.results.items():
            if key.startswith(prefix):
                return result
        return CommandResult(0, "", "")

    def which(self, name: str) -> str | None:
        if name == "systemctl" and self.systemctl:
            return "/usr/bin/systemctl"
        return None

    def spawn(self, argv: Sequence[str], env: Mapping[str, str], log_file: Path) -> int:
        self.spawned.append((list(argv), dict(env), log_file))
        self.alive.add(4242)
        return 4242

    def pid_alive(self, pid: int) -> bool:
        return pid in self.alive

    def terminate(self, pid: int) -> None:
        self.terminated.append(pid)
        self.alive.discard(pid)


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home" / "alice"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("USER", "alice")
    monkeypatch.setenv("LOGNAME", "alice")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("AMC_HOME", raising=False)
    monkeypatch.setattr(service.Path, "home", classmethod(lambda cls: home))
    return home


def _platform(monkeypatch, value: str) -> None:
    monkeypatch.setattr(sys, "platform", value)


# ------------------------------------------------------------------ common


def test_unknown_role_rejected(home):
    with pytest.raises(ValueError):
        service.install("nope", runner=FakeRunner())
    with pytest.raises(ValueError):
        service.log_path("nope")


def test_log_path_in_config_dir(home, monkeypatch):
    _platform(monkeypatch, "linux")
    assert service.log_path("relay") == home / ".config" / "amc" / "logs" / "relay.log"


def test_resolve_executable_order(monkeypatch):
    assert service.resolve_executable("/x/amc") == ["/x/amc"]
    monkeypatch.setattr(service.shutil, "which", lambda name: "/usr/local/bin/amc")
    assert Path(service.resolve_executable()[0]) == Path("/usr/local/bin/amc").resolve() or \
        service.resolve_executable() == ["/usr/local/bin/amc"]
    monkeypatch.setattr(service.shutil, "which", lambda name: None)
    assert service.resolve_executable() == [sys.executable, "-m", "amc"]
    assert service.service_command("host") == [sys.executable, "-m", "amc", "host", "run"]


def test_default_names(home):
    assert service.service_name("relay", "linux") == "amc-relay.service"
    assert service.service_name("host", "linux") == "amc-host.service"
    assert service.service_name("relay", "darwin") == "com.amc.relay"
    assert service.service_name("host", "darwin") == "com.amc.host"
    assert service.service_name("relay", "win32") == "AMC Relay"
    assert service.service_name("host", "win32") == "AMC Host"


def test_amc_home_gets_suffix_and_passthrough(home, monkeypatch, tmp_path):
    sandbox = tmp_path / "sandbox"
    monkeypatch.setenv("AMC_HOME", str(sandbox))
    suffix = service.instance_suffix()
    assert len(suffix) == 8 and suffix == service.instance_suffix()
    assert service.service_name("relay", "linux") == f"amc-relay-{suffix}.service"
    assert service.service_name("relay", "darwin") == f"com.amc.relay.{suffix}"
    assert service.service_name("relay", "win32") == f"AMC Relay {suffix}"
    assert service.service_env(platform="linux")["AMC_HOME"] == str(sandbox.resolve())

    monkeypatch.setenv("AMC_HOME", str(tmp_path / "other"))
    assert service.instance_suffix() != suffix


@posix_host_only
def test_service_env_path(home, monkeypatch):
    linux = service.service_env(platform="linux")["PATH"].split(":")
    assert linux[0] == str(home / ".local" / "bin")
    assert "/usr/bin" in linux and linux.count("/usr/bin") == 1
    mac = service.service_env(platform="darwin")["PATH"].split(":")
    assert "/opt/homebrew/bin" in mac and "/usr/local/bin" in mac
    win = service.service_env({"X": "1"}, platform="win32")
    assert win == {"X": "1"}


# ------------------------------------------------------------------ Linux / systemd


@posix_host_only
def test_linux_install_systemd(home, monkeypatch):
    _platform(monkeypatch, "linux")
    runner = FakeRunner()
    message = service.install("relay", amc_executable=AMC, env={"FOO": "bar baz"}, runner=runner)

    unit = home / ".config" / "systemd" / "user" / "amc-relay.service"
    text = unit.read_text()
    log = home / ".config" / "amc" / "logs" / "relay.log"
    assert f"ExecStart={AMC} relay serve" in text
    assert "Restart=always" in text and "RestartSec=3" in text
    assert f"StandardOutput=append:{log}" in text
    assert f"StandardError=append:{log}" in text
    assert 'Environment="FOO=bar baz"' in text
    assert "WantedBy=default.target" in text
    assert log.parent.is_dir()

    assert runner.calls == [
        ["systemctl", "--user", "show-environment"],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "amc-relay.service"],
        ["systemctl", "--user", "restart", "amc-relay.service"],
        ["loginctl", "enable-linger", "alice"],
    ]
    assert "amc-relay.service" in message and "linger enabled" in message

    # idempotent: a second install rewrites the same unit and restarts it
    runner2 = FakeRunner()
    service.install("relay", amc_executable=AMC, env={"FOO": "bar baz"}, runner=runner2)
    assert unit.read_text() == text
    assert runner2.calls == runner.calls


@posix_host_only
def test_linux_install_quotes_paths_with_spaces(home, monkeypatch):
    _platform(monkeypatch, "linux")
    service.install("host", amc_executable="/home/alice/my tools/amc", runner=FakeRunner())
    text = (home / ".config" / "systemd" / "user" / "amc-host.service").read_text()
    assert 'ExecStart="/home/alice/my tools/amc" host run' in text


@posix_host_only
def test_linux_linger_failure_warns(home, monkeypatch):
    _platform(monkeypatch, "linux")
    runner = FakeRunner(results={"loginctl": CommandResult(1, "", "access denied")})
    message = service.install("host", amc_executable=AMC, runner=runner)
    assert "stops when you log out" in message
    assert "sudo loginctl enable-linger alice" in message


@posix_host_only
def test_linux_restart_failure_raises(home, monkeypatch):
    _platform(monkeypatch, "linux")
    runner = FakeRunner(results={"systemctl --user restart": CommandResult(1, "", "boom")})
    with pytest.raises(service.ServiceError, match="boom"):
        service.install("host", amc_executable=AMC, runner=runner)


@posix_host_only
def test_linux_amc_home_instance(home, monkeypatch, tmp_path):
    _platform(monkeypatch, "linux")
    sandbox = tmp_path / "sandbox"
    monkeypatch.setenv("AMC_HOME", str(sandbox))
    runner = FakeRunner()
    service.install("relay", amc_executable=AMC, runner=runner)
    name = service.service_name("relay")
    assert name != "amc-relay.service"
    text = (home / ".config" / "systemd" / "user" / name).read_text()
    assert f"Environment=AMC_HOME={sandbox.resolve()}" in text
    assert f"StandardOutput=append:{sandbox / 'logs' / 'relay.log'}" in text
    assert not (home / ".config" / "systemd" / "user" / "amc-relay.service").exists()


@posix_host_only
def test_linux_status(home, monkeypatch):
    _platform(monkeypatch, "linux")
    assert service.status("relay", runner=FakeRunner()).startswith("not installed")
    service.install("relay", amc_executable=AMC, runner=FakeRunner())
    running = FakeRunner(results={"systemctl --user is-active": CommandResult(0, "active\n")})
    assert service.status("relay", runner=running).startswith("running")
    stopped = FakeRunner(results={"systemctl --user is-active": CommandResult(3, "inactive\n")})
    assert service.state("relay", runner=stopped)[0] == "stopped"


@posix_host_only
def test_linux_uninstall(home, monkeypatch):
    _platform(monkeypatch, "linux")
    assert "not installed" in service.uninstall("relay", runner=FakeRunner())
    service.install("relay", amc_executable=AMC, runner=FakeRunner())
    runner = FakeRunner()
    message = service.uninstall("relay", runner=runner)
    assert ["systemctl", "--user", "disable", "--now", "amc-relay.service"] in runner.calls
    assert runner.calls[-1] == ["systemctl", "--user", "daemon-reload"]
    assert not (home / ".config" / "systemd" / "user" / "amc-relay.service").exists()
    assert "removed" in message


@posix_host_only
def test_linux_fallback_without_systemd(home, monkeypatch):
    _platform(monkeypatch, "linux")
    runner = FakeRunner(systemctl=False)
    message = service.install("host", amc_executable=AMC, runner=runner)
    argv, env, log = runner.spawned[0]
    assert argv == [AMC, "host", "run"]
    assert "PATH" in env
    assert log == home / ".config" / "amc" / "logs" / "host.log"
    assert (home / ".config" / "amc" / "run" / "host.pid").read_text().strip() == "4242"
    assert "systemd --user is not available" in message
    assert runner.calls == []  # no systemctl, no loginctl

    assert service.status("host", runner=runner).startswith("running")

    # re-install stops the old process first
    service.install("host", amc_executable=AMC, runner=runner)
    assert runner.terminated == [4242]

    message = service.uninstall("host", runner=runner)
    assert "background process" in message
    assert not (home / ".config" / "amc" / "run" / "host.pid").exists()
    assert service.status("host", runner=runner).startswith("not installed")


@posix_host_only
def test_linux_fallback_systemctl_without_bus(home, monkeypatch):
    _platform(monkeypatch, "linux")
    runner = FakeRunner(results={"systemctl --user show-environment": CommandResult(1, "", "no bus")})
    service.install("relay", amc_executable=AMC, runner=runner)
    assert runner.spawned and runner.calls == [["systemctl", "--user", "show-environment"]]


# ------------------------------------------------------------------ macOS / launchd


@posix_host_only
def test_darwin_install(home, monkeypatch):
    _platform(monkeypatch, "darwin")
    monkeypatch.setattr(service.os, "getuid", lambda: 501, raising=False)
    runner = FakeRunner()
    message = service.install("relay", amc_executable=AMC, runner=runner)

    plist_path = home / "Library" / "LaunchAgents" / "com.amc.relay.plist"
    data = plistlib.loads(plist_path.read_bytes())
    log = home / "Library" / "Application Support" / "amc" / "logs" / "relay.log"
    assert data["Label"] == "com.amc.relay"
    assert data["ProgramArguments"] == [AMC, "relay", "serve"]
    assert data["RunAtLoad"] is True and data["KeepAlive"] is True
    assert data["StandardOutPath"] == str(log) and data["StandardErrorPath"] == str(log)
    path = data["EnvironmentVariables"]["PATH"].split(":")
    assert {"/opt/homebrew/bin", "/usr/local/bin", str(home / ".local" / "bin")} <= set(path)

    assert runner.calls == [
        ["launchctl", "bootout", "gui/501", str(plist_path)],
        ["launchctl", "bootstrap", "gui/501", str(plist_path)],
    ]
    assert "com.amc.relay" in message


@posix_host_only
def test_darwin_bootstrap_retry(home, monkeypatch):
    _platform(monkeypatch, "darwin")
    monkeypatch.setattr(service.os, "getuid", lambda: 501, raising=False)

    class Flaky(FakeRunner):
        attempts = 0

        def run(self, argv):
            result = super().run(argv)
            if argv[:2] == ["launchctl", "bootstrap"]:
                Flaky.attempts += 1
                if Flaky.attempts == 1:
                    return CommandResult(5, "", "Bootstrap failed: 5: Input/output error")
            return result

    runner = Flaky()
    service.install("host", amc_executable=AMC, runner=runner)
    assert ["launchctl", "enable", "gui/501/com.amc.host"] in runner.calls
    assert Flaky.attempts == 2


@posix_host_only
def test_darwin_status_and_uninstall(home, monkeypatch):
    _platform(monkeypatch, "darwin")
    monkeypatch.setattr(service.os, "getuid", lambda: 501, raising=False)
    assert service.status("host", runner=FakeRunner()).startswith("not installed")
    service.install("host", amc_executable=AMC, runner=FakeRunner())
    printed = "com.amc.host = {\n\tstate = running\n\tpid = 777\n}\n"
    runner = FakeRunner(results={"launchctl print gui/501/com.amc.host": CommandResult(0, printed)})
    assert service.status("host", runner=runner) == "running (LaunchAgent com.amc.host, pid 777)"
    not_loaded = FakeRunner(results={"launchctl print": CommandResult(113, "", "not found")})
    assert service.state("host", runner=not_loaded)[0] == "stopped"

    runner = FakeRunner()
    service.uninstall("host", runner=runner)
    plist_path = home / "Library" / "LaunchAgents" / "com.amc.host.plist"
    assert runner.calls == [["launchctl", "bootout", "gui/501", str(plist_path)]]
    assert not plist_path.exists()


# ------------------------------------------------------------------ Windows / schtasks


def test_windows_install(home, monkeypatch):
    _platform(monkeypatch, "win32")
    runner = FakeRunner()
    exe = r"C:\Users\alice\.local\bin\amc.exe"
    message = service.install("host", amc_executable=exe, env={"PCT": "100%"}, runner=runner)

    cfg = home / "AppData" / "Roaming" / "amc"
    wrapper = cfg / "service" / "amc-host.cmd"
    text = wrapper.read_bytes().decode("utf-8")
    log = cfg / "logs" / "host.log"
    assert text.startswith("@echo off\r\n")
    assert f'{exe} host run >> "{log}" 2>&1' in text or f"{exe} host run >> {log} 2>&1" in text
    assert 'set "PCT=100%%"' in text
    assert ":loop" in text and "goto loop" in text
    assert "PATH=" not in text  # Windows keeps the user's own PATH

    assert runner.calls == [
        ["schtasks", "/End", "/TN", "AMC Host"],
        [
            "schtasks",
            "/Create",
            "/SC",
            "ONLOGON",
            "/TN",
            "AMC Host",
            "/TR",
            f'"{wrapper}"',
            "/RL",
            "LIMITED",
            "/F",
        ],
        ["schtasks", "/Run", "/TN", "AMC Host"],
    ]
    assert "AMC Host" in message


def test_windows_install_amc_home(home, monkeypatch, tmp_path):
    _platform(monkeypatch, "win32")
    sandbox = tmp_path / "sandbox"
    monkeypatch.setenv("AMC_HOME", str(sandbox))
    runner = FakeRunner()
    service.install("relay", amc_executable="amc.exe", runner=runner)
    name = service.service_name("relay")
    assert name.startswith("AMC Relay ")
    text = (sandbox / "service" / "amc-relay.cmd").read_text(encoding="utf-8")
    assert f'set "AMC_HOME={sandbox.resolve()}"' in text
    assert ["schtasks", "/Run", "/TN", name] in runner.calls


def test_windows_status_and_uninstall(home, monkeypatch):
    _platform(monkeypatch, "win32")
    missing = FakeRunner(results={"schtasks /Query": CommandResult(1, "", "ERROR: not found")})
    assert service.status("relay", runner=missing).startswith("not installed")
    assert "not installed" in service.uninstall("relay", runner=missing)

    listing = "Folder: \\\nHostName: PC\nTaskName: \\AMC Relay\nNext Run Time: N/A\nStatus: Running\n"
    running = FakeRunner(results={"schtasks /Query": CommandResult(0, listing)})
    assert service.status("relay", runner=running).startswith("running")
    assert running.calls[0] == ["schtasks", "/Query", "/TN", "AMC Relay", "/FO", "LIST"]
    # localized Windows: schtasks text is not English, the PowerShell State enum decides
    localized = FakeRunner(results={"schtasks /Query": CommandResult(0, "狀態: 執行中\n"),
                                    "powershell": CommandResult(0, "Running\r\n")})
    assert service.status("relay", runner=localized).startswith("running")
    ready = FakeRunner(results={"schtasks /Query": CommandResult(0, listing.replace("Running", "Ready"))})
    assert service.state("relay", runner=ready)[0] == "stopped"

    service.install("relay", amc_executable="amc.exe", runner=FakeRunner())
    runner = FakeRunner()
    service.uninstall("relay", runner=runner)
    assert ["schtasks", "/End", "/TN", "AMC Relay"] in runner.calls
    assert ["schtasks", "/Delete", "/TN", "AMC Relay", "/F"] in runner.calls
    assert not (home / "AppData" / "Roaming" / "amc" / "service" / "amc-relay.cmd").exists()
