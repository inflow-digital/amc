# AMC security

This page describes what AMC protects, against whom, and where its protection ends.
Component and protocol details are in [architecture.md](architecture.md).

## Assets

- the files, processes and accounts on your devices;
- agent keys (`amck_`), device tokens (`amcd_`), OAuth tokens (`amco_`/`amcr_`) and pairing codes;
- the relay's configuration and audit log.

## Trust assumptions

- **You trust the relay machine.** Whoever controls it (or its config dir) can create keys,
  pair devices and route calls to every device. Run it on a machine you own.
- **A device trusts its relay** for calls within that device's roots and the calling key's
  grant. The device re-checks folder roots itself, but it cannot know whether a call is wise.
- **You trust each AI app with the access level you give its key.** AMC enforces *what* an app
  may do, not *why*; a prompt-injected agent can do anything its key allows.

## Threats and mitigations

| Threat | Mitigation |
|---|---|
| Someone on the network calls the relay | Every MCP/API request needs an agent key or an OAuth token issued for one. `/livez` is the only unauthenticated endpoint and reveals nothing. The relay listens on `127.0.0.1` unless you choose otherwise. |
| Someone scans or attacks a device | Devices listen on nothing; they only open an outbound WebSocket. |
| An app uses more than it was given | Relay policy per key: devices, action class (`read` / `standard` / `full`), optional path prefixes. Unknown tools are denied. |
| Path tricks (`..`, `~`, symlinks, relative paths) | Relay rejects `..` lexically; the device resolves the real path (following symlinks) and checks it against its roots before executing. |
| One app hijacks another app's processes or searches | Built-in executor handles are owned by the key that created them. |
| Stolen credentials from disk | Keys, tokens and codes are stored only as SHA-256 hashes; config files are created `0600` in a `0700` directory (on Windows they rely on the per-user profile ACLs). A key is shown once. |
| Stolen / leaked key | `amc key remove NAME` revokes it and every OAuth token approved with it immediately. `amc device remove ID` disconnects a device and invalidates its token. |
| Pairing-code guessing | Codes are random, single use, expire (15 min default), and the relay stops accepting redemptions after 20 failures per minute. |
| OAuth abuse | Authorization code + PKCE, dynamic client registration; approval requires a valid AMC key (5 wrong attempts cancel the request); the consent page cannot be framed (`X-Frame-Options: DENY`). Access tokens expire after 1 hour; refresh tokens rotate. |
| A compromised or buggy device | It can only answer calls addressed to its own `device_id`; oversized, malformed or foreign frames are counted and the session is closed after 8 violations. Result size is capped (16 MiB). |
| Secrets leaking to commands | Executors get an allowlisted environment; `AMC_*` variables and the device token are never passed on. |
| Secrets leaking to logs | The audit log records identities, tool, action class and decision — never arguments, results, file contents or tokens. Reason strings are truncated. |
| Resource exhaustion by a caller | Argument size cap (256 KiB), per-device in-flight cap (16), per-call timeout (60 s). |

## Known limitations

- **`full` access is not a sandbox.** `start_process` runs a shell as the device's user.
  Folder roots and path prefixes do not apply to commands; a `full` key can read, change or
  delete anything that user can, install software, and reach anything that user's network
  can. Treat a `full` key like your password for that account. `amc up` gives Claude Code and
  Codex `full` by default on your own machine; use `--access standard` if you do not want that.
- **Admin on the relay machine is admin everywhere.** Anyone who can edit the relay's config
  dir can grant themselves any access.
- **No built-in TLS.** The relay speaks plain HTTP. Keys are bearer tokens, so anything
  beyond `127.0.0.1` or a trusted private network (such as a Tailscale tailnet) must go
  through HTTPS: a tunnel (cloudflared, Tailscale Funnel) or a reverse proxy.
- **Tunnels expose the relay to the internet.** Authentication still applies, but the relay
  becomes reachable by anyone; keep keys narrow and remove unused ones.
- **Hashes are unsalted SHA-256** of long random secrets (≥ 256 bits for keys and tokens).
  That is sufficient for random tokens; it would not be for human-chosen passwords, which AMC
  never stores.
- **Relay path prefixes are lexical.** They are a coarse pre-filter; the device roots are
  the real filesystem boundary.
- **stdio executor.** With `--executor stdio`, per-key isolation of processes and searches
  depends on the backend MCP server (DesktopCommanderMCP, for example, does not know about AMC
  keys).
- **Audit is local and append-only by convention**, not tamper-proof. Someone with write
  access to the relay's config dir can edit it.
- **Prompt injection.** Content an agent reads (web pages, files, issues) can steer it. AMC
  limits the blast radius to the key's grant; it does not detect malicious intent.

## Recommendations

1. Give each app its own key and the lowest level that works (`read` for browsing,
   `standard` for editing, `full` only where commands are needed).
2. Pair devices with narrow `--root` folders when you do not need your whole home folder.
3. Keep the relay on `127.0.0.1` or a private network; use HTTPS for anything else.
4. Review `amc relay status` and the audit log (`remote_audit`, `audit.jsonl`) from time to time;
   remove keys and devices you no longer use.
5. Keep AMC up to date (re-run the installer).

## Reporting a vulnerability

Please report security issues **privately** through GitHub Security Advisories:
<https://github.com/inflow-digital/amc/security/advisories/new>. Do not open a public issue.
Include the AMC version (`amc --version`), your OS, and steps to reproduce. We will
acknowledge reports as soon as we can and credit reporters who want to be credited.

## Protected paths

Whatever the device roots and the key, the host always refuses file access to AMC's own
config dir (device token, relay keys, OAuth state) and to common credential locations
(`~/.claude.json`, `~/.codex`, `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.azure`, `~/.config/gcloud`,
`~/.config/gh`, `~/.kube`, `~/.docker/config.json`, `~/.netrc`, `~/.git-credentials`, `~/.pypirc`,
`~/.npmrc`). Directory listings and searches do not enter them and never follow symlinks. A key's
`paths` scope is enforced by the host on the resolved (real) path. Extra paths can be added in
`host.json` (`"protected": [...]`). Note that keys with `full` access can run shell commands, which
are not confined — give `full` only to agents you trust with your user account.

## OAuth tokens

OAuth tokens are bound to the hash of the AMC key that approved them: removing *or rotating*
the key (`amc key add NAME --replace`, `amc connect ...`) revokes them immediately. The consent
page shows the host that will receive access and warns when it is not a known AI app.
