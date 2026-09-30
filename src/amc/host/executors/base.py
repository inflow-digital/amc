"""Executor contract.

An executor receives one already-authorized tool call and returns a ``RemoteResult``
whose ``result`` is an MCP ``CallToolResult``-shaped dict::

    {"content": [{"type": "text", "text": "..."}], "isError": false}

Image reads add ``{"type": "image", "data": <base64>, "mimeType": "image/png"}``.
Tool names and argument names follow the catalog in ``amc.core.policy.TOOL_ACTIONS``
(DesktopCommander-compatible argument names). ``principal_id`` identifies the caller
so executors can keep process/search handles private to the principal that created them.
"""

from __future__ import annotations

from typing import Any, Protocol

from amc.core import RemoteResult


class Executor(Protocol):
    async def start(self) -> None: ...

    async def execute(
        self, *, call_id: str, tool: str, arguments: dict[str, Any], principal_id: str
    ) -> RemoteResult: ...

    async def shutdown(self) -> None: ...


def text_result(call_id: str, text: str, *, is_error: bool = False) -> RemoteResult:
    return RemoteResult(
        call_id=call_id,
        ok=not is_error,
        result={"content": [{"type": "text", "text": text}], "isError": is_error},
        error=text if is_error else None,
    )
