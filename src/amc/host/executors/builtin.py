"""Built-in, dependency-free executor.

``BuiltinExecutor`` implements every tool in :data:`amc.core.policy.TOOL_ACTIONS` in pure
Python with DesktopCommander-compatible tool and argument names. It works on Linux,
macOS and Windows.

Notes for callers:

* Path confinement is **not** done here; the host checks every path argument with
  :class:`amc.host.confine.Confinement` before calling :meth:`BuiltinExecutor.execute`.
  Path arguments are expanded exactly like the confinement layer does
  (``expandvars`` + ``expanduser``, relative paths joined to ``cwd``), so the executor's
  ``cwd`` must equal the confinement ``cwd``.
* Process and search handles belong to the principal that created them. Using a handle
  owned by someone else reports "not found"; listings only show the caller's handles.
  ``list_processes`` and ``kill_process`` (raw OS pid) are OS-wide by design.
* ``read_process_output`` offset semantics: ``offset=0`` returns *new* output since the
  last read of that session (up to ``length`` lines) and advances the read cursor;
  ``offset>0`` returns ``length`` lines starting at that absolute line number (0-based)
  of the retained buffer without moving the cursor; ``offset<0`` returns the last
  ``-offset`` lines without moving the cursor. ``start_process`` and
  ``interact_with_process`` also advance the cursor past the output they return.
* The executor never raises from :meth:`execute`: unknown tools, bad arguments and OS
  errors come back as ``ok=False`` results.
"""

from __future__ import annotations

import asyncio
import base64
import codecs
import collections
import contextlib
import fnmatch
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from amc.core import RemoteResult

from .base import text_result

IS_WINDOWS = sys.platform == "win32"

IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
SKIP_DIRS = {".git", "node_modules"}
MAX_LIST_ENTRIES = 1000
MAX_PROCESS_BUFFER = 5 * 1024 * 1024
MAX_LINE_CHARS = 500
MAX_SESSIONS_KEPT = 100
MAX_PROCESS_LIST_LINES = 2000
START_SEARCH_WAIT = 0.5
FIRST_RESULTS_SHOWN = 50
QUIET_PERIOD = 0.3
POLL = 0.05


class ToolError(Exception):
    """A user-facing tool failure (bad argument, not found, ...)."""


# --------------------------------------------------------------------------- arguments


def _arg_str(args: dict[str, Any], key: str, *, required: bool = True, default: str | None = None) -> str:
    value = args.get(key)
    if value is None:
        if required:
            raise ToolError(f"missing required argument: {key}")
        return default  # type: ignore[return-value]
    if not isinstance(value, str):
        raise ToolError(f"argument {key} must be a string")
    return value


def _arg_opt_str(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ToolError(f"argument {key} must be a string")
    return value


def _arg_int(args: dict[str, Any], key: str, default: int | None, *, required: bool = False) -> int | None:
    value = args.get(key)
    if value is None:
        if required:
            raise ToolError(f"missing required argument: {key}")
        return default
    if isinstance(value, bool):
        raise ToolError(f"argument {key} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            pass
    raise ToolError(f"argument {key} must be an integer")


def _arg_bool(args: dict[str, Any], key: str, default: bool) -> bool:
    value = args.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false", "1", "0"}:
        return value.strip().lower() in {"true", "1"}
    if isinstance(value, int):
        return bool(value)
    raise ToolError(f"argument {key} must be a boolean")


# --------------------------------------------------------------------------- helpers


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _is_binary(path: Path) -> bool:
    with path.open("rb") as handle:
        return b"\x00" in handle.read(8192)


def _clip(line: str) -> str:
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + "..."


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def _ok(call_id: str, content: list[dict[str, Any]]) -> RemoteResult:
    return RemoteResult(call_id=call_id, ok=True, result={"content": content, "isError": False})


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


# --------------------------------------------------------------------------- sessions


@dataclass
class ProcessSession:
    pid: int
    owner: str
    command: str
    proc: asyncio.subprocess.Process
    started: float = field(default_factory=time.monotonic)
    buffer: str = ""
    dropped: int = 0  # characters discarded from the head of the buffer
    cursor: int = 0  # absolute character offset of the next unread output
    last_output: float = field(default_factory=time.monotonic)
    ended: float | None = None
    reader: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self.proc.returncode is None

    @property
    def total(self) -> int:
        return self.dropped + len(self.buffer)

    def append(self, text: str) -> None:
        if not text:
            return
        self.buffer += text
        if len(self.buffer) > MAX_PROCESS_BUFFER:
            cut = len(self.buffer) - MAX_PROCESS_BUFFER
            self.buffer = self.buffer[cut:]
            self.dropped += cut
        self.last_output = time.monotonic()

    def unread(self) -> str:
        start = max(self.cursor, self.dropped) - self.dropped
        return self.buffer[start:]

    def take_unread(self, max_lines: int | None = None) -> tuple[str, int]:
        """Return unread text (up to ``max_lines`` lines), advance the cursor past it.

        Also returns how many unread lines remain afterwards.
        """
        self.cursor = max(self.cursor, self.dropped)
        text = self.unread()
        if max_lines is None:
            self.cursor += len(text)
            return text, 0
        lines = text.splitlines(keepends=True)
        taken = "".join(lines[:max_lines])
        self.cursor += len(taken)
        return taken, max(0, len(lines) - max_lines)

    def status(self) -> str:
        if self.running:
            return "running"
        return f"exited (exit code: {self.proc.returncode})"

    def runtime(self) -> float:
        return (self.ended or time.monotonic()) - self.started


@dataclass
class SearchSession:
    session_id: str
    owner: str
    pattern: str
    search_type: str
    root: str
    started: float = field(default_factory=time.monotonic)
    results: list[str] = field(default_factory=list)
    done: bool = False
    stopped: bool = False
    error: str | None = None
    ended: float | None = None
    stop_flag: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task[None] | None = None

    def status(self) -> str:
        if self.error:
            return f"failed: {self.error}"
        if self.stopped:
            return "stopped"
        return "complete" if self.done else "running"

    def runtime(self) -> float:
        return (self.ended or time.monotonic()) - self.started


# --------------------------------------------------------------------------- executor


class BuiltinExecutor:
    def __init__(self, *, cwd: str, env: dict[str, str], max_read_bytes: int = 10 * 1024 * 1024,
                 is_protected: Callable[[Path], bool] | None = None) -> None:
        self.cwd = str(Path(os.path.expanduser(cwd)))
        # Directory walks (listing, search) never enter protected paths or follow symlinks.
        self.is_protected = is_protected or (lambda _path: False)
        self.env = dict(env)
        self.max_read_bytes = max_read_bytes
        self._processes: dict[int, ProcessSession] = {}
        self._searches: dict[str, SearchSession] = {}
        self._handlers: dict[str, Callable[[str, dict[str, Any], str], Awaitable[RemoteResult]]] = {
            "read_file": self._read_file,
            "read_multiple_files": self._read_multiple_files,
            "write_file": self._write_file,
            "edit_block": self._edit_block,
            "create_directory": self._create_directory,
            "move_file": self._move_file,
            "list_directory": self._list_directory,
            "get_file_info": self._get_file_info,
            "start_search": self._start_search,
            "get_more_search_results": self._get_more_search_results,
            "stop_search": self._stop_search,
            "list_searches": self._list_searches,
            "start_process": self._start_process,
            "read_process_output": self._read_process_output,
            "interact_with_process": self._interact_with_process,
            "force_terminate": self._force_terminate,
            "list_sessions": self._list_sessions,
            "list_processes": self._list_processes,
            "kill_process": self._kill_process,
            "system_status": self._system_status,
        }

    # ------------------------------------------------------------------ protocol

    async def start(self) -> None:
        return None

    async def execute(
        self, *, call_id: str, tool: str, arguments: dict[str, Any], principal_id: str
    ) -> RemoteResult:
        handler = self._handlers.get(tool)
        if handler is None:
            return text_result(call_id, f"Unknown tool: {tool}", is_error=True)
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return text_result(call_id, "arguments must be an object", is_error=True)
        try:
            return await handler(call_id, arguments, principal_id)
        except ToolError as exc:
            return text_result(call_id, f"Error: {exc}", is_error=True)
        except FileNotFoundError as exc:
            return text_result(call_id, f"Error: not found: {exc.filename or exc}", is_error=True)
        except PermissionError as exc:
            return text_result(call_id, f"Error: permission denied: {exc.filename or exc}", is_error=True)
        except OSError as exc:
            return text_result(call_id, f"Error: {exc}", is_error=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never raise out of execute
            return text_result(call_id, f"Error: {type(exc).__name__}: {exc}", is_error=True)

    async def shutdown(self) -> None:
        for search in list(self._searches.values()):
            search.stop_flag.set()
            if search.task is not None and not search.task.done():
                search.task.cancel()
        for session in list(self._processes.values()):
            if session.running:
                with contextlib.suppress(Exception):
                    await self._terminate(session)
            if session.reader is not None and not session.reader.done():
                session.reader.cancel()
        self._processes.clear()
        self._searches.clear()

    # ------------------------------------------------------------------ paths

    def _path(self, raw: str) -> Path:
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            path = Path(self.cwd) / path
        return path

    # ------------------------------------------------------------------ files

    def _image_blocks(self, path: Path, mime: str) -> list[dict[str, Any]]:
        size = path.stat().st_size
        if size > self.max_read_bytes:
            raise ToolError(f"image too large ({size} bytes > {self.max_read_bytes}): {path}")
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return [_text(f"Image file: {path} ({mime})"), {"type": "image", "data": data, "mimeType": mime}]

    def _read_text_lines(self, path: Path, offset: int, length: int) -> str:
        if _is_binary(path):
            raise ToolError(f"cannot read binary file: {path}")
        total = 0
        if offset < 0:
            tail: collections.deque[str] = collections.deque(maxlen=-offset)
            with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
                for line in handle:
                    total += 1
                    tail.append(line)
            selected = list(tail)[:length]
            start = total - len(tail)
        else:
            selected = []
            with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
                for index, line in enumerate(handle):
                    total += 1
                    if offset <= index < offset + length:
                        selected.append(line)
            start = offset
        remaining = max(0, total - start - len(selected))
        header = f"[Reading {len(selected)} lines from line {start} (total: {total} lines"
        header += f", {remaining} remaining)]" if remaining else ")]"
        return header + "\n" + "".join(selected)

    def _read_one(self, raw: str, offset: int, length: int) -> list[dict[str, Any]]:
        path = self._path(raw)
        if path.is_dir():
            raise ToolError(f"is a directory: {path}")
        if not path.exists():
            raise ToolError(f"file not found: {path}")
        mime = IMAGE_MIME.get(path.suffix.lower())
        if mime:
            return self._image_blocks(path, mime)
        return [_text(self._read_text_lines(path, offset, length))]

    async def _read_file(self, call_id: str, args: dict[str, Any], principal: str) -> RemoteResult:
        raw = _arg_str(args, "path")
        offset = _arg_int(args, "offset", 0) or 0
        length = _arg_int(args, "length", 1000)
        if length is None or length <= 0:
            raise ToolError("length must be a positive integer")
        content = await asyncio.to_thread(self._read_one, raw, offset, length)
        return _ok(call_id, content)

    async def _read_multiple_files(self, call_id: str, args: dict[str, Any], principal: str) -> RemoteResult:
        paths = args.get("paths")
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
            raise ToolError("paths must be a non-empty list of strings")

        def work() -> list[dict[str, Any]]:
            blocks: list[dict[str, Any]] = []
            for raw in paths:
                header = f"--- {raw} ---\n"
                try:
                    parts = self._read_one(raw, 0, 10**12)
                except (ToolError, OSError) as exc:
                    blocks.append(_text(f"{header}Error: {exc}\n"))
                    continue
                if parts and parts[0]["type"] == "text" and len(parts) == 1:
                    blocks.append(_text(header + parts[0]["text"]))
                else:
                    blocks.append(_text(header.rstrip("\n")))
                    blocks.extend(parts)
            return blocks

        return _ok(call_id, await asyncio.to_thread(work))

    async def _write_file(self, call_id: str, args: dict[str, Any], principal: str) -> RemoteResult:
        path = self._path(_arg_str(args, "path"))
        content = _arg_str(args, "content")
        mode = _arg_str(args, "mode", required=False, default="rewrite")
        if mode not in {"rewrite", "append"}:
            raise ToolError("mode must be 'rewrite' or 'append'")

        def work() -> str:
            if path.is_dir():
                raise ToolError(f"is a directory: {path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            data = content.encode("utf-8")
            with path.open("ab" if mode == "append" else "wb") as handle:
                handle.write(data)
            lines = len(content.splitlines())
            verb = "appended to" if mode == "append" else "wrote"
            return f"Successfully {verb} {path} ({len(data)} bytes, {lines} lines)"

        return text_result(call_id, await asyncio.to_thread(work))

    async def _edit_block(self, call_id: str, args: dict[str, Any], principal: str) -> RemoteResult:
        path = self._path(_arg_str(args, "file_path"))
        old = _arg_str(args, "old_string")
        new = _arg_str(args, "new_string")
        expected = _arg_int(args, "expected_replacements", 1)
        if not old:
            raise ToolError("old_string must not be empty")
        if expected is None or expected < 1:
            raise ToolError("expected_replacements must be >= 1")

        def work() -> str:
            if not path.is_file():
                raise ToolError(f"file not found: {path}")
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise ToolError(f"file is not valid UTF-8 text: {path}") from None
            count = text.count(old)
            if count == 0:
                raise ToolError(f"old_string not found in {path} (0 occurrences); file not modified")
            if count != expected:
                raise ToolError(
                    f"expected {expected} replacement(s) but found {count} occurrences in {path}; "
                    "file not modified (set expected_replacements to match, or make old_string unique)"
                )
            path.write_bytes(text.replace(old, new).encode("utf-8"))
            return f"Successfully applied {count} edit(s) to {path}"

        return text_result(call_id, await asyncio.to_thread(work))

    async def _create_directory(self, call_id: str, args: dict[str, Any], principal: str) -> RemoteResult:
        path = self._path(_arg_str(args, "path"))
        await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)
        return text_result(call_id, f"Successfully created directory {path}")

    async def _move_file(self, call_id: str, args: dict[str, Any], principal: str) -> RemoteResult:
        source = self._path(_arg_str(args, "source"))
        destination = self._path(_arg_str(args, "destination"))

        def work() -> str:
            if not source.exists() and not source.is_symlink():
                raise ToolError(f"source not found: {source}")
            if destination.exists():
                raise ToolError(f"destination already exists: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            return f"Successfully moved {source} to {destination}"

        return text_result(call_id, await asyncio.to_thread(work))

    async def _list_directory(self, call_id: str, args: dict[str, Any], principal: str) -> RemoteResult:
        root = self._path(_arg_str(args, "path"))
        depth = _arg_int(args, "depth", 2) or 1
        if depth < 1:
            depth = 1

        def work() -> str:
            if not root.is_dir():
                raise ToolError(f"not a directory: {root}")
            lines: list[str] = []
            truncated = False

            def walk(directory: Path, prefix: str, level: int) -> None:
                nonlocal truncated
                try:
                    entries = sorted(os.scandir(directory), key=lambda e: e.name.lower())
                except PermissionError:
                    lines.append(f"[DENIED] {prefix or '.'}")
                    return
                for entry in entries:
                    if len(lines) >= MAX_LIST_ENTRIES:
                        truncated = True
                        return
                    rel = f"{prefix}{entry.name}"
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                    lines.append(f"[DIR] {rel}" if is_dir else f"[FILE] {rel}")
                    if is_dir and level < depth and not self.is_protected(Path(entry.path)):
                        walk(Path(entry.path), rel + "/", level + 1)
                    if truncated:
                        return

            walk(root, "", 1)
            if truncated:
                lines.append(f"[WARNING] listing truncated at {MAX_LIST_ENTRIES} entries")
            return "\n".join(lines) if lines else "(empty directory)"

        return text_result(call_id, await asyncio.to_thread(work))

    async def _get_file_info(self, call_id: str, args: dict[str, Any], principal: str) -> RemoteResult:
        path = self._path(_arg_str(args, "path"))

        def work() -> str:
            st = path.stat()
            created = getattr(st, "st_birthtime", None) or st.st_ctime
            is_dir = path.is_dir()
            is_file = path.is_file()
            lines = [
                f"size: {st.st_size}",
                f"created: {_iso(created)}",
                f"modified: {_iso(st.st_mtime)}",
                f"accessed: {_iso(st.st_atime)}",
                f"isDirectory: {str(is_dir).lower()}",
                f"isFile: {str(is_file).lower()}",
                f"permissions: {st.st_mode & 0o777:o}",
            ]
            if (
                is_file
                and path.suffix.lower() not in IMAGE_MIME
                and st.st_size <= self.max_read_bytes
                and not _is_binary(path)
            ):
                with path.open("rb") as handle:
                    data = handle.read()
                count = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
                lines.append(f"lineCount: {count}")
            return "\n".join(lines)

        return text_result(call_id, await asyncio.to_thread(work))

    # ------------------------------------------------------------------ search

    def _search_owned(self, session_id: str, principal: str) -> SearchSession:
        session = self._searches.get(session_id)
        if session is None or session.owner != principal:
            raise ToolError(f"search session not found: {session_id}")
        return session

    async def _start_search(self, call_id: str, args: dict[str, Any], principal: str) -> RemoteResult:
        root = self._path(_arg_str(args, "path"))
        pattern = _arg_str(args, "pattern")
        search_type = _arg_str(args, "searchType", required=False, default="files")
        if search_type not in {"files", "content"}:
            raise ToolError("searchType must be 'files' or 'content'")
        file_pattern = _arg_opt_str(args, "filePattern")
        ignore_case = _arg_bool(args, "ignoreCase", True)
        literal = _arg_bool(args, "literalSearch", False)
        max_results = _arg_int(args, "maxResults", None)
        context = max(0, _arg_int(args, "contextLines", 5) or 0)
        include_hidden = _arg_bool(args, "includeHidden", False)
        if not pattern:
            raise ToolError("pattern must not be empty")
        if not root.exists():
            raise ToolError(f"path not found: {root}")

        file_globs = [g.strip() for g in (file_pattern or "").split("|") if g.strip()]
        regex: re.Pattern[str] | None = None
        if search_type == "content":
            try:
                flags = re.IGNORECASE if ignore_case else 0
                regex = re.compile(re.escape(pattern) if literal else pattern, flags)
            except re.error as exc:
                raise ToolError(f"invalid regular expression: {exc}") from None

        session = SearchSession(
            session_id=uuid.uuid4().hex[:12],
            owner=principal,
            pattern=pattern,
            search_type=search_type,
            root=str(root),
        )

        def name_ok(name: str) -> bool:
            if not file_globs:
                return True
            probe = name.lower() if ignore_case else name
            return any(fnmatch.fnmatchcase(probe, g.lower() if ignore_case else g) for g in file_globs)

        def name_matches(name: str) -> bool:
            probe = name.lower() if ignore_case else name
            pat = pattern.lower() if ignore_case else pattern
            if not literal and any(ch in pat for ch in "*?["):
                return fnmatch.fnmatchcase(probe, pat)
            return pat in probe

        def full() -> bool:
            return max_results is not None and max_results > 0 and len(session.results) >= max_results

        def scan_content(file_path: Path) -> None:
            assert regex is not None
            try:
                if file_path.is_symlink() or self.is_protected(file_path):
                    return
                if file_path.stat().st_size > self.max_read_bytes or _is_binary(file_path):
                    return
                with file_path.open("r", encoding="utf-8", errors="replace") as handle:
                    lines = handle.read().splitlines()
            except OSError:
                return
            for index, line in enumerate(lines):
                if session.stop_flag.is_set() or full():
                    return
                if regex.search(line):
                    block = [f"{file_path}:{index + 1}: {_clip(line)}"]
                    if context:
                        lo, hi = max(0, index - context), min(len(lines), index + context + 1)
                        for ctx in range(lo, hi):
                            if ctx != index:
                                block.append(f"  {ctx + 1}- {_clip(lines[ctx])}")
                    session.results.append("\n".join(block))

        def walk() -> None:
            if root.is_file():
                candidates = [(root.parent, [], [root.name])]
                iterator: Any = iter(candidates)
            else:
                iterator = os.walk(root)
            for dirpath, dirnames, filenames in iterator:
                if session.stop_flag.is_set() or full():
                    return
                dirnames[:] = sorted(
                    d for d in dirnames
                    if d not in SKIP_DIRS and (include_hidden or not d.startswith("."))
                    and not self.is_protected(Path(dirpath) / d)
                )
                if search_type == "files":
                    for name in sorted(dirnames):
                        if name_matches(name) and not file_globs:
                            session.results.append(str(Path(dirpath) / name))
                            if full():
                                return
                for name in sorted(filenames):
                    if session.stop_flag.is_set() or full():
                        return
                    if not include_hidden and name.startswith("."):
                        continue
                    if not name_ok(name):
                        continue
                    if search_type == "files":
                        if name_matches(name):
                            session.results.append(str(Path(dirpath) / name))
                    else:
                        scan_content(Path(dirpath) / name)

        async def runner() -> None:
            try:
                await asyncio.to_thread(walk)
            except Exception as exc:  # recorded on the session, surfaced to the caller
                session.error = str(exc)
            finally:
                session.done = True
                session.ended = time.monotonic()

        session.task = asyncio.create_task(runner())
        self._searches[session.session_id] = session
        self._prune_searches()
        deadline = time.monotonic() + START_SEARCH_WAIT
        while not session.done and time.monotonic() < deadline:
            await asyncio.sleep(POLL)
        shown = session.results[:FIRST_RESULTS_SHOWN]
        text = [
            f"Started {search_type} search session: {session.session_id}",
            f"sessionId: {session.session_id}",
            f"Status: {session.status()}",
            f"Results so far: {len(session.results)}",
        ]
        if shown:
            text.append("")
            text.extend(shown)
        if not session.done or len(session.results) > len(shown):
            text.append("")
            text.append("Use get_more_search_results with this sessionId for more results.")
        return text_result(call_id, "\n".join(text))

    def _prune_searches(self) -> None:
        finished = [s for s in self._searches.values() if s.done]
        excess = len(self._searches) - MAX_SESSIONS_KEPT
        for session in sorted(finished, key=lambda s: s.started)[: max(0, excess)]:
            self._searches.pop(session.session_id, None)

    async def _get_more_search_results(
        self, call_id: str, args: dict[str, Any], principal: str
    ) -> RemoteResult:
        session = self._search_owned(_arg_str(args, "sessionId"), principal)
        offset = _arg_int(args, "offset", 0) or 0
        length = _arg_int(args, "length", 100) or 100
        results = list(session.results)
        if offset < 0:
            start = max(0, len(results) + offset)
            page = results[start:]
        else:
            start = offset
            page = results[offset : offset + length]
        complete = session.done
        header = [
            f"sessionId: {session.session_id}",
            f"Status: {session.status()}",
            f"Search complete: {'yes' if complete else 'no (more results may arrive)'}",
            f"Showing results {start}-{start + len(page)} of {len(results)}",
        ]
        if not complete:
            pass
        elif start + len(page) < len(results):
            header.append(f"More results available: use offset={start + len(page)}")
        return text_result(call_id, "\n".join(header + ([""] + page if page else [])))

    async def _stop_search(self, call_id: str, args: dict[str, Any], principal: str) -> RemoteResult:
        session = self._search_owned(_arg_str(args, "sessionId"), principal)
        if not session.done:
            session.stop_flag.set()
            session.stopped = True
        return text_result(
            call_id, f"Search session {session.session_id} stopped ({len(session.results)} results kept)"
        )

    async def _list_searches(self, call_id: str, args: dict[str, Any], principal: str) -> RemoteResult:
        mine = [s for s in self._searches.values() if s.owner == principal]
        if not mine:
            return text_result(call_id, "No search sessions.")
        lines = [
            f"sessionId: {s.session_id}, type: {s.search_type}, pattern: {s.pattern!r}, "
            f"status: {s.status()}, results: {len(s.results)}, runtime: {s.runtime():.1f}s"
            for s in mine
        ]
        return text_result(call_id, "\n".join(lines))

    # ------------------------------------------------------------------ processes

    def _which(self, name: str) -> str | None:
        return shutil.which(name, path=self.env.get("PATH") or self.env.get("Path") or os.environ.get("PATH"))

    def _shell_argv(self, command: str, shell: str | None) -> list[str]:
        if IS_WINDOWS:
            chosen = shell or self._which("powershell.exe") or self._which("pwsh.exe")
            if not chosen:
                return [self.env.get("COMSPEC") or "cmd.exe", "/c", command]
            base = os.path.basename(chosen).lower()
            if base.startswith("cmd"):
                return [chosen, "/c", command]
            if base.startswith(("powershell", "pwsh")):
                # -Command reports only 0/1; propagate the last native program's real exit code.
                wrapped = (f"{command}\nif ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) "
                           "{ exit $LASTEXITCODE }")
                return [chosen, "-NoProfile", "-Command", wrapped]
            return [chosen, "-c", command]
        chosen = shell or self.env.get("SHELL") or "/bin/sh"
        return [chosen, "-c", command]

    def _owned_process(self, pid: int, principal: str) -> ProcessSession:
        session = self._processes.get(pid)
        if session is None or session.owner != principal:
            raise ToolError(f"session not found for PID {pid}")
        return session

    async def _pump(self, session: ProcessSession) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        stream = session.proc.stdout
        assert stream is not None
        try:
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                session.append(decoder.decode(chunk))
            session.append(decoder.decode(b"", final=True))
        finally:
            with contextlib.suppress(Exception):
                await session.proc.wait()
            session.ended = session.ended or time.monotonic()

    async def _wait(
        self, session: ProcessSession, timeout: float, *, quiet: float | None = None, since: int | None = None
    ) -> None:
        """Wait until the process exits, ``timeout`` elapses or (with ``quiet``) output
        arrived after ``since`` and then stayed quiet for ``quiet`` seconds."""
        deadline = time.monotonic() + max(0.0, timeout)
        exited_at: float | None = None
        while True:
            now = time.monotonic()
            if not session.running:
                reader_done = session.reader is None or session.reader.done()
                exited_at = exited_at or now
                if reader_done or now - exited_at > 0.5:
                    return
            if now >= deadline:
                return
            if quiet is not None and since is not None and session.total > since:
                if now - session.last_output >= quiet:
                    return
            await asyncio.sleep(POLL)

    async def _terminate(self, session: ProcessSession) -> None:
        if not session.running:
            return
        pid = session.pid
        if IS_WINDOWS:
            with contextlib.suppress(Exception):
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), 10)
            with contextlib.suppress(ProcessLookupError):
                session.proc.kill()
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(session.proc.wait(), 2)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pid, signal.SIGKILL)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(session.proc.wait(), 5)
        session.ended = session.ended or time.monotonic()

    def _prune_processes(self) -> None:
        excess = len(self._processes) - MAX_SESSIONS_KEPT
        if excess <= 0:
            return
        finished = sorted((s for s in self._processes.values() if not s.running), key=lambda s: s.started)
        for session in finished[:excess]:
            self._processes.pop(session.pid, None)

    async def _start_process(self, call_id: str, args: dict[str, Any], principal: str) -> RemoteResult:
        command = _arg_str(args, "command")
        timeout_ms = _arg_int(args, "timeout_ms", 10000) or 0
        shell = _arg_opt_str(args, "shell")
        if not command.strip():
            raise ToolError("command must not be empty")
        argv = self._shell_argv(command, shell)
        kwargs: dict[str, Any] = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self.cwd,
                env=self.env,
                **kwargs,
            )
        except FileNotFoundError:
            raise ToolError(f"shell not found: {argv[0]}") from None
        session = ProcessSession(pid=proc.pid, owner=principal, command=command, proc=proc)
        session.reader = asyncio.create_task(self._pump(session))
        self._processes[proc.pid] = session
        self._prune_processes()
        await self._wait(session, timeout_ms / 1000)
        output, _ = session.take_unread()
        text = [f"Process started with PID {proc.pid}", ""]
        if output:
            text.append(output.rstrip("\n"))
            text.append("")
        if session.running:
            text.append(
                f"Process is still running after {timeout_ms} ms. "
                "Use read_process_output / interact_with_process with this PID."
            )
        else:
            text.append(f"Process finished with exit code: {proc.returncode}")
        return text_result(call_id, "\n".join(text))

    async def _read_process_output(self, call_id: str, args: dict[str, Any], principal: str) -> RemoteResult:
        pid = _arg_int(args, "pid", None, required=True)
        assert pid is not None
        offset = _arg_int(args, "offset", 0) or 0
        length = _arg_int(args, "length", 1000) or 1000
        timeout_ms = _arg_int(args, "timeout_ms", 1000) or 0
        session = self._owned_process(pid, principal)
        remaining = 0
        if offset == 0:
            if not session.unread() and session.running:
                await self._wait(session, timeout_ms / 1000, quiet=0.1, since=session.total)
            output, remaining = session.take_unread(length)
            label = "New output"
        else:
            lines = session.buffer.splitlines(keepends=True)
            if offset < 0:
                picked = lines[offset:][:length]
                label = f"Last {len(picked)} lines"
            else:
                picked = lines[offset : offset + length]
                label = f"Lines {offset}-{offset + len(picked)} of {len(lines)}"
            output = "".join(picked)
        body = output.rstrip("\n") if output else "(no output)"
        text = [f"PID {pid}: {session.status()}", f"{label}:", body]
        if remaining:
            text.append(f"[{remaining} more unread lines; call again]")
        return text_result(call_id, "\n".join(text))

    async def _interact_with_process(
        self, call_id: str, args: dict[str, Any], principal: str
    ) -> RemoteResult:
        pid = _arg_int(args, "pid", None, required=True)
        assert pid is not None
        data = _arg_str(args, "input")
        timeout_ms = _arg_int(args, "timeout_ms", 8000) or 0
        wait_for_prompt = _arg_bool(args, "wait_for_prompt", True)
        session = self._owned_process(pid, principal)
        if not session.running:
            raise ToolError(f"process {pid} is not running ({session.status()})")
        stdin = session.proc.stdin
        if stdin is None or stdin.is_closing():
            raise ToolError(f"stdin of process {pid} is closed")
        if not data.endswith("\n"):
            data += "\n"
        # Anything printed before this input is part of this call's reply as well.
        since = session.total
        try:
            stdin.write(data.encode("utf-8"))
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            raise ToolError(f"stdin of process {pid} is closed") from None
        timeout = timeout_ms / 1000 if wait_for_prompt else min(timeout_ms / 1000, 1.0)
        await self._wait(session, timeout, quiet=QUIET_PERIOD, since=since)
        output, _ = session.take_unread()
        text = [f"PID {pid}: {session.status()}", output.rstrip("\n") if output else "(no output)"]
        return text_result(call_id, "\n".join(text))

    async def _force_terminate(self, call_id: str, args: dict[str, Any], principal: str) -> RemoteResult:
        pid = _arg_int(args, "pid", None, required=True)
        assert pid is not None
        session = self._owned_process(pid, principal)
        if not session.running:
            return text_result(call_id, f"Process {pid} already {session.status()}")
        await self._terminate(session)
        return text_result(call_id, f"Terminated process {pid} ({session.status()})")

    async def _list_sessions(self, call_id: str, args: dict[str, Any], principal: str) -> RemoteResult:
        mine = [s for s in self._processes.values() if s.owner == principal]
        if not mine:
            return text_result(call_id, "No active sessions.")
        lines = []
        for s in mine:
            command = s.command if len(s.command) <= 80 else s.command[:77] + "..."
            lines.append(
                f"PID: {s.pid}, Status: {s.status()}, Runtime: {s.runtime():.1f}s, Command: {command}"
            )
        return text_result(call_id, "\n".join(lines))

    async def _run(self, argv: list[str], timeout: float = 10) -> tuple[int, str] | None:
        exe = argv[0] if os.path.isabs(argv[0]) else self._which(argv[0])
        if not exe:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, *argv[1:],
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL, env=self.env,
            )
        except OSError:
            return None
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return None
        return proc.returncode or 0, out.decode("utf-8", errors="replace")

    async def _list_processes(self, call_id: str, args: dict[str, Any], principal: str) -> RemoteResult:
        if IS_WINDOWS:
            result = await self._run(["tasklist", "/FO", "CSV", "/NH"])
        else:
            result = await self._run(["ps", "-eo", "pid,ppid,pcpu,pmem,comm"])
        if result is None:
            raise ToolError("could not list processes (ps/tasklist unavailable)")
        lines = result[1].splitlines()
        if len(lines) > MAX_PROCESS_LIST_LINES:
            extra = len(lines) - MAX_PROCESS_LIST_LINES
            lines = lines[:MAX_PROCESS_LIST_LINES] + [f"[... {extra} more processes truncated]"]
        return text_result(call_id, "\n".join(lines))

    async def _kill_process(self, call_id: str, args: dict[str, Any], principal: str) -> RemoteResult:
        pid = _arg_int(args, "pid", None, required=True)
        assert pid is not None
        if pid <= 0:
            raise ToolError("pid must be a positive integer")
        if pid == os.getpid():
            raise ToolError("refusing to kill the host agent itself")
        if IS_WINDOWS:
            result = await self._run(["taskkill", "/PID", str(pid), "/F"])
            if result is None or result[0] != 0:
                raise ToolError(f"failed to kill process {pid}")
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                raise ToolError(f"no such process: {pid}") from None
        return text_result(call_id, f"Sent termination signal to process {pid}")

    # ------------------------------------------------------------------ system

    async def _uptime(self) -> str | None:
        if sys.platform.startswith("linux"):
            with contextlib.suppress(OSError, ValueError, IndexError):
                return self._fmt_duration(float(Path("/proc/uptime").read_text().split()[0]))
        if IS_WINDOWS:
            with contextlib.suppress(Exception):
                import ctypes

                ticks = ctypes.windll.kernel32.GetTickCount64  # type: ignore[attr-defined]
                ticks.restype = ctypes.c_ulonglong
                return self._fmt_duration(ticks() / 1000)
            return None
        result = await self._run(["sysctl", "-n", "kern.boottime"])
        if result:
            match = re.search(r"sec\s*=\s*(\d+)", result[1])
            if match:
                return self._fmt_duration(time.time() - int(match.group(1)))
        result = await self._run(["uptime"])
        return result[1].strip() if result else None

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        seconds = int(seconds)
        days, rest = divmod(seconds, 86400)
        hours, rest = divmod(rest, 3600)
        return f"{days}d {hours}h {rest // 60}m"

    async def _memory(self) -> tuple[int, int] | None:
        """Return (total, available) bytes."""
        if sys.platform.startswith("linux"):
            with contextlib.suppress(OSError, ValueError):
                info: dict[str, int] = {}
                for line in Path("/proc/meminfo").read_text().splitlines():
                    key, _, value = line.partition(":")
                    parts = value.split()
                    if parts:
                        info[key] = int(parts[0]) * 1024
                return info["MemTotal"], info.get("MemAvailable", info.get("MemFree", 0))
            return None
        if IS_WINDOWS:
            with contextlib.suppress(Exception):
                import ctypes

                class MemStatus(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]

                status = MemStatus()
                status.dwLength = ctypes.sizeof(MemStatus)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                    return status.ullTotalPhys, status.ullAvailPhys
            return None
        total_res = await self._run(["sysctl", "-n", "hw.memsize"])
        if not total_res:
            return None
        with contextlib.suppress(ValueError):
            total = int(total_res[1].strip())
            available = 0
            vm = await self._run(["vm_stat"])
            if vm:
                page = re.search(r"page size of (\d+)", vm[1])
                page_size = int(page.group(1)) if page else 4096
                for key in ("Pages free", "Pages inactive", "Pages speculative"):
                    match = re.search(rf"{key}:\s+(\d+)", vm[1])
                    if match:
                        available += int(match.group(1)) * page_size
            return total, available
        return None

    async def _system_status(self, call_id: str, args: dict[str, Any], principal: str) -> RemoteResult:
        lines = [
            f"platform: {platform.platform()}",
            f"system: {platform.system()} {platform.release()}",
            f"machine: {platform.machine()}",
            f"cpuCount: {os.cpu_count()}",
        ]
        with contextlib.suppress(Exception):
            if hasattr(os, "getloadavg"):
                load = os.getloadavg()
                lines.append(f"loadAverage: {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}")
        with contextlib.suppress(Exception):
            uptime = await self._uptime()
            if uptime:
                lines.append(f"uptime: {uptime}")
        with contextlib.suppress(Exception):
            memory = await self._memory()
            if memory:
                total, available = memory
                lines.append(f"memory: total {_human(total)}, available {_human(available)}")
        with contextlib.suppress(Exception):
            anchor = Path(self.cwd).anchor or "/"
            usage = await asyncio.to_thread(shutil.disk_usage, anchor)
            lines.append(
                f"disk ({anchor}): total {_human(usage.total)}, used {_human(usage.used)}, "
                f"free {_human(usage.free)}"
            )
        with contextlib.suppress(Exception):
            gpu = await self._run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total,memory.used,utilization.gpu",
                    "--format=csv,noheader",
                ]
            )
            if gpu and gpu[0] == 0 and gpu[1].strip():
                for index, row in enumerate(gpu[1].strip().splitlines()):
                    lines.append(f"gpu{index}: {row.strip()}")
        return text_result(call_id, "\n".join(lines))


__all__ = ["BuiltinExecutor"]
