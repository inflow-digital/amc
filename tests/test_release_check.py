"""Tests for scripts/release_check.py (the public release gate).

Sensitive-looking sample strings are assembled at runtime so this file itself
stays clean for the gate.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("release_check", ROOT / "scripts" / "release_check.py")
assert _spec and _spec.loader
rc = importlib.util.module_from_spec(_spec)
sys.modules["release_check"] = rc
_spec.loader.exec_module(rc)

DOT = "."
CGNAT = DOT.join(["100", "101", "7", "9"])
LAN = DOT.join(["192", "168", "44", "7"])
TEN = DOT.join(["10", "20", "30", "40"])
TSNET = "laptop" + DOT + "tail9f3a" + DOT + "ts" + DOT + "net"
HOME = "/" + "home" + "/" + "zed" + "/project"
MAC_HOME = "/" + "Users" + "/" + "zed" + "/project"
WIN_HOME = "C:" + "\\" + "Users" + "\\" + "zed" + "\\project"
AMC_KEY = "amc" + "k_" + "A" * 43
GH_TOKEN = "gh" + "p_" + "b" * 36
AWS_KEY = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
OPENAI_KEY = "s" + "k-proj-" + "c" * 40
PRIVATE_KEY = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"
EMAIL = "zed" + "@" + "corp-mail" + DOT + "io"


def rules(line: str, denylist=()) -> set[str]:
    return {rule for rule, _ in rc.scan_line(line, denylist)}


@pytest.mark.parametrize(
    ("line", "rule"),
    [
        (f"relay at http://{CGNAT}:8790", "cgnat-ip"),
        (f"ssh {LAN}", "private-ip"),
        (f"bind {TEN}", "private-ip"),
        (f"https://{TSNET}/mcp", "ts-net-host"),
        (f"cd {HOME}/src", "home-path"),
        (f"open {MAC_HOME}", "home-path"),
        (f"dir {WIN_HOME}", "home-path"),
        (f"Authorization: Bearer {AMC_KEY}", "amc-secret"),
        (f"token={GH_TOKEN}", "github-token"),
        (f"aws={AWS_KEY}", "aws-access-key"),
        (f"OPENAI_API_KEY={OPENAI_KEY}", "openai-key"),
        (PRIVATE_KEY, "private-key"),
        (f"Author: Zed <{EMAIL}>", "email"),
    ],
)
def test_detects(line, rule):
    assert rule in rules(line)


@pytest.mark.parametrize(
    "line",
    [
        "https://<machine>.<tailnet>.ts.net",
        "https://relay.example.ts.net",
        "/home/alice/my tools/amc and /Users/bob/x and /home/runner/work and /home/<name>/",
        r"C:\Users\alice\.local\bin\amc.exe",
        "the 100.64.0.0/10 range; 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16; router 192.168.1.1",
        "version 1.10.0.0.5 and 110.64.1.1",
        "amck_... (placeholder) and amcd_ + 'x' * 40",
        "Co-Authored-By: Bot <noreply@anthropic.com>, alice@example.com",
        "pip install amc-commander@git+https://github.com/example/amc",
        "task-1234567890abcdefghijkl is not a key",
    ],
)
def test_clean_lines(line):
    assert rules(line) == set()


def test_allow_marker_skips_line():
    assert rules(f"{LAN}  # release-check: allow") == set()


def test_denylist_is_case_insensitive_and_hides_the_term(tmp_path):
    deny = tmp_path / "deny.txt"
    deny.write_text("# comment\n\nAcmeCorp\n", encoding="utf-8")
    terms = rc.load_denylist(deny)
    assert terms == ["AcmeCorp"]
    hits = rc.scan_line("made by acmecorp ltd", terms)
    assert hits == [("denylist", "term #1")]


def test_secrets_are_redacted_in_output():
    ((_, excerpt),) = rc.scan_line(f"key {AMC_KEY}")
    assert excerpt != AMC_KEY and len(excerpt) < 12


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Alice")
    _git(repo, "config", "user.email", "alice@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    for name in rc.REQUIRED_FILES:
        (repo / name).write_text("ok\n", encoding="utf-8")
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def test_clean_repo_passes(repo, capsys):
    assert rc.main(["--root", str(repo)]) == 0
    assert "OK" in capsys.readouterr().err


def test_hit_in_history_is_found_after_removal(repo, capsys):
    (repo / "notes.md").write_text(f"relay: {CGNAT}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "notes")
    (repo / "notes.md").write_text("relay: relay.example.com\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "scrub")

    assert rc.main(["--root", str(repo), "--no-history"]) == 0
    capsys.readouterr()
    assert rc.main(["--root", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "history " in out and "notes.md:1" in out and "cgnat-ip" in out


def test_tree_hit_reports_file_and_line(repo, capsys):
    (repo / "cfg.txt").write_text(f"a\nb\nhost={LAN}\n", encoding="utf-8")  # untracked files count too
    assert rc.main(["--root", str(repo), "--no-history"]) == 1
    assert f"cfg.txt:3: [private-ip] {LAN}" in capsys.readouterr().out


def test_missing_required_file_fails(repo, capsys):
    (repo / "NOTICE").unlink()
    assert rc.main(["--root", str(repo), "--no-history"]) == 1
    assert "NOTICE: [missing-file]" in capsys.readouterr().out


def test_denylist_hits_commit_author(repo, tmp_path, capsys):
    deny = tmp_path / "deny.txt"
    deny.write_text("Alice\n", encoding="utf-8")
    assert rc.main(["--root", str(repo), "--denylist", str(deny)]) == 1
    assert "(header): [denylist] term #1" in capsys.readouterr().out


def test_unreadable_denylist_fails_closed(repo, tmp_path):
    assert rc.main(["--root", str(repo), "--denylist", str(tmp_path / "missing.txt")]) == 2


def test_not_a_repo_fails_closed(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    assert rc.main(["--root", str(tmp_path), "--no-history"]) == 2
