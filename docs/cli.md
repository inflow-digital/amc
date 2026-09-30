# `amc` command reference

One program, `amc`, does everything. Configuration lives in the AMC config dir
(`~/.config/amc` on Linux, `~/Library/Application Support/amc` on macOS,
`%APPDATA%\amc` on Windows; `AMC_HOME` overrides it).

| File | Written by | Contents |
|---|---|---|
| `relay.json` (0600) | `amc relay init` / relay itself | listen address, public URL, clients (hashed keys + grants), devices (hashed tokens), pending pairing codes |
| `relay-oauth.json` (0600) | relay | OAuth clients / hashed tokens issued to Claude.ai, ChatGPT, ... |
| `audit.jsonl` | relay | one JSON line per decision (never arguments, results or secrets) |
| `host.json` (0600) | `amc host pair` | relay URL, device id + token, allowed roots, executor |
| `logs/` | services | `relay.log`, `host.log` when run as a service |

## Quick start (everything on one machine)

```
amc up            # relay + this machine as a device + background services + agent keys
```

`amc up` is idempotent. It:
1. runs `amc relay init` if `relay.json` does not exist;
2. installs and starts the relay service (`amc service install relay`);
3. pairs this machine to it (`amc host pair` with a fresh code) unless `host.json` exists;
4. installs and starts the host service (`amc service install host`);
5. creates agent keys and registers AMC with every agent CLI found on PATH
   (`amc connect claude`, `amc connect codex`); prints instructions for the rest;
6. runs `amc doctor`.

Options: `--port N` (default 8790), `--public-url URL`, `--access read|standard|full`
(default `full` for the owner's own machine), `--no-service` (foreground-free setup only).

## Relay

| Command | Effect |
|---|---|
| `amc relay init [--port 8790] [--bind 127.0.0.1] [--public-url URL] [--force]` | create `relay.json` |
| `amc relay serve` | run the relay in the foreground |
| `amc relay pair [--name NAME] [--ttl 900]` | print a one-time pairing code and the exact install/pair command to run on the new machine |
| `amc relay status` | listen address, public URL, devices (online/offline), clients |
| `amc relay url --public-url URL` | set/replace the public URL (needed for Claude.ai / ChatGPT OAuth) |

## Agent keys (clients)

| Command | Effect |
|---|---|
| `amc key add NAME [--access read\|standard\|full] [--devices a,b\|*] [--paths p1,p2]` | create an agent key; printed **once** |
| `amc key list` / `amc key remove NAME` | manage keys |
| `amc connect claude\|codex\|chatgpt\|claude-web\|generic [--key NAME] [--url URL]` | create (or rotate) a key for that agent and configure it: runs `claude mcp add ...` / `codex mcp add ...` when the CLI is installed, otherwise prints what to paste |

Access levels: `read` = read-only file/process listing; `standard` = + write/edit files;
`full` = + run commands and kill processes.

## Devices (hosts)

| Command | Effect |
|---|---|
| `amc host pair RELAY_URL CODE [--name NAME] [--root PATH ...] [--executor builtin\|stdio] [--stdio-command CMD] [--stdio-arg ARG ...]` | redeem a pairing code, write `host.json` |
| `amc host run` | run the device agent in the foreground (outbound connection only) |
| `amc host status` | show `host.json` summary (never the token) and whether the relay sees this device |
| `amc device list` / `amc device remove ID` | on the relay machine |

## Services

| Command | Effect |
|---|---|
| `amc service install relay\|host` | register + start a per-user background service (systemd --user on Linux, launchd LaunchAgent on macOS, Scheduled Task at logon on Windows) |
| `amc service uninstall relay\|host` | stop + remove it (config and identity are kept) |
| `amc service status relay\|host` | running / stopped / not installed |

## Diagnostics

`amc doctor` checks: config files and permissions, relay reachable, `/livez`,
device online, a test read through the relay, agent CLI registration. Every failed
check prints the one command that fixes it.

## Uninstall

`amc service uninstall host; amc service uninstall relay; uv tool uninstall amc-commander`
(add `rm -rf <config dir>` to also forget identity and keys).
