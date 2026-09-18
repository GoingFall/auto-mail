# Register auto-mail scheduled tasks for the PACKAGED exe.
#
# IMPORTANT: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 using
# the system code page (GBK on a zh-CN machine), so UTF-8 comments become
# mojibake -- and a swallowed newline can merge a comment into the following
# code line, silently deleting statements (including `exit`).
#
# Difference from install-tasks.ps1: that one runs through the project's
# virtualenv (for development); this one runs the built exe directly, so it
# works on a machine without Python installed.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-tasks-exe.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-tasks-exe.ps1 -Remove
#   ... -ExeDir D:\Tools\auto-mail            # if you moved the folder
#
# Registers:
#   auto-mail startup at logon (+2 min) -- catches up on mail received while off
#   auto-mail run     every 30 minutes  -- sync + extract + push
#   auto-mail digest  daily at 08:00
#   auto-mail audit   daily at 08:30 (read-only extraction review)
#   auto-mail backup  weekly, Sunday 03:00
#
# Why an "at logon" task instead of a true boot-time task: schtasks can register
# ONSTART, but that requires storing an account password (or running as SYSTEM),
# which is a bigger security cost than the benefit for a personal tool. The
# logon task achieves the practical goal: whatever accumulated while the machine
# was off gets processed shortly after you log in. (schtasks has no
# "StartWhenAvailable" option -- missed runs are simply skipped -- so an
# explicit logon trigger is the way to catch up.)
#
# Why 30 minutes and not 15: NetEase 163 has no IDLE support, so we must poll.
# Polling too often triggers their risk control and the connection gets dropped.

param(
    [switch]$Remove,
    [int]$IntervalMinutes = 30,
    [string]$DigestTime = '08:00',
    # Minutes to wait after logon before the catch-up run. schtasks requires the
    # delay as mmmm:ss (e.g. 0002:00) -- "2:00" and "02:00" are both rejected.
    [int]$LogonDelayMinutes = 2,
    # Skip the at-logon task entirely (it needs administrator rights).
    [switch]$NoLogon,
    # Folder containing auto-mail.exe. Defaults to dist\auto-mail next to this script.
    [string]$ExeDir
)

$ErrorActionPreference = 'Stop'

# Write-Error is unusable here: with $ErrorActionPreference = 'Stop' it raises a
# terminating error, so the `exit N` that follows never runs and the script
# reports success (exit 0) despite failing.
function Write-Fatal {
    param([string]$Message)
    [Console]::Error.WriteLine($Message)
}

# Non-admin alternative to an ONLOGON scheduled task.
#
# schtasks refuses /sc onlogon for a non-elevated account ("Access is denied"),
# but the per-user Startup folder runs programs at logon with NO admin rights --
# that is the standard Windows mechanism for this, and the reason a fallback is
# possible at all rather than simply telling the user to elevate.
#
# Defined BEFORE the -Remove block: PowerShell only binds a function when its
# definition statement executes, so a later definition is not callable earlier
# (verified -- it errors with "not recognized as a cmdlet").
#
# A .lnk (not a .cmd) is used so the window starts minimized with no console
# flashing at every logon. The shortcut's working directory must be the exe
# folder, otherwise .env / data / out / logs would resolve elsewhere.
$StartupLinkName = 'auto-mail (catch-up at logon).lnk'

function New-StartupEntry {
    param([string]$ExePath)

    $startup = [Environment]::GetFolderPath('Startup')
    if (-not $startup) {
        Write-Fatal 'Could not locate the Startup folder.'
        return $null
    }
    $link = Join-Path $startup $StartupLinkName

    try {
        $shell = New-Object -ComObject WScript.Shell
        $sc = $shell.CreateShortcut($link)
        $sc.TargetPath = $ExePath
        $sc.Arguments = 'run --apply'
        $sc.WorkingDirectory = (Split-Path -Parent $ExePath)
        $sc.WindowStyle = 7   # 7 = minimized (avoid a console window each logon)
        $sc.Description = 'auto-mail: process mail received while the PC was off'
        $sc.Save()
    } catch {
        Write-Fatal ('Failed to create startup shortcut: ' + $_.Exception.Message)
        return $null
    }
    Write-Host ('  startup shortcut: ' + $link)
    return $link
}

function Remove-StartupEntry {
    $startup = [Environment]::GetFolderPath('Startup')
    if (-not $startup) { return $true }
    $link = Join-Path $startup $StartupLinkName
    if (Test-Path $link) {
        Remove-Item -Path $link -Force
        Write-Host ('Removed: ' + $link)
    } else {
        Write-Host ('Not present, skipping: ' + $link)
    }
    return $true
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $ExeDir) {
    $ExeDir = Join-Path $ProjectRoot 'dist\auto-mail'
}
$Exe = Join-Path $ExeDir 'auto-mail.exe'

if ($Remove) {
    $ok = $true
    foreach ($name in @('auto-mail startup', 'auto-mail run', 'auto-mail digest', 'auto-mail audit', 'auto-mail backup')) {
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $null = & schtasks /query /tn $name 2>$null
            if ($LASTEXITCODE -ne 0) {
                Write-Host ("Not present, skipping: " + $name)
                continue
            }
            $out = & schtasks /delete /tn $name /F 2>&1
            if ($LASTEXITCODE -ne 0) {
                Write-Fatal ("Failed to remove " + $name + ": " + ($out -join ' '))
                $ok = $false
            } else {
                Write-Host ("Removed: " + $name)
            }
        } finally {
            $ErrorActionPreference = $previous
        }
    }
    $null = Remove-StartupEntry
    if ($ok) { exit 0 } else { exit 1 }
}

if (-not (Test-Path $Exe)) {
    Write-Fatal ("Executable not found: " + $Exe)
    Write-Fatal "Build it first: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-exe.ps1"
    Write-Fatal "Or pass -ExeDir <folder containing auto-mail.exe>"
    exit 2
}
if ($IntervalMinutes -lt 15) {
    Write-Fatal "IntervalMinutes must be >= 15."
    Write-Fatal "NetEase 163 has no IDLE support, so we must poll; polling more often"
    Write-Fatal "than every 15 minutes triggers their risk control and drops the connection."
    exit 2
}

# Working directory must be the exe folder: that is where data/ out/ logs/ and
# .env live (app_base_dir resolves to the exe's directory when frozen).
# /TR cannot set a working directory, so wrap via cmd /c with a cd.
# Why a wrapper .cmd instead of putting `cmd /c "cd /d ... && ..."` in /TR:
# schtasks.exe receives the /TR value through PowerShell's native-argument
# marshalling, and the embedded `&&` leaks out as a schtasks argument:
#   "invalid argument/option - '&&'"
# (Verified: both plain double quotes and --% escaping fail.) A one-line
# wrapper file sidesteps all quote layers and is also easier for the user to
# inspect and run by hand.
function New-RunWrapper {
    param([string]$Directory, [string]$ExePath)

    $wrapper = Join-Path $Directory 'auto-mail-run.cmd'
    # CRLF line endings: .cmd files must not rely on LF.
    $lines = @(
        '@echo off',
        'rem Generated by install-tasks-exe.ps1 -- do not edit by hand.',
        'rem Runs auto-mail with the given arguments from the exe directory,',
        'rem so data/ out/ logs/ and .env resolve next to the exe.',
        'cd /d "%~dp0"',
        '"%~dp0auto-mail.exe" %*',
        'exit /b %ERRORLEVEL%'
    )
    $content = ($lines -join "`r`n") + "`r`n"
    Set-Content -Path $wrapper -Value $content -Encoding ASCII -NoNewline
    Write-Host ("  wrapper: " + $wrapper)
    return $wrapper
}

function Register-Task {
    param(
        [string]$Name,
        [string]$Wrapper,
        [string]$Arguments,
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$ScheduleArgs
    )

    # /TR points at the wrapper; arguments go inside it via the wrapper's "%*".
    # No quotes and no && in /TR, so no escaping problems.
    $tr = '"' + $Wrapper + '" ' + $Arguments

    $full = @('/create', '/tn', $Name, '/tr', $tr, '/F') + $ScheduleArgs
    Write-Host ("Registering: " + $Name)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & schtasks @full 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }

    if ($code -ne 0) {
        Write-Fatal ("Failed to register " + $Name + " (exit " + $code + "):")
        Write-Fatal ("  " + ($out -join ' '))
        return $false
    }
    Write-Host ("  OK: " + ($out -join ' '))
    return $true
}

# One wrapper shared by all tasks (arguments are appended per task).
$wrapper = New-RunWrapper -Directory $ExeDir -ExePath $Exe
Write-Host ""

$allOk = $true

# Catch-up run at logon: processes whatever accumulated while the machine was
# off. Delayed by a couple of minutes so the network is up. Safe to overlap with
# the periodic task -- `run` takes a TTL single-instance lock and the later one
# simply reports "already running" (exit 1, not an error).
#
# Which mechanism ended up in use decides how the summary below is worded --
# the Startup folder cannot express a delay, so claiming "+2 min" there would
# be wrong.
$logonMethod = 'none'
if (-not $NoLogon) {
    # ONLOGON tasks require administrator rights -- a non-elevated account gets
    # "Access is denied" from schtasks, which is not a bug in this script.
    # Detect it up front so the message is actionable, and do NOT count it as a
    # failure of the whole install: the periodic tasks still cover the need
    # (worst case a few minutes of latency after logon instead of immediate).
    $isAdmin = ([Security.Principal.WindowsPrincipal] `
        [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

    if (-not $isAdmin) {
        # Fall back to the per-user Startup folder, which needs NO admin rights.
        # This gives the same practical outcome (catch-up at logon) without
        # asking the user to elevate -- elevating a personal mail tool just to
        # register a logon hook is a worse trade than using the documented
        # per-user mechanism.
        Write-Host ''
        Write-Host 'schtasks cannot register an ONLOGON task without admin rights.'
        Write-Host 'Using the current user Startup folder instead (no admin needed):'
        $link = New-StartupEntry -ExePath $Exe
        if ($link) {
            $logonMethod = 'startup-folder'
        } else {
            Write-Host ''
            Write-Host '  Startup entry was not created. The periodic run task still works;'
            Write-Host '  mail received while the PC was off is picked up within one'
            Write-Host ('  interval (' + $IntervalMinutes + ' min) after logon.')
        }
    } else {
        # mmmm:ss -- schtasks rejects "02:00" (only 4+ digit minutes accepted).
        $delay = '{0:0000}:00' -f $LogonDelayMinutes
        $allOk = (Register-Task -Name 'auto-mail startup' `
            -Wrapper $wrapper -Arguments 'run --apply' `
            /sc onlogon /delay $delay) -and $allOk
        if ($allOk) { $logonMethod = 'onlogon' }
    }
}

# --apply because a scheduled task that only previews is useless. `run` is still
# conservative: it only pushes already-approved events and never sends mail.
$allOk = (Register-Task -Name 'auto-mail run' `
    -Wrapper $wrapper -Arguments 'run --apply' `
    /sc minute /mo $IntervalMinutes) -and $allOk

$allOk = (Register-Task -Name 'auto-mail digest' `
    -Wrapper $wrapper -Arguments 'digest' `
    /sc daily /st $DigestTime) -and $allOk

# Audit: read-only review of the last 24h, written to out\audit-<date>.md.
# Exists so extraction quality can be iterated on: it surfaces the silent
# failures (prefilter said "extract me" and nothing came out) that the normal
# pipeline reports as success. Never calls the LLM, never touches mail/calendar.
$auditTime = ([datetime]::ParseExact($DigestTime, 'HH:mm', $null)).AddMinutes(30).ToString('HH:mm')
$allOk = (Register-Task -Name 'auto-mail audit' `
    -Wrapper $wrapper -Arguments 'audit --hours 24' `
    /sc daily /st $auditTime) -and $allOk

$allOk = (Register-Task -Name 'auto-mail backup' `
    -Wrapper $wrapper -Arguments 'backup' `
    /sc weekly /d SUN /st 03:00) -and $allOk

if ($allOk) {
    Write-Host ''
    Write-Host 'All tasks registered.'
    if ($logonMethod -eq 'onlogon') {
        Write-Host ('  auto-mail startup at logon, +' + $LogonDelayMinutes + ' min')
    } elseif ($logonMethod -eq 'startup-folder') {
        Write-Host '  auto-mail catch-up at logon (Startup folder, no delay)'
    }
    Write-Host ('  auto-mail run     every ' + $IntervalMinutes + ' minutes')
    Write-Host ('  auto-mail digest  daily at ' + $DigestTime)
    Write-Host ('  auto-mail audit   daily at ' + $auditTime + ' (last 24h review)')
    Write-Host '  auto-mail backup  weekly, Sunday 03:00'
    Write-Host ''
    Write-Host ('Executable: ' + $Exe)
    Write-Host 'Verify with:  schtasks /query /tn "auto-mail run"'
    Write-Host 'Remove with:  scripts\install-tasks-exe.ps1 -Remove'
    exit 0
} else {
    Write-Fatal 'Some tasks failed to register.'
    exit 1
}
