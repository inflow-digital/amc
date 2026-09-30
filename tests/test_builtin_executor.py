from __future__ import annotations

import asyncio
import base64
import os
import re
import sys
import time
from pathlib import Path

import pytest

from amc.core import TOOL_ACTIONS
from amc.host.executors.builtin import BuiltinExecutor

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

ALICE = "alice"
BOB = "bob"


@pytest.fixture
async def ex(tmp_path: Path):
    executor = BuiltinExecutor(cwd=str(tmp_path), env=dict(os.environ))
    await executor.start()
    yield executor
    await executor.shutdown()


async def call(ex: BuiltinExecutor, tool: str, principal: str = ALICE, **arguments):
    return await ex.execute(call_id="c1", tool=tool, arguments=arguments, principal_id=principal)


def text(result) -> str:
    return "\n".join(block["text"] for block in result.result["content"] if block["type"] == "text")


def py_cmd(code: str) -> str:
    exe = sys.executable
    if sys.platform == "win32":
        return f"& '{exe}' -u -c \"{code}\""
    return f"'{exe}' -u -c \"{code}\""


def pid_of(result) -> int:
    match = re.search(r"PID (\d+)", text(result))
    assert match, text(result)
    return int(match.group(1))


def test_every_catalog_tool_is_implemented(tmp_path: Path):
    executor = BuiltinExecutor(cwd=str(tmp_path), env={})
    assert set(executor._handlers) == set(TOOL_ACTIONS)


async def test_unknown_tool(ex):
    result = await call(ex, "format_disk")
    assert not result.ok
    assert result.result["isError"]
    assert "Unknown tool" in text(result)


async def test_bad_arguments(ex):
    result = await call(ex, "read_file")
    assert not result.ok and "path" in text(result)
    result = await call(ex, "read_file", path="x.txt", offset="abc")
    assert not result.ok and "offset" in text(result)


async def test_write_read_file_lines(ex, tmp_path: Path):
    content = "".join(f"line{i}\n" for i in range(10))
    result = await call(ex, "write_file", path="sub/a.txt", content=content)
    assert result.ok, text(result)
    assert "10 lines" in text(result)
    assert (tmp_path / "sub" / "a.txt").read_text() == content

    result = await call(ex, "read_file", path="sub/a.txt", offset=2, length=3)
    out = text(result)
    assert "total: 10 lines" in out
    assert "line2\nline3\nline4\n" in out and "line5" not in out

    result = await call(ex, "read_file", path=str(tmp_path / "sub" / "a.txt"), offset=-2)
    out = text(result)
    assert "line8\nline9" in out and "line7" not in out

    await call(ex, "write_file", path="sub/a.txt", content="tail\n", mode="append")
    assert (tmp_path / "sub" / "a.txt").read_text().endswith("line9\ntail\n")

    result = await call(ex, "write_file", path="sub/a.txt", content="x", mode="bogus")
    assert not result.ok


async def test_read_missing_and_binary(ex, tmp_path: Path):
    assert not (await call(ex, "read_file", path="nope.txt")).ok
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    result = await call(ex, "read_file", path="blob.bin")
    assert not result.ok and "binary" in text(result)


async def test_read_image(ex, tmp_path: Path):
    (tmp_path / "pic.png").write_bytes(PNG_1PX)
    result = await call(ex, "read_file", path="pic.png")
    assert result.ok
    blocks = result.result["content"]
    assert blocks[0]["type"] == "text" and "image/png" in blocks[0]["text"]
    assert blocks[1] == {"type": "image", "data": base64.b64encode(PNG_1PX).decode(), "mimeType": "image/png"}


async def test_read_image_too_large(tmp_path: Path):
    (tmp_path / "pic.png").write_bytes(PNG_1PX)
    executor = BuiltinExecutor(cwd=str(tmp_path), env={}, max_read_bytes=10)
    result = await call(executor, "read_file", path="pic.png")
    assert not result.ok and "too large" in text(result)


async def test_read_multiple_files(ex, tmp_path: Path):
    (tmp_path / "a.txt").write_text("alpha\n")
    (tmp_path / "pic.png").write_bytes(PNG_1PX)
    result = await call(ex, "read_multiple_files", paths=["a.txt", "missing.txt", "pic.png"])
    assert result.ok
    out = text(result)
    assert "--- a.txt ---" in out and "alpha" in out
    assert "--- missing.txt ---" in out and "Error" in out
    assert any(block["type"] == "image" for block in result.result["content"])


async def test_edit_block(ex, tmp_path: Path):
    target = tmp_path / "e.txt"
    target.write_text("foo bar foo\n")
    result = await call(ex, "edit_block", file_path="e.txt", old_string="foo", new_string="baz")
    assert not result.ok
    assert "found 2" in text(result)
    assert target.read_text() == "foo bar foo\n"

    result = await call(
        ex, "edit_block", file_path="e.txt", old_string="foo", new_string="baz", expected_replacements=2
    )
    assert result.ok, text(result)
    assert target.read_text() == "baz bar baz\n"

    result = await call(ex, "edit_block", file_path="e.txt", old_string="zzz", new_string="y")
    assert not result.ok and "0 occurrences" in text(result)


async def test_create_move_list(ex, tmp_path: Path):
    assert (await call(ex, "create_directory", path="d1/d2/d3")).ok
    assert (await call(ex, "create_directory", path="d1/d2/d3")).ok
    (tmp_path / "d1" / "f.txt").write_text("x")
    (tmp_path / "d1" / "d2" / "d3" / "deep.txt").write_text("x")

    result = await call(ex, "move_file", source="d1/f.txt", destination="d1/d2/g.txt")
    assert result.ok, text(result)
    assert (tmp_path / "d1" / "d2" / "g.txt").exists()
    assert not (await call(ex, "move_file", source="d1/nothing", destination="z")).ok

    out = text(await call(ex, "list_directory", path="d1", depth=2))
    lines = out.splitlines()
    assert "[DIR] d2" in lines
    assert "[FILE] d2/g.txt" in lines
    assert "[DIR] d2/d3" in lines
    assert not any("deep.txt" in line for line in lines)

    out = text(await call(ex, "list_directory", path="d1", depth=1))
    assert out.splitlines() == ["[DIR] d2"]


async def test_list_directory_truncates(ex, tmp_path: Path):
    many = tmp_path / "many"
    many.mkdir()
    for i in range(1005):
        (many / f"f{i:04}").write_text("")
    out = text(await call(ex, "list_directory", path="many"))
    assert "truncated" in out
    assert len([line for line in out.splitlines() if line.startswith("[FILE]")]) == 1000


async def test_get_file_info(ex, tmp_path: Path):
    (tmp_path / "info.txt").write_bytes(b"one\ntwo\nthree\n")
    out = text(await call(ex, "get_file_info", path="info.txt"))
    lines = out.splitlines()
    assert "size: 14" in lines
    assert "isFile: true" in lines and "isDirectory: false" in lines
    assert "lineCount: 3" in lines
    assert any(line.startswith("modified: ") for line in lines)
    assert any(re.fullmatch(r"permissions: [0-7]{3}", line) for line in lines)

    out = text(await call(ex, "get_file_info", path="."))
    assert "isDirectory: true" in out and "lineCount" not in out
    assert not (await call(ex, "get_file_info", path="missing")).ok


def _session_id(result) -> str:
    match = re.search(r"sessionId: (\w+)", text(result))
    assert match, text(result)
    return match.group(1)


async def _wait_search(ex, sid, principal=ALICE):
    for _ in range(100):
        out = text(await call(ex, "get_more_search_results", principal, sessionId=sid))
        if "Search complete: yes" in out:
            return out
        await asyncio.sleep(0.05)
    raise AssertionError("search did not finish")


async def test_search_files_and_content(ex, tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Alpha.py").write_text("import os\nNEEDLE = 1\n")
    (tmp_path / "src" / "beta.txt").write_text("nothing\nneedle here\n")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "alpha_secret.py").write_text("NEEDLE\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "alpha.js").write_text("needle\n")
    (tmp_path / "bin.dat").write_bytes(b"\x00needle")

    result = await call(ex, "start_search", path=".", pattern="alpha")
    assert result.ok, text(result)
    out = await _wait_search(ex, _session_id(result))
    assert "Alpha.py" in out
    assert "alpha_secret" not in out and "alpha.js" not in out

    result = await call(ex, "start_search", path=".", pattern="alpha", includeHidden=True)
    out = await _wait_search(ex, _session_id(result))
    assert "alpha_secret.py" in out and "alpha.js" not in out

    result = await call(ex, "start_search", path=".", pattern="*.TXT")
    out = await _wait_search(ex, _session_id(result))
    assert "beta.txt" in out and "Alpha.py" not in out

    result = await call(ex, "start_search", path=".", pattern="needle", searchType="content", contextLines=0)
    out = await _wait_search(ex, _session_id(result))
    assert re.search(r"Alpha\.py:2: NEEDLE = 1", out)
    assert re.search(r"beta\.txt:2: needle here", out)
    assert "bin.dat" not in out and "alpha_secret" not in out

    result = await call(
        ex, "start_search", path=".", pattern="needle", searchType="content",
        ignoreCase=False, filePattern="*.py|*.txt",
    )
    out = await _wait_search(ex, _session_id(result))
    assert "beta.txt:2" in out and "Alpha.py" not in out

    bad = await call(ex, "start_search", path=".", pattern="(", searchType="content")
    assert not bad.ok and "regular expression" in text(bad)
    literal = await call(ex, "start_search", path=".", pattern="(", searchType="content", literalSearch=True)
    assert literal.ok


async def test_search_paging_stop_and_list(ex, tmp_path: Path):
    for i in range(30):
        (tmp_path / f"item{i:02}.log").write_text("")
    result = await call(ex, "start_search", path=".", pattern="item")
    sid = _session_id(result)
    await _wait_search(ex, sid)
    page = text(await call(ex, "get_more_search_results", sessionId=sid, offset=10, length=5))
    assert "Showing results 10-15 of 30" in page
    assert "item10.log" in page and "item14.log" in page and "item15.log" not in page
    assert "offset=15" in page

    limited = await call(ex, "start_search", path=".", pattern="item", maxResults=7)
    out = await _wait_search(ex, _session_id(limited))
    assert "of 7" in out

    listing = text(await call(ex, "list_searches"))
    assert sid in listing
    assert (await call(ex, "stop_search", sessionId=sid)).ok


async def test_search_principal_isolation(ex, tmp_path: Path):
    (tmp_path / "a.txt").write_text("x")
    sid = _session_id(await call(ex, "start_search", path=".", pattern="a"))
    for tool in ("get_more_search_results", "stop_search"):
        result = await call(ex, tool, BOB, sessionId=sid)
        assert not result.ok and "not found" in text(result)
    assert sid not in text(await call(ex, "list_searches", BOB))
    assert (await call(ex, "get_more_search_results", ALICE, sessionId=sid)).ok


async def test_start_process_completes(ex, tmp_path: Path):
    result = await call(ex, "start_process", command=py_cmd("print(42); import os; print(os.getcwd())"))
    assert result.ok, text(result)
    out = text(result)
    assert "Process started with PID" in out
    assert "42" in out
    assert "exit code: 0" in out
    assert str(tmp_path.name) in out

    result = await call(ex, "start_process", command=py_cmd("import sys; sys.exit(3)"))
    assert "exit code: 3" in text(result)


REPL = (
    "import sys\n"
    "print('ready', flush=True)\n"
    "for line in sys.stdin:\n"
    "    print('echo:' + line.strip(), flush=True)\n"
    "    if line.strip() == 'quit': break\n"
)


async def _start_repl(ex, tmp_path: Path, principal=ALICE) -> int:
    script = tmp_path / "repl.py"
    script.write_text(REPL)
    cmd = f"& '{sys.executable}' -u '{script}'" if sys.platform == "win32" else (
        f"'{sys.executable}' -u '{script}'"
    )
    result = await call(ex, "start_process", principal, command=cmd, timeout_ms=1500)
    assert result.ok, text(result)
    assert "still running" in text(result)
    return pid_of(result)


async def test_long_running_process_interaction(ex, tmp_path: Path):
    pid = await _start_repl(ex, tmp_path)

    result = await call(ex, "interact_with_process", pid=pid, input="hello", timeout_ms=5000)
    assert result.ok, text(result)
    assert "echo:hello" in text(result)

    result = await call(ex, "interact_with_process", pid=pid, input="again", timeout_ms=5000)
    assert "echo:again" in text(result) and "echo:hello" not in text(result)

    # nothing new: read returns quickly with no output
    started = time.monotonic()
    result = await call(ex, "read_process_output", pid=pid, timeout_ms=300)
    assert "(no output)" in text(result) and "running" in text(result)
    assert time.monotonic() - started < 3

    # absolute line reads and tail reads do not consume
    whole = text(await call(ex, "read_process_output", pid=pid, offset=1, length=10))
    assert "echo:hello" in whole and "echo:again" in whole
    tail = text(await call(ex, "read_process_output", pid=pid, offset=-1))
    assert "echo:again" in tail and "echo:hello" not in tail

    sessions = text(await call(ex, "list_sessions"))
    assert f"PID: {pid}" in sessions and "running" in sessions

    result = await call(ex, "interact_with_process", pid=pid, input="quit", timeout_ms=5000)
    assert "echo:quit" in text(result)
    for _ in range(100):
        out = text(await call(ex, "read_process_output", pid=pid, timeout_ms=100))
        if "exit code: 0" in out:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError(out)


async def test_force_terminate(ex, tmp_path: Path):
    pid = await _start_repl(ex, tmp_path)
    result = await call(ex, "force_terminate", pid=pid)
    assert result.ok, text(result)
    out = text(await call(ex, "list_sessions"))
    assert f"PID: {pid}" in out and "exited" in out
    assert not (await call(ex, "interact_with_process", pid=pid, input="x")).ok
    assert not (await call(ex, "force_terminate", pid=999999999)).ok


async def test_process_principal_isolation(ex, tmp_path: Path):
    pid = await _start_repl(ex, tmp_path, principal=ALICE)
    for tool, extra in (
        ("read_process_output", {}),
        ("interact_with_process", {"input": "hi"}),
        ("force_terminate", {}),
    ):
        result = await call(ex, tool, BOB, pid=pid, **extra)
        assert not result.ok and "not found" in text(result), tool
    assert f"PID: {pid}" not in text(await call(ex, "list_sessions", BOB))
    # still alive for its owner
    assert "running" in text(await call(ex, "read_process_output", ALICE, pid=pid, timeout_ms=100))


async def test_shutdown_kills_sessions(tmp_path: Path):
    executor = BuiltinExecutor(cwd=str(tmp_path), env=dict(os.environ))
    pid = await _start_repl(executor, tmp_path)
    proc = executor._processes[pid].proc
    await executor.shutdown()
    assert proc.returncode is not None


async def test_list_and_kill_processes(ex, tmp_path: Path):
    result = await call(ex, "list_processes")
    assert result.ok, text(result)
    assert str(os.getpid()) in text(result)

    pid = await _start_repl(ex, tmp_path)
    assert (await call(ex, "kill_process", pid=pid)).ok
    for _ in range(100):
        if "exited" in text(await call(ex, "list_sessions")):
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("process not killed")
    assert not (await call(ex, "kill_process", pid=os.getpid())).ok


async def test_system_status(ex):
    result = await call(ex, "system_status")
    assert result.ok
    out = text(result)
    assert "platform:" in out and "cpuCount:" in out and "disk (" in out
