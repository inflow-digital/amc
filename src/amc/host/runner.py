"""Build the executor(s) from ``host.json`` and run the device agent."""

from __future__ import annotations

import asyncio
import logging

from .client import HostClient
from .config import HostConfig
from .executors.base import Executor
from .safe_env import safe_environment

logger = logging.getLogger("amc.host")

EXIT_REJECTED = 3
"""Exit status when the relay rejects this device; service managers must not restart on it."""


def build_executors(config: HostConfig) -> tuple[Executor, Executor | None]:
    from .confine import Confinement
    from .executors.builtin import BuiltinExecutor

    env = safe_environment(config.env_passthrough)
    guard = Confinement(config.roots, config.cwd, config.protected)
    builtin = BuiltinExecutor(cwd=config.cwd, env=env, is_protected=guard.is_protected)
    if config.executor == "builtin":
        return builtin, None
    if config.executor == "stdio":
        from .executors.stdio import StdioMcpExecutor

        stdio_env = safe_environment(config.env_passthrough, extra=config.stdio_env)
        return StdioMcpExecutor(command=config.stdio_command, args=config.stdio_args, env=stdio_env,
                                cwd=config.cwd), builtin
    raise ValueError(f"unknown executor {config.executor!r} (use builtin or stdio)")


async def run_host(config: HostConfig) -> None:
    executor, status_executor = build_executors(config)
    client = HostClient(config, executor, status_executor=status_executor)
    try:
        await client.run()
    finally:
        await client.shutdown()
        await executor.shutdown()
        if status_executor is not None:
            await status_executor.shutdown()


def main(config: HostConfig) -> int:
    from .client import HostRejected

    try:
        asyncio.run(run_host(config))
    except HostRejected as exc:
        logger.error("%s", exc)
        return EXIT_REJECTED
    except KeyboardInterrupt:
        pass
    return 0
