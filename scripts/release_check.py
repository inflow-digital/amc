#!/usr/bin/env python3
"""Fail-closed public release gate for this repository.

Scans every file git knows about (tracked plus untracked-but-not-ignored) and the
full git history (``git log -p --all``, including author/committer headers) for
things that must never be published:

* tailnet / CGNAT addresses (100.64.0.0/10) and ``*.ts.net`` host names
* RFC 1918 private addresses (except a few obvious documentation examples)
* home directories such as ``/home/<name>/``, ``/Users/<name>/``, ``C:\\Users\\<name>``
  (placeholders like alice, bob, user, you, runner, example are fine)
* secret shapes: AMC keys/tokens, private keys, AWS / GitHub / OpenAI / Slack keys
* e-mail addresses (except example.* and noreply domains), including commit authors
* any term from ``--denylist PATH`` (one term per line, case-insensitive; keep the
  real list private and out of the repository)

It also checks that LICENSE, NOTICE and THIRD_PARTY.md exist.

A line containing ``release-check: allow`` is skipped (use sparingly, for
deliberate examples). Exit status: 0 clean, 1 findings, 2 the check could not run.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

ALLOW_MARKER = "release-check: allow"
REQUIRED_FILES = ("LICENSE", "NOTICE", "THIRD_PARTY.md")

PLACEHOLDER_USERS = frozenset(
    {"alice", "bob", "carol", "user", "username", "you", "your-name", "yourname", "me", "runner",
     "example", "someone", "name", "shared", "test", "tester"}
)
PLACEHOLDER_TAILNET_LABELS = frozenset(
    {"example", "tailnet", "your-tailnet", "yourtailnet", "machine", "my-machine", "relay", "device", "host"}
)
# Private addresses that are obviously documentation (router defaults, range bases, ...).
DOC_PRIVATE_IPS = frozenset(
    {"10.0.0.0", "10.0.0.1", "10.0.0.2", "10.0.0.5", "172.16.0.0", "172.16.0.1",
     "192.168.0.0", "192.168.0.1", "192.168.1.1", "192.168.1.10", "192.168.1.20"}
)

_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_IP_END = r"(?![\d.]*\d)"  # not followed by more dotted digits (version strings, longer tokens)
_IP_START = r"(?<![\w.])"

CGNAT_RE = re.compile(rf"{_IP_START}100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.{_OCTET}\.{_OCTET}{_IP_END}")
PRIVATE_RE = re.compile(
    rf"{_IP_START}(?:10\.{_OCTET}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.{_OCTET}\.{_OCTET}{_IP_END}"
)
TSNET_RE = re.compile(r"(?<![\w.-])((?:[a-z0-9-]+\.)+)ts\.net\b", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b")
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "users.noreply.github.com", "anthropic.com")
HOME_RE = re.compile(r"(?:/home/|/Users/|\b[A-Za-z]:(?:\\{1,2}|/)Users(?:\\{1,2}|/))([A-Za-z0-9._-]+)")

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("amc-secret", re.compile(r"\bamc[kdor]_[A-Za-z0-9_-]{20,}")),
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-|ant-)?[A-Za-z0-9_-]{20,}")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
)


@dataclass(frozen=True)
class Finding:
    where: str
    rule: str
    excerpt: str

    def render(self) -> str:
        return f"{self.where}: [{self.rule}] {self.excerpt}"


def _redact(value: str) -> str:
    return value if len(value) <= 10 else value[:8] + "…"


def load_denylist(path: Path) -> list[str]:
    terms: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        term = raw.strip()
        if term and not term.startswith("#"):
            terms.append(term)
    return terms


def scan_line(line: str, denylist: Iterable[str] = ()) -> list[tuple[str, str]]:
    """Return ``(rule, excerpt)`` hits for one line of text (after the allow marker check)."""
    if ALLOW_MARKER in line:
        return []
    hits: list[tuple[str, str]] = []
    for match in CGNAT_RE.finditer(line):
        if match.group(0) == "100.64.0.0":  # the range itself (100.64.0.0/10), not a host
            continue
        hits.append(("cgnat-ip", match.group(0)))
    for match in PRIVATE_RE.finditer(line):
        if match.group(0) not in DOC_PRIVATE_IPS:
            hits.append(("private-ip", match.group(0)))
    for match in TSNET_RE.finditer(line):
        labels = [label.lower() for label in match.group(1).split(".") if label]
        if not all(label in PLACEHOLDER_TAILNET_LABELS for label in labels):
            hits.append(("ts-net-host", match.group(0)))
    for match in HOME_RE.finditer(line):
        if match.group(1).lower() not in PLACEHOLDER_USERS:
            hits.append(("home-path", match.group(0)))
    for match in EMAIL_RE.finditer(line):
        domain = match.group(1).lower()
        if not any(domain == allowed or domain.endswith("." + allowed) for allowed in ALLOWED_EMAIL_DOMAINS):
            hits.append(("email", match.group(0)))
    for rule, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(line):
            hits.append((rule, _redact(match.group(0))))
    lowered = line.lower()
    for index, term in enumerate(denylist, start=1):
        if term.lower() in lowered:
            hits.append(("denylist", f"term #{index}"))
    return hits


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def repo_files(root: Path) -> list[str]:
    out = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return sorted({name for name in out.split("\0") if name})


def scan_files(root: Path, denylist: list[str]) -> Iterator[Finding]:
    for name in repo_files(root):
        # the path itself can leak (e.g. a file named after a person)
        for rule, excerpt in scan_line(name, denylist):
            yield Finding(f"{name}:0", rule, excerpt)
        path = root / name
        if not path.is_file():
            continue  # deleted in the working tree
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue  # binary
        text = data.decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            for rule, excerpt in scan_line(line, denylist):
                yield Finding(f"{name}:{number}", rule, excerpt)


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def scan_history(root: Path, denylist: list[str]) -> Iterator[Finding]:
    log = _git(root, "log", "-p", "--all", "--no-color", "--no-ext-diff", "--format=fuller", "--no-renames")
    commit = "?"
    path = "(header)"
    old_line = new_line = 0
    seen: set[tuple[str, str, str]] = set()
    for line in log.splitlines():
        if line.startswith("commit ") and len(line.split()) >= 2:
            commit = line.split()[1][:12]
            path = "(header)"
            continue
        if line.startswith("diff --git "):
            path = line.split(" b/", 1)[-1]
            continue
        hunk = _HUNK_RE.match(line)
        if hunk:
            old_line, new_line = int(hunk.group(1)), int(hunk.group(2))
            continue
        if line.startswith(("+++ ", "--- ", "index ")):
            continue
        location = path
        if path != "(header)":
            if line.startswith("+"):
                location, new_line = f"{path}:{new_line}", new_line + 1
            elif line.startswith("-"):
                location, old_line = f"{path}:{old_line}(old)", old_line + 1
            else:
                location = f"{path}:{new_line}"
                old_line, new_line = old_line + 1, new_line + 1
        for rule, excerpt in scan_line(line, denylist):
            key = (path, rule, excerpt)
            if key in seen:
                continue
            seen.add(key)
            yield Finding(f"history {commit} {location}", rule, excerpt)


def check_required(root: Path) -> Iterator[Finding]:
    for name in REQUIRED_FILES:
        if not (root / name).is_file():
            yield Finding(name, "missing-file", "required for a public release")


def run(root: Path, denylist: list[str], *, history: bool = True) -> list[Finding]:
    findings = list(check_required(root))
    findings += scan_files(root, denylist)
    if history:
        findings += scan_history(root, denylist)
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--denylist", action="append", type=Path, default=[],
                        help="file with private terms, one per line (repeatable); never commit it")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="repository root (default: this script's repository)")
    parser.add_argument("--no-history", action="store_true", help="skip the git history scan")
    args = parser.parse_args(argv)

    try:
        terms: list[str] = []
        for path in args.denylist:
            terms += load_denylist(path)
        findings = run(args.root.resolve(), terms, history=not args.no_history)
    except (OSError, RuntimeError) as exc:
        print(f"release_check: cannot run: {exc}", file=sys.stderr)
        return 2

    for finding in findings:
        print(finding.render())
    scope = "tree" if args.no_history else "tree + history"
    extra = f", {len(terms)} denylist terms" if args.denylist else ""
    if findings:
        print(f"release_check: FAIL — {len(findings)} finding(s) ({scope}{extra})", file=sys.stderr)
        return 1
    print(f"release_check: OK ({scope}{extra})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
