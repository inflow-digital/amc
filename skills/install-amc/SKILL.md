---
name: install-amc
description: Install, pair and connect AMC (Agent MCP Commander) on the user's machine, then register it with Claude Code / Claude.ai and verify it works. Use when the user asks to "install AMC", "set up AMC", "connect this machine to my AMC relay", "pair this computer", or "let Claude use my other machine".
---

# Install AMC for the user

AMC gives AI agents governed MCP access to the user's machines through a self-hosted
relay. One command, `amc`, does everything. Your job: get it installed, running as a
background service, connected to the user's agents, and **verified**.

Work step by step, run the commands yourself, and show the user each result. Never print
or paste an agent key or device token into chat beyond what a command prints once; never
commit AMC config files anywhere.

## 1. Decide the mode (ask only if unclear)

| Situation | Mode |
|---|---|
| First machine / "set it up here" | **Everything on this machine**: relay + this machine + agent keys (`amc up`) |
| User already has a relay and gave you a pairing command or `RELAY_URL` + `CODE` | **Pair** this machine to that relay |
| User only wants the CLI | **CLI only** (`--no-start`) |

If the user wants to add another machine but has no code yet: on the relay machine run
`amc relay pair --name <device-name>`; it prints the exact install/pair command for the
new machine (codes expire after 15 minutes).

## 2. Run the installer

Linux / macOS:

```sh
# everything on this machine
curl -fsSL https://raw.githubusercontent.com/inflow-digital/amc/main/installers/install.sh | sh
# pair to an existing relay
curl -fsSL https://raw.githubusercontent.com/inflow-digital/amc/main/installers/install.sh | sh -s -- --pair https://relay.example.com CODE --name my-laptop
# CLI only
curl -fsSL https://raw.githubusercontent.com/inflow-digital/amc/main/installers/install.sh | sh -s -- --no-start
```

Windows (PowerShell, no admin needed):

```powershell
irm https://raw.githubusercontent.com/inflow-digital/amc/main/installers/install.ps1 | iex
# pair to an existing relay
$env:AMC_PAIR_URL = "https://relay.example.com"; $env:AMC_PAIR_CODE = "CODE"; $env:AMC_PAIR_NAME = "my-pc"
irm https://raw.githubusercontent.com/inflow-digital/amc/main/installers/install.ps1 | iex
```

The installer installs `uv` if missing, then `uv tool install amc-commander`, then runs
`amc up` or `amc host pair ... && amc service install host`. Re-running it upgrades.
If it fails it prints a `Fix:` line: do that, then re-run.

If `amc` is "not found" afterwards in your shell, prepend the tool dir for this session:
`export PATH="$(uv tool dir --bin):$PATH"` (PowerShell: `$env:Path = "$(uv tool dir --bin);$env:Path"`),
and tell the user to run `uv tool update-shell` once so new terminals find it.

Already have Python tooling and prefer it? Equivalent manual steps:
`uv tool install --python 3.12 "amc-commander @ https://github.com/inflow-digital/amc/archive/refs/heads/main.zip"`
then `amc up` (or `amc host pair RELAY_URL CODE` + `amc service install host`).

## 3. Connect the agents

`amc up` already registers AMC with every agent CLI it finds on PATH. To (re)do it
explicitly, or on a relay machine after pairing more devices:

```sh
amc connect claude        # runs `claude mcp add ...` with a fresh key
amc connect codex         # runs `codex mcp add ...`
amc connect claude-web    # prints what to paste into Claude.ai (needs a public HTTPS URL, see 4)
amc connect chatgpt       # same for ChatGPT
```

Default access for `amc up` is `full` on the owner's own machine. For a narrower key:
`amc key add NAME --access read|standard|full [--devices a,b] [--paths p1,p2]`.
Ask the user before granting `full` to anything other than their own local agent.

After `amc connect claude`, a running Claude Code session must be restarted (or use
`/mcp`) to see the new server.

## 4. Claude.ai web / ChatGPT: give the relay a public HTTPS URL

Web agents cannot reach `127.0.0.1`. Expose the relay port (default 8790) with one of:

```sh
cloudflared tunnel --url http://127.0.0.1:8790     # prints https://<random>.trycloudflare.com
tailscale funnel 8790                              # prints https://<machine>.<tailnet>.ts.net
```

Then register that URL with the relay and connect the web agent:

```sh
amc relay url --public-url https://relay.example.com
amc connect claude-web
```

A `trycloudflare.com` URL changes each time cloudflared restarts; for a stable setup use
a named Cloudflare tunnel or Tailscale Funnel. Only the relay is exposed; devices always
connect outbound, so never open inbound ports on the user's other machines.

## 5. Verify (do not skip)

```sh
amc doctor                     # every failed check prints the one command that fixes it
amc service status relay       # expect: running   (only on the relay machine)
amc service status host        # expect: running
amc relay status               # this device listed as online (relay machine)
amc host status                # paired device summary (never shows the token)
```

Then prove it end to end from the agent: in Claude Code run `claude mcp list` and check
AMC is connected, and ask the agent to list a directory on the device through AMC.

Report to the user: mode used, relay URL, device name(s), which agents were connected and
with what access level, service status, and anything left for them to do (for example
`uv tool update-shell`, or `sudo loginctl enable-linger $USER` if the service message
warned that it stops at logout).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `amc: command not found` | `export PATH="$(uv tool dir --bin):$PATH"`; persist with `uv tool update-shell` |
| pairing failed / code expired | run `amc relay pair` on the relay for a new code |
| service `stopped` | read the log in `<config dir>/logs/<role>.log`, then `amc service install relay|host` |
| Linux: service stops at logout | `sudo loginctl enable-linger $USER` |
| container / WSL without systemd | AMC runs as a background process; re-run `amc service install host` after a reboot |
| Windows: console window "AMC Host" | that is the service; minimize it, do not close it |
| Claude.ai cannot connect | public URL must be HTTPS and set with `amc relay url --public-url ...` |

Config dir: `~/.config/amc` (Linux), `~/Library/Application Support/amc` (macOS),
`%APPDATA%\amc` (Windows); `AMC_HOME` overrides it.

Uninstall: `amc service uninstall host; amc service uninstall relay; uv tool uninstall amc-commander`
(also delete the config dir to forget identity and keys).
