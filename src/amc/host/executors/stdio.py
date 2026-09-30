"""Executor that forwards calls to any local stdio MCP server (e.g. DesktopCommanderMCP).

The server runs with an allowlisted environment (see ``amc.host.safe_env``), so it
never sees the host's device token or unrelated credentials.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from amc.core import RemoteResult

logger = logging.getLogger("amc.host.stdio")


class StdioMcpExecutor:
    def __init__(self, *, command: str, args: list[str], env: dict[str, str], cwd: str | None) -> None:
        if not command:
            raise ValueError("stdio executor needs a command (amc host pair --stdio-command ...)")
        self._params = StdioServerParameters(command=command, args=args, env=env, cwd=cwd)
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def start(self) -> None:
        if self._session is not None:
            return
        stack = AsyncExitStack()
        try:
            read_stream, write_stream = await stack.enter_async_context(stdio_client(self._params))
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
        except BaseException:
            await stack.aclose()
            raise
        self._stack, self._session = stack, session
        logger.info("local MCP server ready: %s", self._params.command)

    async def execute(self, *, call_id: str, tool: str, arguments: dict[str, Any], principal_id: str) -> RemoteResult:
        try:
            await self.start()
            assert self._session is not None
            response = await self._session.call_tool(tool, arguments)
            return RemoteResult(
                call_id=call_id,
                ok=not response.isError,
                result=response.model_dump(mode="json"),
                error="tool returned an error" if response.isError else None,
            )
        except Exception as exc:  # noqa: BLE001 - backend failures become protocol-safe errors
            logger.warning("local MCP call %s failed: %s", tool, type(exc).__name__)
            await self._reset()
            return RemoteResult(call_id=call_id, ok=False, error=f"{type(exc).__name__}: {exc}")

    async def _reset(self) -> None:
        stack, self._stack, self._session = self._stack, None, None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:  # noqa: BLE001
                pass

    async def shutdown(self) -> None:
        await self._reset()
