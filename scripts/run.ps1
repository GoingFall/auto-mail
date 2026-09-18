# auto-mail scheduled-task entry point.
#
# IMPORTANT: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 files
# using the system code page (GBK on a zh-CN machine), so UTF-8 comments become
# mojibake -- and a swallowed newline can merge a comment into the following
# code line, silently deleting statements (including `exit`).
#
# Pins the interpreter path and working directory so scheduled tasks do not
# depend on the ambient environment.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run.ps1 sync
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run.ps1 digest

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('doctor', 'runs', 'auth', 'sync', 'extract', 'audit', 'mark-read', 'pause', 'push', 'events', 'threads', 'digest', 'stats', 'setup', 'backup', 'run')]
    [string]$Command,

    # Extra arguments forwarded to automail, e.g. --apply
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$ErrorActionPreference = 'Stop'

# Project root is the parent of this script's directory.
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $Python)) {
    Write-Error ("Virtualenv interpreter not found: " + $Python + "`n" +
                 "Run: python -m venv .venv; .venv\Scripts\python.exe -m pip install -e .")
    exit 2
}

Set-Location $ProjectRoot

$exe = Join-Path $ProjectRoot '.venv\Scripts\automail.exe'
if (Test-Path $exe) {
    & $exe $Command @ExtraArgs
} else {
    & $Python -m automail.cli $Command @ExtraArgs
}

# Propagate the exit code verbatim (0 ok / 1 partial / 2 fatal) so Task
# Scheduler can judge the result. Capture it first: a later statement could
# otherwise clobber $LASTEXITCODE.
$exitCode = $LASTEXITCODE
exit $exitCode
