# AMC (Agent MCP Commander) installer for Windows (PowerShell 5.1+ or PowerShell 7).
#
#   irm https://raw.githubusercontent.com/inflow-digital/amc/main/installers/install.ps1 | iex
#       install AMC and run `amc up` (relay + this machine + agent keys, as background tasks)
#
#   $env:AMC_PAIR_URL = "https://relay.example.com"; $env:AMC_PAIR_CODE = "CODE"
#   irm https://raw.githubusercontent.com/inflow-digital/amc/main/installers/install.ps1 | iex
#       install AMC and connect this machine to an existing relay ($env:AMC_PAIR_NAME optional)
#
#   $env:AMC_NO_START = "1"; irm .../install.ps1 | iex
#       only install the `amc` command
#
# When run as a file the same options are parameters:
#   .\install.ps1 -PairUrl https://relay.example.com -PairCode CODE [-Name NAME] [-NoStart]
#
# $env:AMC_SOURCE overrides what `uv tool install` installs (git URL, local path or wheel).
# Re-running the installer upgrades AMC in place.

param(
    [string]$PairUrl = "",
    [string]$PairCode = "",
    [string]$Name = "",
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"

function Install-Amc {
    param([string]$PairUrl, [string]$PairCode, [string]$Name, [bool]$NoStart)

    $DefaultSource = "amc-commander@git+https://github.com/inflow-digital/amc"
    $RunningAsFile = [bool]$PSCommandPath

    # Native tools (uv, amc) write progress to stderr. Under "Stop", Windows PowerShell 5.1 turns
    # redirected stderr lines into terminating errors, so run them with "Continue" and rely on
    # the exit code instead.
    function Invoke-Native([scriptblock]$Command) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try { & $Command } finally { $ErrorActionPreference = $previous }
        return $LASTEXITCODE
    }
    function Say([string]$Text) { Write-Host "==> $Text" -ForegroundColor Cyan }
    function Note([string]$Text) { Write-Host "    $Text" }
    function Fail([string]$Problem, [string]$Fix = "") {
        Write-Host ""
        Write-Host "AMC install failed: $Problem" -ForegroundColor Red
        if ($Fix) { Write-Host "Fix: $Fix" -ForegroundColor Yellow }
        # `exit` would close the user's PowerShell window when piped into iex.
        if ($RunningAsFile) { exit 1 }
        throw "AMC install failed: $Problem"
    }

    if (-not $PairUrl -and $env:AMC_PAIR_URL) { $PairUrl = $env:AMC_PAIR_URL }
    if (-not $PairCode -and $env:AMC_PAIR_CODE) { $PairCode = $env:AMC_PAIR_CODE }
    if (-not $Name -and $env:AMC_PAIR_NAME) { $Name = $env:AMC_PAIR_NAME }
    if (-not $NoStart -and $env:AMC_NO_START -and $env:AMC_NO_START -ne "0") { $NoStart = $true }
    $Source = if ($env:AMC_SOURCE) { $env:AMC_SOURCE } else { $DefaultSource }

    if ($PairUrl -and -not $PairCode) {
        Fail "a pairing URL was given without a pairing code" `
            "on the relay machine run 'amc relay pair' and copy the command it prints"
    }

    $LocalBin = Join-Path $env:USERPROFILE ".local\bin"
    $OriginalPath = $env:Path
    if (($env:Path -split ";") -notcontains $LocalBin) { $env:Path = "$LocalBin;$env:Path" }

    # 1. uv (Python tool installer; brings its own Python, no admin needed)
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        Say "Using uv: $((Get-Command uv).Source)"
    } else {
        Say "Installing uv (https://docs.astral.sh/uv/) ..."
        try {
            $code = Invoke-Native { powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex" | Out-Host }
            if ($code -ne 0) { throw "uv installer exited with $code" }
        } catch {
            Fail "could not install uv" "install it manually (https://docs.astral.sh/uv/getting-started/installation/) and re-run"
        }
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            Fail "uv was installed but is not on PATH" "open a new PowerShell window and re-run this installer"
        }
    }

    # 2. AMC itself (--force makes a re-run an upgrade)
    # Upgrading: a running AMC service keeps its files locked on Windows, so stop it first
    # (amc up / pairing below starts it again).
    $running = @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like "AMC *" })
    if ($running.Count -gt 0) {
        Say "Stopping the running AMC service(s) for the upgrade ..."
        $running | Stop-ScheduledTask -ErrorAction SilentlyContinue
        Get-Process -Name amc -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
    Say "Installing AMC from $Source ..."
    $code = Invoke-Native { & uv tool install --force --python 3.12 $Source | Out-Host }
    if ($code -ne 0) {
        Fail "uv tool install failed" "check your internet connection, then re-run; details are printed above"
    }

    $BinDir = ""
    try { $BinDir = (& uv tool dir --bin 2>$null) } catch { $BinDir = "" }
    if (-not $BinDir) { $BinDir = $LocalBin }
    $BinDir = "$BinDir".Trim()
    if (($env:Path -split ";") -notcontains $BinDir) { $env:Path = "$BinDir;$env:Path" }
    $Amc = Get-Command amc -ErrorAction SilentlyContinue
    if (-not $Amc) { Fail "the amc command was not found after installing" "add $BinDir to your PATH, then run: amc up" }
    Say "AMC installed: $($Amc.Source)"

    # 3. start
    if ($NoStart) {
        Say "Done. Next: run 'amc up' (everything on this machine) or 'amc host pair RELAY_URL CODE'."
    } elseif ($PairUrl) {
        Say "Pairing this machine with $PairUrl ..."
        if ($Name) {
            $code = Invoke-Native { & amc host pair $PairUrl $PairCode --name $Name | Out-Host }
        } else {
            $code = Invoke-Native { & amc host pair $PairUrl $PairCode | Out-Host }
        }
        if ($code -ne 0) {
            Fail "pairing failed" "codes expire after 15 minutes: run 'amc relay pair' on the relay for a new one and re-run"
        }
        Say "Starting the AMC host service ..."
        $code = Invoke-Native { & amc service install host | Out-Host }
        if ($code -ne 0) { Fail "could not start the host service" "run: amc doctor" }
        Say "Done. This machine is connected. Check it any time with: amc host status"
    } else {
        Say "Setting up AMC on this machine (amc up) ..."
        $code = Invoke-Native { & amc up | Out-Host }
        if ($code -ne 0) { Fail "amc up did not finish" "run: amc doctor   (it prints the command that fixes each problem)" }
    }

    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ((($UserPath -split ";") -notcontains $BinDir) -and (($OriginalPath -split ";") -notcontains $BinDir)) {
        Write-Host ""
        Say "To use 'amc' in new windows, add it to your PATH once:"
        Note "uv tool update-shell"
    }
}

Install-Amc -PairUrl $PairUrl -PairCode $PairCode -Name $Name -NoStart ([bool]$NoStart)
