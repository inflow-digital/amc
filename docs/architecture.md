# AMC architecture

AMC has three moving parts, all in one Python package (`amc-commander`, command `amc`):

| Component | Code | Runs where | Job |
|---|---|---|---|
| **Relay** | `src/amc/relay/` | one machine you choose (`amc relay serve`) | the only thing AI apps talk to: authenticates, authorizes, routes and audits every call. It never reads files or runs commands itself. |
| **Device agent (host)** | `src/amc/host/` | every machine AI may use (`amc host run`) | keeps one outbound WebSocket to the relay, re-checks each call against its own folder roots, and executes it with an *executor*. |
| **Core** | `src/amc/core/` | shared | wire contracts (`contracts.py`), tool catalog and policy (`policy.py`), call broker (`broker.py`). |

The CLI (`src/amc/cli.py`) and the service installers (`src/amc/service.py`) glue these
together; see [cli.md](cli.md).

```
 AI app ── HTTPS/HTTP ──> relay                                     device
           POST /mcp       ├─ auth: agent key or OAuth token         (outbound only)
                           ├─ policy: key × device × action × path      │
                           ├─ runtime: limits, timeout, in-flight cap   │
                           ├─ broker ──── WS "call" ───────────────────>├─ confinement (roots, symlinks resolved)
                           │         <─── WS "result" ──────────────────┤─ executor: builtin | stdio MCP server
                           └─ audit.jsonl (decision only, no content)
```

## Relay surfaces

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /mcp` | agent key or OAuth access token | MCP Streamable HTTP (stateless, JSON responses) |
| `/.well-known/oauth-*`, `/authorize`, `/token`, `/register`, `/revoke` | — | OAuth 2.1 authorization server (enabled when a public URL is set) |
| `GET/POST /oauth/approve` | an AMC agent key typed by the user | consent page |
| `POST /api/v1/pair` | one-time pairing code | turns a code into a device identity (`device_id` + device token) |
| `GET /api/v1/devices`, `GET /api/v1/devices/{id}` | agent key | devices visible to the caller |
| `POST /api/v1/execute` | agent key | the same governed call path as the MCP tools |
| `GET /api/v1/audit` | agent key | the caller's own audit events |
| `WS /devices/connect` | device token | device agents |
| `GET /livez` | none | liveness; reveals nothing |

State lives in the AMC config dir: `relay.json` (listen address, public URL, limits,
clients with hashed keys and grants, devices with hashed tokens, pending pairing codes),
`relay-oauth.json` (OAuth clients and hashed tokens) and `audit.jsonl`. The relay re-reads
`relay.json` when it changes, so `amc key add` or `amc relay pair` in another terminal takes
effect without a restart.

## Device protocol (version 1)

JSON text frames over one WebSocket that the **device opens**:

```
device -> relay   GET /devices/connect?device_id=<id>&agent_version=<v>
                  Authorization: Bearer <device token>
relay  -> device  {"type": "hello_ack", "device_id": ..., "protocol": 1, ...}
relay  -> device  {"type": "call",   "call":   RemoteCall}
device -> relay   {"type": "result", "result": RemoteResult}
either            {"type": "ping"} / {"type": "pong"}
```

- `RemoteCall` = `call_id`, `device_id`, `tool`, `arguments`, `principal` (the calling key),
  `action` (its action class). `RemoteResult` = `call_id`, `ok`, `result`, `error`
  (see `src/amc/core/contracts.py`).
- The device checks that `hello_ack` names its own `device_id` and protocol 1, otherwise it
  disconnects.
- A device can only answer calls addressed to its own `device_id`; results for other devices
  are rejected and audited.
- Rejection close codes: `4401` bad or missing token (also sent when a device is removed),
  `4400` malformed `device_id`, `4403` token not bound to that `device_id`. After accept:
  `4409` superseded by a newer connection for the same device, `1008` too many (8)
  protocol violations. On `4401`/`4403` the device agent exits with status 3 (the systemd
  unit does not restart on it, since re-pairing is needed); on anything else it reconnects with jittered exponential back-off
  (1 s → 30 s).
- Limits (in `relay.json`, `limits`): call timeout 60 s, 16 in-flight calls per device,
  256 KiB arguments, 16 MiB results.

## Tool catalog

Every device tool belongs to exactly one action class (`src/amc/core/policy.py`,
`TOOL_ACTIONS`). A tool that is not in the catalog is denied (fail closed). The relay
exposes each tool to MCP clients as `amc_<tool>` with a `device_id` argument.

| Action class | Tools | Access level needed |
|---|---|---|
| `read` | `read_file`, `read_multiple_files`, `list_directory`, `get_file_info`, `start_search`, `get_more_search_results`, `stop_search`, `list_searches`, `list_processes`, `list_sessions`, `read_process_output`, `system_status` | `read` |
| `write` | `write_file`, `edit_block`, `create_directory`, `move_file` | `standard` |
| `destructive` | `kill_process`, `force_terminate` | `full` |
| `admin` | `start_process`, `interact_with_process` (arbitrary commands; not confined by folder rules) | `full` |

Relay-only MCP tools: `remote_devices`, `remote_device_status`, `remote_audit` (the caller's
own events), `remote_call` (invoke any catalog tool by name) and `amc_view_image` (returns an
image file as MCP image content; built on `get_file_info` + `read_file`).

Tool and argument names follow [DesktopCommanderMCP](https://github.com/wonderwhy-er/DesktopCommanderMCP)
so either implementation can serve as a device backend.

## Authorization

For each call the relay builds a `Principal` from the credential (`key:<name>`) and asks
`RemoteExecutionPolicy.authorize`:

1. classify the tool → action class (unknown → deny);
2. find a grant of that key that covers the device, the action class and the tool;
3. if the key has path prefixes, every path argument (`path`, `file_path`, `source`,
   `destination`, `paths[]`) must lie under one of them; `..` is rejected. This is a
   lexical pre-check;
4. the device then resolves `~`, relative paths and symlinks and checks every path argument
   against its own roots (`src/amc/host/confine.py`) before any executor sees it.

Process handles and search sessions of the built-in executor are private to the key that
started them: another key cannot read, feed or kill them.

## Authentication paths

Both paths end at the same principal — an AMC agent key — so revoking a key revokes
everything issued for it.

1. **Agent key as bearer token** — `Authorization: Bearer amck_...`. Used by Claude Code,
   Codex, scripts and any client that can send a header (`amc connect claude|codex|generic`).
2. **OAuth 2.1 with consent-by-key** — for hosted apps that only speak OAuth (Claude.ai /
   Claude Desktop custom connectors, ChatGPT connectors). Enabled when the relay has a public
   URL (`amc relay url --public-url https://...`). The app discovers the authorization server,
   registers itself dynamically, and starts an authorization-code flow with PKCE. The consent
   page (`/oauth/approve`) asks the user for an AMC agent key once; the issued access token
   (1 hour) and rotating refresh token (30 days) act as that key. Five wrong keys cancel the
   request.

Credential shapes: `amck_` agent key, `amcd_` device token, `amco_` OAuth access token,
`amcr_` OAuth refresh token. Only SHA-256 hashes are stored.

## Executors

A device runs calls with one executor (`amc host pair --executor ...`):

- **`builtin`** (default, `src/amc/host/executors/builtin.py`) — pure-Python implementation
  of the whole catalog for Linux, macOS and Windows (commands run in `$SHELL`/`/bin/sh`, or
  PowerShell on Windows).
- **`stdio`** — forwards calls to any local stdio MCP server, e.g. DesktopCommanderMCP:

  ```sh
  amc host pair https://relay.example.com CODE --executor stdio \
      --stdio-command npx --stdio-arg -y --stdio-arg @wonderwhy-er/desktop-commander
  ```

  The server is started lazily and restarted after a failure; it gets the same allowlisted
  environment as built-in commands. `system_status` is still answered by the built-in
  executor. Relay policy and device confinement apply exactly as with `builtin`; per-key
  isolation of process and search handles is only as good as the backend's.

Executors never inherit the device agent's full environment (`src/amc/host/safe_env.py`):
only ordinary variables (`PATH`, `HOME`, locale, Windows system paths, ...) plus names listed
in `env_passthrough` in `host.json`; `AMC_*` variables are always dropped.

## Pairing

`amc relay pair` stores the SHA-256 of a random 8-character code (`ABCD-2345`, 15 minutes,
single use). The new machine posts it to `/api/v1/pair` and receives a `device_id` and a
device token, which it saves in `host.json` (mode 0600). Twenty failed redemptions per
minute lock pairing for the rest of that minute.
