"""Tool catalog and relay-side authorization.

Every tool a device can run is classified into exactly one action class. Unknown
tools are denied (fail closed). A grant says which devices, action classes, tools
and (optionally) path prefixes a principal may use; ``"*"`` matches any device or
tool. Path prefixes here are a *lexical* pre-check; the host additionally enforces
its own filesystem roots after resolving symlinks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from .contracts import ActionClass, Principal

TOOL_ACTIONS: dict[str, ActionClass] = {
    # read
    "read_file": ActionClass.READ,
    "read_multiple_files": ActionClass.READ,
    "list_directory": ActionClass.READ,
    "get_file_info": ActionClass.READ,
    "start_search": ActionClass.READ,
    "get_more_search_results": ActionClass.READ,
    "stop_search": ActionClass.READ,
    "list_searches": ActionClass.READ,
    "list_processes": ActionClass.READ,
    "list_sessions": ActionClass.READ,
    "read_process_output": ActionClass.READ,
    "system_status": ActionClass.READ,
    # write
    "write_file": ActionClass.WRITE,
    "edit_block": ActionClass.WRITE,
    "create_directory": ActionClass.WRITE,
    "move_file": ActionClass.WRITE,
    # destructive
    "kill_process": ActionClass.DESTRUCTIVE,
    "force_terminate": ActionClass.DESTRUCTIVE,
    # admin (arbitrary command execution: not confined by path rules)
    "start_process": ActionClass.ADMIN,
    "interact_with_process": ActionClass.ADMIN,
}

PATH_ARGUMENT_KEYS = ("path", "file_path", "source", "destination")
PATH_LIST_ARGUMENT_KEYS = ("paths",)

ACCESS_LEVELS: dict[str, frozenset[ActionClass]] = {
    "read": frozenset({ActionClass.READ}),
    "standard": frozenset({ActionClass.READ, ActionClass.WRITE}),
    "full": frozenset(ActionClass),
}


class PolicyDenied(PermissionError):
    pass


def classify_action(tool: str) -> ActionClass:
    try:
        return TOOL_ACTIONS[tool]
    except KeyError:
        raise PolicyDenied(f"tool has no classified action: {tool}") from None


def path_arguments(arguments: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in PATH_ARGUMENT_KEYS:
        value = arguments.get(key)
        if isinstance(value, str):
            values.append(value)
    for key in PATH_LIST_ARGUMENT_KEYS:
        raw = arguments.get(key)
        if isinstance(raw, list):
            values.extend(value for value in raw if isinstance(value, str))
    return values


def _is_windows_path(path: str) -> bool:
    return "\\" in path or (len(path) >= 2 and path[1] == ":")


def _parts(path: str) -> tuple[str, ...]:
    if _is_windows_path(path):
        parts = tuple(part.casefold() for part in PureWindowsPath(path).parts)
    else:
        parts = PurePosixPath(path).parts
    if ".." in parts:
        raise PolicyDenied("path traversal is not allowed")
    return parts


def path_inside(path: str, prefix: str) -> bool:
    parts = _parts(path)
    base = _parts(prefix)
    return parts[: len(base)] == base


@dataclass(frozen=True)
class PolicyGrant:
    principal_id: str
    device_ids: frozenset[str]
    actions: frozenset[ActionClass]
    tools: frozenset[str] = frozenset({"*"})
    path_prefixes: tuple[str, ...] = field(default_factory=tuple)

    def covers_device(self, device_id: str) -> bool:
        return "*" in self.device_ids or device_id in self.device_ids

    def covers_tool(self, tool: str) -> bool:
        return "*" in self.tools or tool in self.tools


class RemoteExecutionPolicy:
    def __init__(self, grants: list[PolicyGrant] | None = None) -> None:
        self._grants = list(grants or [])

    def grants_for(self, principal_id: str) -> list[PolicyGrant]:
        return [grant for grant in self._grants if grant.principal_id == principal_id]

    def can_see_device(self, principal: Principal, device_id: str) -> bool:
        return any(grant.covers_device(device_id) for grant in self.grants_for(principal.principal_id))

    def allowed_tools(self, principal: Principal) -> set[str]:
        tools: set[str] = set()
        for grant in self.grants_for(principal.principal_id):
            for tool, action in TOOL_ACTIONS.items():
                if action in grant.actions and grant.covers_tool(tool):
                    tools.add(tool)
        return tools

    def authorize(
        self,
        *,
        principal: Principal,
        device_id: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> ActionClass:
        return self.authorize_grant(principal=principal, device_id=device_id, tool=tool, arguments=arguments)[0]

    def authorize_grant(
        self,
        *,
        principal: Principal,
        device_id: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> tuple[ActionClass, PolicyGrant]:
        action = classify_action(tool)
        paths = path_arguments(arguments)
        for grant in self.grants_for(principal.principal_id):
            if not grant.covers_device(device_id):
                continue
            if action not in grant.actions or not grant.covers_tool(tool):
                continue
            if grant.path_prefixes and any(
                not any(path_inside(path, prefix) for prefix in grant.path_prefixes) for path in paths
            ):
                continue
            return action, grant
        raise PolicyDenied("no grant for principal × device × action × resource scope")
