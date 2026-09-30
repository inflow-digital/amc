# AMC — Agent MCP Commander

**Let your AI apps safely work on your own computers.**

AMC is a small, self-hosted relay that gives AI apps — Claude Code, Codex, Claude.ai,
ChatGPT or any MCP client — a single [MCP](https://modelcontextprotocol.io) endpoint for
all of your machines. Each machine runs a tiny device agent that connects *out* to the
relay, so you never open a port on your laptop, desktop or server. Every call is checked
against the key the AI app was given (which machines, which folders, read/write/run) and
written to an audit log.

```
  Claude Code ─┐                                    ┌──> laptop    (device agent)
  Codex ───────┤   MCP (HTTPS + key/OAuth)          │
  Claude.ai ───┼──────────────────────>  AMC relay  ─┼──> desktop   (device agent)
  ChatGPT ─────┤                        (you host)  │
  any MCP app ─┘                                    └──> server    (device agent)
                                                ^
                     devices connect OUTBOUND ──┘  (WebSocket; no inbound ports on devices)
```

## Install in one line

**macOS / Linux**

```sh
curl -fsSL https://raw.githubusercontent.com/inflow-digital/amc/main/installers/install.sh | sh
```

**Windows** (PowerShell, no admin needed)

```powershell
irm https://raw.githubusercontent.com/inflow-digital/amc/main/installers/install.ps1 | iex
```

The installer gets [`uv`](https://docs.astral.sh/uv/) if you don't have it (it brings its
own Python), installs the `amc` command and runs **`amc up`**, which:

1. creates the relay on this machine (listening on `127.0.0.1:8790` by default);
2. starts it as a per-user background service (systemd `--user`, a launchd LaunchAgent,
   or a Scheduled Task at logon on Windows);
3. pairs this machine to it as a device, allowed to use your home folder;
4. connects Claude Code and Codex automatically if they are installed;
5. runs `amc doctor` to prove everything works.

Running the installer (or `amc up`) again is safe: it upgrades and repairs.

## Add another machine

On the machine running the relay:

```sh
amc relay pair --name my-desktop
```

It prints a one-time code (valid 15 minutes) and the exact command to paste on the new
machine, for example:

```sh
curl -fsSL https://raw.githubusercontent.com/inflow-digital/amc/main/installers/install.sh \
  | sh -s -- --pair https://relay.example.com ABCD-2345
```

The new machine must be able to reach the relay. If the relay only listens on
`127.0.0.1`, either give it a private-network address (`amc relay init --bind 0.0.0.0 --force`,
e.g. on a VPN such as Tailscale) or an HTTPS URL (see below).

## Connect your AI

| App | Command | What happens |
|---|---|---|
| Claude Code | `amc connect claude` | automatic (`claude mcp add ...` with a fresh key) |
| Codex | `amc connect codex` | automatic (writes `[mcp_servers.amc]` to `~/.codex/config.toml`) |
| Claude.ai / Claude Desktop | `amc connect claude-web` | prints the URL to add as a custom connector and a key to paste on the AMC login page |
| ChatGPT | `amc connect chatgpt` | same, for a ChatGPT connector (developer mode) |
| Any MCP client | `amc connect generic` | prints the Streamable HTTP URL and the `Authorization: Bearer ...` header |

Keys are shown **once**. Running `amc connect ...` again rotates that app's key.

**Claude.ai and ChatGPT need HTTPS**, because they connect from the internet. Two easy
ways that need no router changes:

```sh
cloudflared tunnel --url http://127.0.0.1:8790   # prints https://<random>.trycloudflare.com
tailscale funnel 8790                            # https://<machine>.<tailnet>.ts.net
```

Then tell AMC its public address and connect again:

```sh
amc relay url --public-url https://relay.example.com
amc connect claude-web      # or: amc connect chatgpt
```

A quick tunnel's URL changes every time it restarts; use a named tunnel or Tailscale
Funnel for something permanent.

## Let your AI install it

**Claude Code** — add the plugin, then just ask "install AMC":

```
/plugin marketplace add inflow-digital/amc
/plugin install amc@amc
```

**Codex** (or any agent that reads `AGENTS.md`) — point it at
[`skills/install-amc/AGENTS.md`](skills/install-amc/AGENTS.md) and ask it to follow it.

## Security model

AMC is built so that the relay decides and the devices do the work — and each side
checks again.

- **Keys and access levels.** Every AI app gets its own agent key. A key has an access
  level and can be limited to some devices (`--devices`) and folders (`--paths`):

  | Level | Allows |
  |---|---|
  | `read` | read files, list folders, search, list processes, system status |
  | `standard` | `read` + write, edit, move files and create folders |
  | `full` | `standard` + **run commands** and stop processes |

  `amc key add NAME --access read --devices laptop --paths /home/alice/projects` creates a
  narrow one (use `amc device list` for device ids; `--paths` takes absolute paths).
  `amc key remove NAME` revokes it immediately, including OAuth sessions it approved.
- **What `full` really means.** Running commands (`start_process`) runs a shell **as your
  user account**. Folder limits cannot contain a shell: a `full` key can do anything you can
  do on that machine. Give `full` only to apps you trust with your account; use `standard`
  or `read` otherwise. AMC is not a sandbox.
- **Device roots.** Each device only touches files inside its allowed folders (`--root`,
  default: your home folder). The device resolves `~`, relative paths and symlinks before
  checking, so `..` or a symlink cannot escape.
- **Outbound-only devices.** Devices open one WebSocket to the relay. Nothing listens on
  them, so there is nothing to port-scan or expose.
- **Hashed secrets.** Agent keys, device tokens, pairing codes and OAuth tokens are stored
  only as SHA-256 hashes (files are readable only by you). Pairing codes are single use and
  expire; failed attempts are rate-limited.
- **Audit log.** Every allowed or denied call is written to `audit.jsonl` with who, which
  device, which tool and the decision — never arguments, file contents, results or secrets.
  An app can read its own events with the `remote_audit` tool.
- **Environment isolation.** Commands and stdio MCP servers on a device get only an
  allowlist of ordinary environment variables (`PATH`, `HOME`, locale, ...). The device
  token and any `AMC_*` variables are never passed on.
- **Use HTTPS beyond a private network.** Keys travel as bearer tokens. Plain `http://`
  is fine on `127.0.0.1` or inside a VPN such as Tailscale; anywhere else, put the relay
  behind HTTPS (a tunnel or a reverse proxy).

Details: [docs/security.md](docs/security.md). Report vulnerabilities privately — see
[SECURITY.md](SECURITY.md).

## Troubleshooting

```sh
amc doctor                  # checks everything; every failure prints the command that fixes it
amc relay status            # devices online/offline, keys
amc host status             # this machine's pairing
amc service status relay    # or: host — running / stopped / not installed
```

Service logs are in the AMC config folder under `logs/` (`relay.log`, `host.log`):
`~/.config/amc` on Linux, `~/Library/Application Support/amc` on macOS, `%APPDATA%\amc`
on Windows.

**Uninstall**

```sh
amc service uninstall host
amc service uninstall relay
uv tool uninstall amc-commander
```

Also delete the config folder above to forget keys and device identities.

## Documentation

- [docs/cli.md](docs/cli.md) — every `amc` command
- [docs/architecture.md](docs/architecture.md) — components, protocol, tool catalog, auth
- [docs/security.md](docs/security.md) — threat model and known limitations
- [CONTRIBUTING.md](CONTRIBUTING.md) · [THIRD_PARTY.md](THIRD_PARTY.md) · [LICENSE](LICENSE) (MIT)
