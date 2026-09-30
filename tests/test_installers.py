"""Installer script checks: syntax, lint (when available) and a dry run with fake tools."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "installers" / "install.sh"
INSTALL_PS1 = ROOT / "installers" / "install.ps1"
DEFAULT_SOURCE = "amc-commander @ https://github.com/inflow-digital/amc/archive/refs/heads/main.zip"

SH = shutil.which("sh")
# install.sh tests need a POSIX sh; the install.ps1 checks run everywhere (pwsh one only when installed).
posix_sh = pytest.mark.skipif(SH is None or os.name == "nt", reason="needs a POSIX sh")


@posix_sh
def test_install_sh_syntax():
    subprocess.run([SH, "-n", str(INSTALL_SH)], check=True)


@posix_sh
@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_install_sh_shellcheck():
    subprocess.run(["shellcheck", "-s", "sh", str(INSTALL_SH)], check=True)


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
def test_install_ps1_parses():
    script = (
        "$errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{INSTALL_PS1}', [ref]$null, [ref]$errors)"
        " | Out-Null; if ($errors.Count) { $errors; exit 1 }"
    )
    subprocess.run(["pwsh", "-NoProfile", "-Command", script], check=True)


def test_install_ps1_has_no_hard_exit_in_iex_path():
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "if ($RunningAsFile) { exit 1 }" in text
    assert "AMC_PAIR_URL" in text and "AMC_PAIR_CODE" in text and "AMC_SOURCE" in text


def _write_tool(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_env(tmp_path):
    home = tmp_path / "home"
    fakebin = tmp_path / "fakebin"
    toolbin = tmp_path / "toolbin"
    for d in (home, fakebin, toolbin):
        d.mkdir()
    log = tmp_path / "calls.log"
    # fake uv: logs its arguments; `uv tool install` drops a fake amc into the tool bin dir
    _write_tool(
        fakebin / "uv",
        f"""echo "uv $*" >> "{log}"
if [ "$1 $2" = "tool dir" ]; then echo "{toolbin}"; exit 0; fi
if [ "$1 $2" = "tool install" ]; then
  printf '#!/bin/sh\\necho "amc $*" >> "{log}"\\nexit ${{FAKE_AMC_EXIT:-0}}\\n' > "{toolbin}/amc"
  chmod +x "{toolbin}/amc"
fi
exit 0
""",
    )
    system_path = os.pathsep.join(p for p in ("/usr/bin", "/bin") if Path(p).is_dir())
    env = {
        "HOME": str(home),
        "PATH": f"{fakebin}{os.pathsep}{system_path}",
        "LANG": "C",
    }
    return env, log, toolbin


def _run(env, *args, extra_env=None):
    return subprocess.run(
        [SH, str(INSTALL_SH), *args],
        env={**env, **(extra_env or {})},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _calls(log: Path) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


@posix_sh
def test_default_mode_runs_amc_up(fake_env):
    env, log, toolbin = fake_env
    result = _run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(log)
    assert f"uv tool install --force --python 3.12 {DEFAULT_SOURCE}" in calls
    assert calls[-1] == "amc up"
    assert "uv tool update-shell" in result.stdout  # toolbin was not on the user's PATH


@posix_sh
def test_pair_mode(fake_env):
    env, log, _ = fake_env
    result = _run(env, "--pair", "https://relay.example.com", "ABCD-1234", "--name", "laptop")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(log)
    amc_calls = [c for c in calls if c.startswith("amc ")]
    assert amc_calls == [
        "amc host pair https://relay.example.com ABCD-1234 --name laptop",
        "amc service install host",
    ]


@posix_sh
def test_pair_mode_from_env_without_name(fake_env):
    env, log, _ = fake_env
    extra = {"AMC_PAIR_URL": "https://relay.example.com", "AMC_PAIR_CODE": "XY-9"}
    result = _run(env, extra_env=extra)
    assert result.returncode == 0, result.stdout + result.stderr
    amc_calls = [c for c in _calls(log) if c.startswith("amc ")]
    assert amc_calls == ["amc host pair https://relay.example.com XY-9", "amc service install host"]


@posix_sh
def test_no_start_and_source_override(fake_env, tmp_path):
    env, log, _ = fake_env
    wheel = str(tmp_path / "amc_commander-0.9.0-py3-none-any.whl")
    result = _run(env, "--no-start", extra_env={"AMC_SOURCE": wheel})
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(log)
    assert f"uv tool install --force --python 3.12 {wheel}" in calls
    assert not [c for c in calls if c.startswith("amc ")]


@posix_sh
def test_failure_exits_nonzero_with_fix(fake_env):
    env, _, _ = fake_env
    result = _run(env, extra_env={"FAKE_AMC_EXIT": "3"})
    assert result.returncode != 0
    assert "amc doctor" in result.stderr


@posix_sh
def test_bad_arguments(fake_env):
    env, _, _ = fake_env
    result = _run(env, "--pair", "https://relay.example.com")
    assert result.returncode != 0 and "--pair needs" in result.stderr
    result = _run(env, "--bogus")
    assert result.returncode != 0 and "unknown option" in result.stderr
