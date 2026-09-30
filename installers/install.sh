#!/bin/sh
# AMC (Agent MCP Commander) installer for Linux and macOS.
#
#   curl -fsSL https://raw.githubusercontent.com/inflow-digital/amc/main/installers/install.sh | sh
#       install AMC and run `amc up` (relay + this machine + agent keys, as background services)
#
#   curl -fsSL .../install.sh | sh -s -- --pair https://relay.example.com CODE [--name NAME]
#       install AMC and connect this machine to an existing relay
#
#   curl -fsSL .../install.sh | sh -s -- --no-start
#       only install the `amc` command
#
# Environment overrides: AMC_SOURCE (what `uv tool install` installs; a git URL, local path
# or wheel), AMC_PAIR_URL / AMC_PAIR_CODE / AMC_PAIR_NAME, AMC_NO_START=1.
# Re-running the installer upgrades AMC in place.

set -eu

AMC_DEFAULT_SOURCE="amc-commander@git+https://github.com/inflow-digital/amc"
UV_INSTALL_URL="https://astral.sh/uv/install.sh"

say() { printf '==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
fail() {
    printf '\nAMC install failed: %s\n' "$1" >&2
    if [ "${2:-}" != "" ]; then
        printf 'Fix: %s\n' "$2" >&2
    fi
    exit 1
}

usage() {
    cat <<'EOF'
Usage: install.sh [--pair RELAY_URL CODE [--name NAME]] [--no-start]

  (no options)                 install AMC and run `amc up` on this machine
  --pair RELAY_URL CODE        install AMC and connect this machine to a relay
  --name NAME                  device name to use with --pair (default: hostname)
  --no-start                   only install the `amc` command
EOF
}

main() {
    pair_url="${AMC_PAIR_URL:-}"
    pair_code="${AMC_PAIR_CODE:-}"
    pair_name="${AMC_PAIR_NAME:-}"
    no_start="${AMC_NO_START:-}"
    source_spec="${AMC_SOURCE:-$AMC_DEFAULT_SOURCE}"

    while [ $# -gt 0 ]; do
        case "$1" in
            --pair)
                [ $# -ge 3 ] || fail "--pair needs RELAY_URL and CODE" \
                    "sh -s -- --pair https://relay.example.com ABCD-1234"
                pair_url="$2"
                pair_code="$3"
                shift 3
                ;;
            --name)
                [ $# -ge 2 ] || fail "--name needs a value" "--name my-laptop"
                pair_name="$2"
                shift 2
                ;;
            --no-start)
                no_start=1
                shift
                ;;
            -h|--help)
                usage
                return 0
                ;;
            *)
                usage >&2
                fail "unknown option: $1" "see the usage above"
                ;;
        esac
    done

    if [ -n "$pair_url" ] && [ -z "$pair_code" ]; then
        fail "a pairing URL was given without a pairing code" \
            "on the relay machine run \`amc relay pair\` and copy the command it prints"
    fi

    case "$(uname -s 2>/dev/null || echo unknown)" in
        Linux|Darwin) ;;
        *)
            fail "this installer supports Linux and macOS only" \
                "on Windows run in PowerShell: irm https://raw.githubusercontent.com/inflow-digital/amc/main/installers/install.ps1 | iex"
            ;;
    esac

    original_path="$PATH"
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    export PATH

    # 1. uv (Python tool installer; brings its own Python, no sudo needed)
    if command -v uv >/dev/null 2>&1; then
        say "Using uv: $(command -v uv)"
    else
        say "Installing uv (https://docs.astral.sh/uv/) ..."
        if command -v curl >/dev/null 2>&1; then
            curl -LsSf "$UV_INSTALL_URL" | sh || fail "could not install uv" \
                "install it manually (https://docs.astral.sh/uv/getting-started/installation/) and re-run"
        elif command -v wget >/dev/null 2>&1; then
            wget -qO- "$UV_INSTALL_URL" | sh || fail "could not install uv" \
                "install it manually (https://docs.astral.sh/uv/getting-started/installation/) and re-run"
        else
            fail "neither curl nor wget is available" "install curl (e.g. sudo apt install curl) and re-run"
        fi
        command -v uv >/dev/null 2>&1 || fail "uv was installed but is not on PATH" \
            "open a new terminal and re-run this installer"
    fi

    # 2. AMC itself (--force makes a re-run an upgrade)
    say "Installing AMC from $source_spec ..."
    uv tool install --force --python 3.12 "$source_spec" || fail "uv tool install failed" \
        "check your internet connection, then re-run; details are printed above"

    bin_dir="$(uv tool dir --bin 2>/dev/null || true)"
    [ -n "$bin_dir" ] || bin_dir="$HOME/.local/bin"
    PATH="$bin_dir:$PATH"
    export PATH
    command -v amc >/dev/null 2>&1 || fail "the amc command was not found after installing" \
        "make sure $bin_dir is on your PATH, then run: amc up"
    say "AMC installed: $(command -v amc)"

    case ":$original_path:" in
        *":$bin_dir:"*) path_ok=1 ;;
        *) path_ok="" ;;
    esac

    # 3. start
    if [ -n "$no_start" ]; then
        say "Done. Next: run \`amc up\` (everything on this machine) or \`amc host pair RELAY_URL CODE\`."
    elif [ -n "$pair_url" ]; then
        say "Pairing this machine with $pair_url ..."
        if [ -n "$pair_name" ]; then
            amc host pair "$pair_url" "$pair_code" --name "$pair_name" || fail "pairing failed" \
                "codes expire after 15 minutes: run \`amc relay pair\` on the relay for a new one and re-run"
        else
            amc host pair "$pair_url" "$pair_code" || fail "pairing failed" \
                "codes expire after 15 minutes: run \`amc relay pair\` on the relay for a new one and re-run"
        fi
        say "Starting the AMC host service ..."
        amc service install host || fail "could not start the host service" "run: amc doctor"
        say "Done. This machine is connected. Check it any time with: amc host status"
    else
        say "Setting up AMC on this machine (amc up) ..."
        amc up || fail "amc up did not finish" "run: amc doctor   (it prints the command that fixes each problem)"
    fi

    if [ -z "$path_ok" ]; then
        printf '\n'
        say "To use \`amc\` in new terminals, add it to your PATH once:"
        note "uv tool update-shell"
        note "(or add this line to ~/.profile:  export PATH=\"$bin_dir:\$PATH\")"
    fi
}

# Everything runs from main so a partially downloaded script never executes.
main "$@"
