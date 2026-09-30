"""``amc`` — one command for the relay, devices, agent keys, services and diagnostics."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

from amc import __version__
from amc.paths import config_dir


def _symbols() -> tuple[str, str, str]:
    try:
        "✔✘".encode(sys.stdout.encoding or "ascii")
        return "✔", "!", "✘"
    except (UnicodeEncodeError, LookupError):
        return "OK", "!", "X"


OK, WARN, FAIL = _symbols()
AGENTS = ("claude", "codex", "claude-web", "chatgpt", "generic")


class CliError(RuntimeError):
    pass


def say(message: str = "") -> None:
    print(message, flush=True)


# --- helpers ------------------------------------------------------------------------------------


def _store():
    from amc.relay.store import RelayStore

    return RelayStore()


def _bind_hosts(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()] or ["127.0.0.1"]


def _local_url(state: Any) -> str:
    """URL this machine uses to reach its own relay."""
    host = _bind_hosts(state.host)[0]
    if host in {"0.0.0.0", "::", "", "localhost", "::1"} or host.startswith("127."):
        host = "127.0.0.1"
    elif ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{state.port}"


def _agent_url(state: Any, override: str | None = None) -> str:
    return (override or state.public_url or _local_url(state)).rstrip("/")


def _lan_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # TEST-NET; nothing is sent
            addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    return sorted(addresses)


def _wait_http(url: str, timeout: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


@contextmanager
def _temporary_key(store: Any, access: str = "read"):
    """A short-lived agent key for local checks (removed afterwards)."""
    name = f"amc-doctor-{os.getpid()}"
    key = store.add_client(name, access=access, replace=True)
    try:
        yield key
    finally:
        try:
            store.remove_client(name)
        except Exception:  # noqa: BLE001
            pass


def _service():
    from amc import service

    return service


def _devices_via_api(url: str, key: str) -> list[dict[str, Any]]:
    response = httpx.get(f"{url}/api/v1/devices", headers={"Authorization": f"Bearer {key}"}, timeout=5)
    response.raise_for_status()
    return response.json()


# --- relay ----------------------------------------------------------------------------------------


def cmd_relay_init(args: argparse.Namespace) -> int:
    from amc.relay.store import StoreError

    try:
        state = _store().init(host=args.bind, port=args.port, public_url=args.public_url, force=args.force)
    except StoreError as exc:
        raise CliError(str(exc)) from exc
    say(f"{OK} relay configured: {_store().path}")
    say(f"  listening on {state.host}:{state.port}" + (f", public URL {state.public_url}" if state.public_url else ""))
    say("  next: amc service install relay   (or: amc relay serve)")
    return 0


def cmd_relay_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from amc.relay.app import create_app

    store = _store()
    state = store.state
    hosts = _bind_hosts(args.bind or state.host)
    port = args.port or state.port
    for host in hosts:
        if host not in {"127.0.0.1", "localhost", "::1"} and not (state.public_url or "").startswith("https://"):
            logging.getLogger("amc.relay").warning(
                "listening on %s without HTTPS: keys travel in clear text unless this network is private "
                "(e.g. Tailscale/WireGuard) or a TLS proxy/tunnel sits in front", host)
    config = uvicorn.Config(
        create_app(store),
        port=port,
        ws_max_size=state.max_result_bytes,
        server_header=False,
        proxy_headers=state.trust_proxy_headers,
        log_level="info",
    )
    sockets = []
    for host in hosts:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            raise CliError(f"cannot listen on {host}:{port}: {exc}") from exc
        sock.listen(2048)
        sock.set_inheritable(True)
        sockets.append(sock)
    logging.getLogger("amc.relay").info("AMC relay listening on %s", ", ".join(f"{h}:{port}" for h in hosts))
    uvicorn.Server(config).run(sockets=sockets)
    return 0


def cmd_relay_pair(args: argparse.Namespace) -> int:
    store = _store()
    state = store.state
    code = store.create_pairing(args.name or "", ttl_seconds=args.ttl)
    minutes = args.ttl // 60
    urls = [state.public_url] if state.public_url else []
    for host in _bind_hosts(state.host):
        if host in {"0.0.0.0", "::"}:
            urls += [f"http://{address}:{state.port}" for address in _lan_addresses()]
        elif host not in {"127.0.0.1", "localhost", "::1"}:
            urls.append(f"http://{host}:{state.port}")
    say(f"Pairing code: {code}   (valid {minutes} min, one use)")
    if not urls:
        say(f"{WARN} this relay only listens on 127.0.0.1, so other machines cannot reach it.")
        say("  Give it an address other machines can reach, then pair again:")
        say("    amc relay url --public-url https://...   (tunnel/HTTPS), or")
        say("    amc relay init --bind 0.0.0.0 --force    (private network such as Tailscale)")
        say("  On this same machine you can still run:")
        urls = [_local_url(state)]
    url = urls[0]
    say("")
    say("On the new machine run ONE of these:")
    say(f"  macOS / Linux:  curl -fsSL https://raw.githubusercontent.com/inflow-digital/amc/main/installers/install.sh"
        f" | sh -s -- --pair {url} {code}")
    say(f"  Windows (PowerShell):  $env:AMC_PAIR_URL='{url}'; $env:AMC_PAIR_CODE='{code}'; "
        "irm https://raw.githubusercontent.com/inflow-digital/amc/main/installers/install.ps1 | iex")
    say(f"  AMC already installed:  amc host pair {url} {code} && amc service install host")
    if len(urls) > 1:
        say("  (other relay addresses: " + ", ".join(urls[1:]) + ")")
    return 0


def cmd_relay_url(args: argparse.Namespace) -> int:
    url = args.public_url.rstrip("/") if args.public_url else None
    if url and not re.match(r"^https?://", url):
        raise CliError("public URL must start with https:// (or http:// for a private network)")
    with _store().edit() as state:
        state.public_url = url
    say(f"{OK} public URL set to {url}" if url else f"{OK} public URL cleared")
    _restart_if_installed("relay")
    return 0


def _restart_if_installed(role: str) -> None:
    try:
        service = _service()
        if not service.status(role).startswith("not installed"):
            say(service.install(role))
            return
    except Exception as exc:  # noqa: BLE001
        say(f"{WARN} could not restart the {role} service automatically: {exc}")
    say(f"  restart the {role} to apply (amc service install {role}, or re-run amc {role} serve/run)")


def cmd_relay_status(args: argparse.Namespace) -> int:
    store = _store()
    state = store.state
    say(f"relay config: {store.path}")
    say(f"listen: {state.host}:{state.port}   public URL: {state.public_url or '-'}")
    online: set[str] = set()
    running = False
    try:
        running = httpx.get(f"{_local_url(state)}/livez", timeout=2).status_code == 200
        if running and state.devices:
            with _temporary_key(store) as key:
                online = {d["device_id"] for d in _devices_via_api(_local_url(state), key) if d["connected"]}
    except httpx.HTTPError:
        pass
    say(f"relay process: {'running' if running else 'NOT running'}")
    say("devices:")
    for device in state.devices:
        say(f"  {device.device_id:<28} {device.name:<16} {'online' if device.device_id in online else 'offline'}")
    if not state.devices:
        say("  (none — add one with: amc relay pair)")
    say("agent keys:")
    for client in state.clients:
        if client.name.startswith("amc-doctor-"):
            continue
        scope = "all devices" if "*" in client.devices else ",".join(client.devices)
        paths = f" paths={client.paths}" if client.paths else ""
        say(f"  {client.name:<20} access={client.access:<8} {scope}{paths}")
    return 0


# --- keys & agent connection --------------------------------------------------------------------


def cmd_key_add(args: argparse.Namespace) -> int:
    from amc.relay.store import StoreError

    secret = _read_secret() if args.from_stdin else None
    try:
        key = _store().add_client(
            args.name, access=args.access, devices=_csv(args.devices), paths=_csv(args.paths), replace=args.replace,
            key=secret,
        )
    except StoreError as exc:
        raise CliError(str(exc)) from exc
    if secret:
        say(f"{OK} imported existing secret as agent key {args.name!r} ({args.access})")
        return 0
    say(f"{OK} agent key {args.name!r} ({args.access}) — shown once, store it safely:")
    say(f"  {key}")
    return 0


def _read_secret() -> str:
    """Read a secret from stdin (never from argv, which other users can see in the process list)."""
    if sys.stdin.isatty():
        import getpass

        return getpass.getpass("secret: ").strip()
    return sys.stdin.readline().strip()


def _csv(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def cmd_key_list(args: argparse.Namespace) -> int:
    for client in _store().state.clients:
        devices = ",".join(client.devices)
        say(f"{client.name:<20} access={client.access:<8} devices={devices} created={client.created_at}")
    return 0


def cmd_key_remove(args: argparse.Namespace) -> int:
    if not _store().remove_client(args.name):
        raise CliError(f"no agent key named {args.name!r}")
    say(f"{OK} removed key {args.name!r} (tokens it approved stop working immediately)")
    return 0


def _codex_config_path() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"


def write_codex_server(path: Path, url: str, key: str, name: str = "amc") -> None:
    """Add/replace ``[mcp_servers.<name>]`` in Codex's config.toml, leaving everything else intact."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    header = re.compile(rf"^\[mcp_servers\.{re.escape(name)}(\.[^\]]+)?\]\s*$")
    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            skipping = bool(header.match(stripped))
        if not skipping:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    block = [
        f"[mcp_servers.{name}]",
        f'url = "{url}"',
        f'http_headers = {{ Authorization = "Bearer {key}" }}',
    ]
    new_text = "\n".join(kept + ([""] if kept else []) + block) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(path, 0o600)


def connect_agent(agent: str, *, key_name: str | None = None, url: str | None = None, access: str = "full",
                  quiet: bool = False) -> bool:
    """Create/rotate a key for ``agent`` and register AMC with it. Returns True when fully automatic."""
    store = _store()
    state = store.state
    base = _agent_url(state, url)
    mcp_url = f"{base}/mcp"
    name = key_name or {"claude": "claude-code", "claude-web": "claude-web"}.get(agent, agent)
    key = store.add_client(name, access=access, replace=True)
    out = (lambda *_: None) if quiet else say
    if agent == "claude":
        claude = shutil.which("claude")
        if claude:
            subprocess.run([claude, "mcp", "remove", "--scope", "user", "amc"], capture_output=True)
            done = subprocess.run(
                [claude, "mcp", "add", "--transport", "http", "--scope", "user", "amc", mcp_url,
                 "--header", f"Authorization: Bearer {key}"],
                capture_output=True, text=True,
            )
            if done.returncode == 0:
                say(f"{OK} Claude Code: AMC added (user scope) → {mcp_url}")
                return True
            say(f"{WARN} `claude mcp add` failed: {done.stderr.strip() or done.stdout.strip()}")
        out("Claude Code: run")
        out(f'  claude mcp add --transport http --scope user amc {mcp_url} --header "Authorization: Bearer {key}"')
        return False
    if agent == "codex":
        path = _codex_config_path()
        write_codex_server(path, mcp_url, key)
        say(f"{OK} Codex: AMC added to {path} → {mcp_url}")
        return True
    if agent in {"claude-web", "chatgpt"}:
        app = "Claude (claude.ai / Desktop): Settings → Connectors → Add custom connector" if agent == "claude-web" \
            else "ChatGPT: Settings → Apps & Connectors → Advanced → Developer mode → Create connector (OAuth)"
        if not base.startswith("https://"):
            out(f"{WARN} {agent} connects from the internet and needs an HTTPS URL for this relay.")
            out("  Quick options (no port forwarding needed):")
            out(f"    cloudflared tunnel --url {_local_url(state)}      # prints https://<random>.trycloudflare.com")
            out(f"    tailscale funnel {state.port}                           # https://<machine>.<tailnet>.ts.net")
            out("  then: amc relay url --public-url https://...  and run this command again.")
        out(f"{app}")
        out(f"  URL:  {mcp_url}")
        out("  When the AMC login page opens, paste this key (shown once):")
        out(f"  {key}")
        return False
    out(f"Any MCP client (Streamable HTTP): URL {mcp_url}")
    out(f"  header: Authorization: Bearer {key}")
    return False


def cmd_connect(args: argparse.Namespace) -> int:
    connect_agent(args.agent, key_name=args.key, url=args.url, access=args.access)
    return 0


# --- host / devices -----------------------------------------------------------------------------


def pair_host(relay_url: str, code: str, *, name: str | None = None, roots: list[str] | None = None,
              executor: str = "builtin", stdio_command: str = "", stdio_args: list[str] | None = None,
              force: bool = False) -> Any:
    from amc.host.config import HostConfig, relay_http_url

    if HostConfig.default_path().exists() and not force:
        raise CliError(f"this machine is already paired ({HostConfig.default_path()}); use --force to re-pair")
    base = relay_http_url(relay_url)
    label = name or socket.gethostname().split(".")[0] or "device"
    try:
        response = httpx.post(f"{base}/api/v1/pair", json={"code": code, "name": label,
                                                            "agent_version": f"amc-host/{__version__}"}, timeout=15)
    except httpx.HTTPError as exc:
        raise CliError(f"cannot reach the relay at {base}: {exc}") from exc
    if response.status_code != 200:
        detail = response.json().get("detail") if "json" in response.headers.get("content-type", "") else response.text
        raise CliError(f"pairing failed ({response.status_code}): {detail}")
    data = response.json()
    config = HostConfig(relay_url=base, device_id=data["device_id"], device_token=data["device_token"],
                        name=data.get("name", label))
    if roots:
        config.roots = roots
    config.executor = executor
    config.stdio_command = stdio_command
    config.stdio_args = stdio_args or []
    config.save()
    return config


def cmd_host_pair(args: argparse.Namespace) -> int:
    config = pair_host(args.relay_url, args.code, name=args.name, roots=args.root, executor=args.executor,
                       stdio_command=args.stdio_command or "", stdio_args=args.stdio_arg, force=args.force)
    say(f"{OK} paired as {config.device_id} with {config.relay_url}")
    say(f"  allowed roots: {', '.join(config.roots)}")
    say("  next: amc service install host   (or: amc host run)")
    return 0


def cmd_host_run(args: argparse.Namespace) -> int:
    from amc.host.config import HostConfig
    from amc.host.runner import main as run

    try:
        config = HostConfig.load()
    except FileNotFoundError as exc:
        raise CliError(str(exc)) from exc
    return run(config)


def cmd_host_status(args: argparse.Namespace) -> int:
    from amc.host.config import HostConfig

    try:
        config = HostConfig.load()
    except FileNotFoundError as exc:
        raise CliError(str(exc)) from exc
    say(f"device: {config.device_id} ({config.name})")
    say(f"relay:  {config.relay_url}")
    say(f"roots:  {', '.join(config.roots)}   executor: {config.executor}")
    try:
        say(f"service: {_service().status('host')}")
    except Exception as exc:  # noqa: BLE001
        say(f"service: unknown ({exc})")
    return 0


def cmd_device_list(args: argparse.Namespace) -> int:
    for device in _store().state.devices:
        say(f"{device.device_id:<28} {device.name:<16} paired {device.created_at}")
    return 0


def cmd_device_add(args: argparse.Namespace) -> int:
    from amc.relay.store import StoreError

    try:
        device = _store().add_device(args.device_id, _read_secret(), name=args.name or args.device_id)
    except StoreError as exc:
        raise CliError(str(exc)) from exc
    say(f"{OK} device {device.device_id} registered with the token read from stdin")
    return 0


def cmd_host_adopt(args: argparse.Namespace) -> int:
    from amc.host.config import HostConfig, relay_http_url

    if HostConfig.default_path().exists() and not args.force:
        raise CliError(f"this machine is already configured ({HostConfig.default_path()}); use --force")
    token = _read_secret()
    if len(token) < 16:
        raise CliError("device token must be at least 16 characters")
    config = HostConfig(relay_url=relay_http_url(args.relay_url), device_id=args.device_id, device_token=token,
                        name=args.name or args.device_id)
    if args.root:
        config.roots = args.root
    config.executor = args.executor
    config.stdio_command = args.stdio_command or ""
    config.stdio_args = args.stdio_arg or []
    config.save()
    say(f"{OK} this machine is configured as {config.device_id} for {config.relay_url}")
    say("  next: amc service install host")
    return 0


def cmd_device_remove(args: argparse.Namespace) -> int:
    if not _store().remove_device(args.device_id):
        raise CliError(f"no device {args.device_id!r}")
    say(f"{OK} removed {args.device_id}; it is disconnected within 15 s and cannot reconnect")
    return 0


# --- services -----------------------------------------------------------------------------------


def cmd_service(args: argparse.Namespace) -> int:
    service = _service()
    if args.action == "install":
        say(service.install(args.role))
    elif args.action == "uninstall":
        say(service.uninstall(args.role))
    else:
        say(service.status(args.role))
    return 0


# --- doctor ----------------------------------------------------------------------------------------


def run_doctor(*, quiet_ok: bool = False) -> int:
    from amc.host.config import HostConfig, relay_http_url

    problems = 0

    def check(ok: bool, label: str, fix: str = "", warn: bool = False) -> None:
        nonlocal problems
        if ok:
            if not quiet_ok:
                say(f"{OK} {label}")
        else:
            say(f"{WARN if warn else FAIL} {label}" + (f"\n    fix: {fix}" if fix else ""))
            if not warn:
                problems += 1

    store = _store()
    host_path = HostConfig.default_path()
    say(f"AMC {__version__} — config dir {config_dir()}")
    relay_here = store.exists()
    host_here = host_path.exists()
    check(relay_here or host_here, "this machine has a relay or a device configured", "amc up  (or amc host pair ...)")
    if relay_here:
        state = store.state
        url = _local_url(state)
        alive = _wait_http(f"{url}/livez", timeout=3)
        check(alive, f"relay answers on {url}", "amc service install relay   (logs: amc service status relay)")
        if alive:
            with _temporary_key(store) as key:
                try:
                    rows = _devices_via_api(url, key)
                    check(True, "relay accepts agent keys")
                    for row in rows:
                        check(row["connected"], f"device {row['device_id']} ({row['name']}) online",
                              "on that machine: amc service install host", warn=True)
                    if not rows:
                        check(False, "at least one device paired", "amc relay pair", warn=True)
                except httpx.HTTPError as exc:
                    check(False, f"relay API works ({exc})", "amc service install relay")
        check(bool([c for c in state.clients if not c.name.startswith("amc-doctor-")]), "an agent key exists",
              "amc connect claude   (or codex / claude-web / chatgpt)", warn=True)
        if state.public_url:
            try:
                ok = httpx.get(f"{state.public_url.rstrip('/')}/livez", timeout=8).status_code == 200
            except httpx.HTTPError:
                ok = False
            check(ok, f"public URL {state.public_url} reaches the relay",
                  "start your tunnel/proxy, or fix it with: amc relay url --public-url ...", warn=True)
    if host_here:
        config = HostConfig.load()
        base = relay_http_url(config.relay_url)
        alive = _wait_http(f"{base}/livez", timeout=3)
        check(alive, f"this device reaches its relay {base}",
              "check network/VPN, or re-pair: amc host pair ... --force")
        try:
            status = _service().status("host")
            check(status.startswith("running"), f"host service: {status}", "amc service install host")
        except Exception as exc:  # noqa: BLE001
            check(False, f"host service status unknown: {exc}", "amc service install host", warn=True)
        if relay_here and alive:
            with _temporary_key(store) as key:
                probe = _probe_read(base, key, config.device_id, config.roots[0] if config.roots else "~")
            check(probe is None, "end-to-end: a read through the relay reaches this device",
                  probe or "", warn=False)
    if shutil.which("claude"):
        listed = subprocess.run(["claude", "mcp", "get", "amc"], capture_output=True, text=True)
        check(listed.returncode == 0, "Claude Code has AMC configured", "amc connect claude", warn=True)
    if _codex_config_path().exists() or shutil.which("codex"):
        configured = _codex_config_path().exists() and "[mcp_servers.amc]" in _codex_config_path().read_text()
        check(configured, "Codex has AMC configured", "amc connect codex", warn=True)
    say("all good" if problems == 0 else f"{problems} problem(s) found")
    return 0 if problems == 0 else 1


def _probe_read(base: str, key: str, device_id: str, path: str) -> str | None:
    """Return None when a governed read works end to end, else a fix hint."""
    for _ in range(20):
        try:
            response = httpx.post(f"{base}/api/v1/execute", headers={"Authorization": f"Bearer {key}"},
                                  json={"device_id": device_id, "tool": "get_file_info", "arguments": {"path": path}},
                                  timeout=15)
        except httpx.HTTPError as exc:
            return f"relay request failed: {exc}"
        if response.status_code == 503:
            time.sleep(1)
            continue
        if response.status_code != 200:
            return f"relay said {response.status_code}: {response.text[:200]}"
        return None if response.json().get("ok") else f"device error: {response.json().get('error')}"
    return "device is not connected — amc service install host (logs: amc service status host)"


def cmd_doctor(args: argparse.Namespace) -> int:
    return run_doctor()


# --- up ------------------------------------------------------------------------------------------------


def cmd_up(args: argparse.Namespace) -> int:
    from amc.host.config import HostConfig

    store = _store()
    if not store.exists() and HostConfig.default_path().exists() and not args.relay_here:
        # Already paired to a relay elsewhere: this is an upgrade/re-run on a device.
        say(f"{OK} this machine is a device of {HostConfig.load().relay_url}; refreshing its service")
        if not args.no_service:
            say(_service().install("host"))
            time.sleep(2)
        return run_doctor()
    if not store.exists():
        store.init(host=args.bind, port=args.port, public_url=args.public_url)
        say(f"{OK} relay configured ({store.path})")
    elif args.public_url:
        with store.edit() as state:
            state.public_url = args.public_url.rstrip("/")
    state = store.state
    url = _local_url(state)
    if args.no_service:
        say("--no-service: start the relay yourself with `amc relay serve` and the device with `amc host run`")
    else:
        say(_service().install("relay"))
        if not _wait_http(f"{url}/livez"):
            raise CliError(f"relay did not come up on {url}; see: amc service status relay")
        say(f"{OK} relay running on {url}")
    if not HostConfig.default_path().exists():
        if args.no_service and not _wait_http(f"{url}/livez", timeout=1):
            say(f"{WARN} relay not running; skipping device pairing (run amc up again once it runs)")
        else:
            code = store.create_pairing("", ttl_seconds=120)
            config = pair_host(url, code, name=socket.gethostname().split(".")[0] or "this-machine")
            say(f"{OK} this machine paired as device {config.device_id} (files under {', '.join(config.roots)})")
    if not args.no_service:
        say(_service().install("host"))
    for agent, binary in (("claude", "claude"), ("codex", "codex")):
        if shutil.which(binary) and not args.no_agents:
            connect_agent(agent, access=args.access)
    say("")
    say("Other AI apps:")
    say("  amc connect claude-web   # Claude.ai / Claude Desktop (needs an HTTPS URL)")
    say("  amc connect chatgpt      # ChatGPT connector (needs an HTTPS URL)")
    say("  amc connect generic      # any other MCP client")
    say("Add another machine: amc relay pair")
    say("")
    if not args.no_service:
        time.sleep(1)
        return run_doctor(quiet_ok=False)
    return 0


# --- parser ---------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="amc", description="AMC — let AI agents safely use your machines.")
    parser.add_argument("--version", action="version", version=f"amc {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="set up everything on this machine (relay + device + agents)")
    up.add_argument("--port", type=int, default=8790)
    up.add_argument("--bind", default="127.0.0.1")
    up.add_argument("--public-url")
    up.add_argument("--access", choices=["read", "standard", "full"], default="full")
    up.add_argument("--no-service", action="store_true")
    up.add_argument("--no-agents", action="store_true", help="do not configure Claude Code / Codex")
    up.add_argument("--relay-here", action="store_true",
                    help="also run a relay on a machine that is already a device of another relay")
    up.set_defaults(func=cmd_up)

    relay = sub.add_parser("relay", help="relay commands").add_subparsers(dest="relay_command", required=True)
    p = relay.add_parser("init", help="create relay.json")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--public-url")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_relay_init)
    p = relay.add_parser("serve", help="run the relay in the foreground")
    p.add_argument("--bind")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_relay_serve)
    p = relay.add_parser("pair", help="create a one-time pairing code for a new machine")
    p.add_argument("--name", default="")
    p.add_argument("--ttl", type=int, default=900)
    p.set_defaults(func=cmd_relay_pair)
    p = relay.add_parser("status", help="relay, devices and keys")
    p.set_defaults(func=cmd_relay_status)
    p = relay.add_parser("url", help="set the public URL (HTTPS) used by hosted AI apps")
    p.add_argument("--public-url", required=False)
    p.set_defaults(func=cmd_relay_url)

    key = sub.add_parser("key", help="agent keys").add_subparsers(dest="key_command", required=True)
    p = key.add_parser("add")
    p.add_argument("name")
    p.add_argument("--access", choices=["read", "standard", "full"], default="standard")
    p.add_argument("--devices", help="comma-separated device ids (default: all)")
    p.add_argument("--paths", help="comma-separated path prefixes (default: device roots)")
    p.add_argument("--replace", action="store_true")
    p.add_argument("--from-stdin", action="store_true", help="import an existing secret read from stdin")
    p.set_defaults(func=cmd_key_add)
    p = key.add_parser("list")
    p.set_defaults(func=cmd_key_list)
    p = key.add_parser("remove")
    p.add_argument("name")
    p.set_defaults(func=cmd_key_remove)

    p = sub.add_parser("connect", help="connect an AI agent (creates its key and configures it)")
    p.add_argument("agent", choices=AGENTS)
    p.add_argument("--key", help="key name to create/rotate (default: per agent)")
    p.add_argument("--url", help="relay base URL the agent should use")
    p.add_argument("--access", choices=["read", "standard", "full"], default="full")
    p.set_defaults(func=cmd_connect)

    host = sub.add_parser("host", help="device commands").add_subparsers(dest="host_command", required=True)
    p = host.add_parser("pair", help="pair this machine with a relay")
    p.add_argument("relay_url")
    p.add_argument("code")
    p.add_argument("--name")
    p.add_argument("--root", action="append", help="allowed directory (repeatable; default: home)")
    p.add_argument("--executor", choices=["builtin", "stdio"], default="builtin")
    p.add_argument("--stdio-command")
    p.add_argument("--stdio-arg", action="append")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_host_pair)
    p = host.add_parser("adopt", help="configure this machine with an existing device id + token (stdin)")
    p.add_argument("relay_url")
    p.add_argument("--device-id", required=True)
    p.add_argument("--name")
    p.add_argument("--root", action="append")
    p.add_argument("--executor", choices=["builtin", "stdio"], default="builtin")
    p.add_argument("--stdio-command")
    p.add_argument("--stdio-arg", action="append")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_host_adopt)
    p = host.add_parser("run", help="run the device agent in the foreground")
    p.set_defaults(func=cmd_host_run)
    p = host.add_parser("status")
    p.set_defaults(func=cmd_host_status)

    device = sub.add_parser("device", help="devices known to the relay").add_subparsers(dest="device_command",
                                                                                          required=True)
    p = device.add_parser("list")
    p.set_defaults(func=cmd_device_list)
    p = device.add_parser("add", help="register a device with an existing token read from stdin")
    p.add_argument("device_id")
    p.add_argument("--name")
    p.set_defaults(func=cmd_device_add)
    p = device.add_parser("remove")
    p.add_argument("device_id")
    p.set_defaults(func=cmd_device_remove)

    p = sub.add_parser("service", help="background services")
    p.add_argument("action", choices=["install", "uninstall", "status"])
    p.add_argument("role", choices=["relay", "host"])
    p.set_defaults(func=cmd_service)

    p = sub.add_parser("doctor", help="check everything and say how to fix problems")
    p.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if sys.platform == "win32":
        with contextlib_suppress():
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    try:
        return int(args.func(args) or 0)
    except CliError as exc:
        say(f"{FAIL} {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - friendly top-level error
        from amc.relay.store import StoreError
        from amc.service import ServiceError

        if isinstance(exc, StoreError | ServiceError | FileNotFoundError | json.JSONDecodeError):
            say(f"{FAIL} {exc}")
            return 1
        raise


@contextmanager
def contextlib_suppress():
    try:
        yield
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    raise SystemExit(main())
