# Register auto-mail scheduled tasks in Windows Task Scheduler.
#
# IMPORTANT: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 using
# the system code page (GBK on a zh-CN machine), so UTF-8 comments become
# mojibake -- and a swallowed newline can merge a comment into the following
# code line, silently deleting statements (including `exit`).
#
# Usage (run once, from the project root):
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-tasks.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-tasks.ps1 -Remove
#
# What it registers:
#   auto-mail startup at logon (+2 min) - catch up on mail received while off
#   auto-mail run     every 30 minutes  - sync + extract + push
#   auto-mail digest  daily at 08:00    - write the daily digest
#   auto-mail audit   daily at 08:30    - read-only extraction review
#   auto-mail backup  weekly Sunday 03:00
#
# Why 30 minutes and not 15: NetEase 163 has no IDLE support, so we must poll.
# Polling too often triggers their risk control and the connection gets dropped.
# The lock in `run` makes overlapping schedules safe, but we still keep the
# interval conservative.
#
# RUN CONDITION: schtasks registers these with the current user account and no
# stored password, so they run ONLY while that user is logged on (Status shows
# "Interactive only"). That is deliberate for a personal tool -- storing a
# password to run while logged off is a bigger security cost than the benefit.
# Consequence: a missed run while the machine is off/asleep is caught up on the
# next run, because sync is incremental and extraction works from local data.
#
# The at-logon task needs admin rights (schtasks limitation). Without them the
# script falls back to the per-user Startup folder, which needs no elevation.

param(
    [switch]$Remove,
    # Polling interval in minutes for the main run task.
    [int]$IntervalMinutes = 30,
    # Time of day for the digest task.
    [string]$DigestTime = '08:00',
    # Minutes to wait after logon before the catch-up run. schtasks requires the
    # delay as mmmm:ss (e.g. 0002:00) -- "2:00" and "02:00" are both rejected.
    [int]$LogonDelayMinutes = 2,
    # Skip the at-logon catch-up entirely.
    [switch]$NoLogon
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Runner = Join-Path $PSScriptRoot 'run.ps1'

if (-not (Test-Path $Runner)) {
    [Console]::Error.WriteLine("Runner script not found: " + $Runner)
    exit 2
}
if (-not (Test-Path $Python)) {
    [Console]::Error.WriteLine("Virtualenv interpreter not found: " + $Python)
    [Console]::Error.WriteLine("Run: python -m venv .venv; .venv\Scripts\python.exe -m pip install -e .")
    exit 2
}
if ($IntervalMinutes -lt 15) {
    [Console]::Error.WriteLine("IntervalMinutes must be >= 15.")
    [Console]::Error.WriteLine("NetEase 163 has no IDLE support, so we must poll; polling more")
    [Console]::Error.WriteLine("often than every 15 minutes triggers their risk control and the")
    [Console]::Error.WriteLine("connection gets dropped.")
    exit 2
}

$TaskRunner = 'powershell -NoProfile -ExecutionPolicy Bypass -File "' + $Runner + '"'

# Write-Error is unusable here: with $ErrorActionPreference = 'Stop' it raises
# a terminating error, so the `exit N` that follows never runs and the script
# reports success (exit 0) despite failing. Write to stderr directly instead so
# the exit code stays under our control.
function Write-Fatal {
    param([string]$Message)
    [Console]::Error.WriteLine($Message)
}

# Non-admin alternative to an ONLOGON scheduled task. See install-tasks-exe.ps1
# for the full rationale: schtasks needs elevation for /sc onlogon, but the
# per-user Startup folder does not, and it achieves the same practical goal.
#
# Defined before use: PowerShell binds a function only when its definition
# statement executes, so a later definition is not callable from earlier code.
$StartupLinkName = 'auto-mail (catch-up at logon).lnk'

function New-StartupEntry {
    param([string]$Runner)

    $startup = [Environment]::GetFolderPath('Startup')
    if (-not $startup) {
        Write-Fatal 'Could not locate the Startup folder.'
        return $null
    }
    $link = Join-Path $startup $StartupLinkName
    $projectRoot = Split-Path -Parent (Split-Path -Parent $Runner)

    try {
        $shell = New-Object -ComObject WScript.Shell
        $sc = $shell.CreateShortcut($link)
        # Run through the same runner script the scheduled tasks use, so the
        # working directory and interpreter pinning stay identical.
        $sc.TargetPath = 'powershell.exe'
        $sc.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $Runner + '" run --apply'
        $sc.WorkingDirectory = $projectRoot
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

function Register-AutoMailTask {
    param(
        [string]$Name,
        [string]$Arguments,
        # Remaining args become individual schtasks switches. Passing them as a
        # single string (e.g. '/sc minute /mo 30') does NOT work: PowerShell
        # hands a native exe that whole string as ONE argument and schtasks
        # rejects it with "invalid argument/option".
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$ScheduleArgs
    )

    # /F overwrites an existing task with the same name (idempotent install).
    $full = @('/create', '/tn', $Name, '/tr', $Arguments, '/F') + $ScheduleArgs
    Write-Host ("Registering: " + $Name)

    # schtasks writes progress to stderr on success too, so merge and check the
    # exit code rather than the stream.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & schtasks @full 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }

    if ($code -ne 0) {
        Write-Fatal ("Failed to register " + $Name + " (exit " + $code + "):")
        Write-Fatal ("  " + ($output -join ' '))
        return $false
    }
    Write-Host ("  OK: " + ($output -join ' '))
    return $true
}

function Test-AutoMailTask {
    param([string]$Name)

    # schtasks writes to stderr when the task does not exist. With
    # $ErrorActionPreference = 'Stop' that stderr gets promoted to a terminating
    # error, which would abort removal on the (normal) "not registered yet"
    # path. Temporarily relax it and silence the stream.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $null = & schtasks /query /tn $Name 2>$null
        return ($LASTEXITCODE -eq 0)
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Remove-AutoMailTask {
    param([string]$Name)

    if (-not (Test-AutoMailTask -Name $Name)) {
        Write-Host ("Not present, skipping: " + $Name)
        return $true
    }

    $output = & schtasks /delete /tn $Name /F 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Fatal ("Failed to remove " + $Name + ": " + ($output -join ' '))
        return $false
    }
    Write-Host ("Removed: " + $Name)
    return $true
}

$RunTask = 'auto-mail run'
$DigestTask = 'auto-mail digest'
$AuditTask = 'auto-mail audit'
$BackupTask = 'auto-mail backup'
$StartupTask = 'auto-mail startup'

if ($Remove) {
    $ok = $true
    foreach ($name in @($StartupTask, $RunTask, $DigestTask, $AuditTask, $BackupTask)) {
        if (-not (Remove-AutoMailTask -Name $name)) { $ok = $false }
    }
    $null = Remove-StartupEntry
    if ($ok) { exit 0 } else { exit 1 }
}

$allOk = $true

# Catch-up at logon: processes whatever accumulated while the machine was off.
# Safe to overlap with the periodic task -- `run` takes a TTL single-instance
# lock and the later one simply reports "already running" (exit 1, not an error).
$logonMethod = 'none'
if (-not $NoLogon) {
    $isAdmin = ([Security.Principal.WindowsPrincipal] `
        [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

    if (-not $isAdmin) {
        Write-Host ''
        Write-Host 'schtasks cannot register an ONLOGON task without admin rights.'
        Write-Host 'Using the current user Startup folder instead (no admin needed):'
        $link = New-StartupEntry -Runner $Runner
        if ($link) { $logonMethod = 'startup-folder' }
    } else {
        # mmmm:ss -- schtasks rejects "02:00" (only 4+ digit minutes accepted).
        $delay = '{0:0000}:00' -f $LogonDelayMinutes
        $allOk = (Register-AutoMailTask -Name $StartupTask `
            -Arguments ($TaskRunner + ' run --apply') `
            /sc onlogon /delay $delay) -and $allOk
        if ($allOk) { $logonMethod = 'onlogon' }
    }
}

# Main pipeline: every N minutes. Runs apply mode because a scheduled task that
# only previews is useless -- but note `run` is still conservative: it only
# pushes events that were already approved, and it never sends mail.
$allOk = (Register-AutoMailTask -Name $RunTask `
    -Arguments ($TaskRunner + ' run --apply') `
    /sc minute /mo $IntervalMinutes) -and $allOk

# Digest: daily. Includes the cancellable auto-push window so the user has
# something to act on.
$allOk = (Register-AutoMailTask -Name $DigestTask `
    -Arguments ($TaskRunner + ' digest') `
    /sc daily /st $DigestTime) -and $allOk

# Audit: daily, 30 minutes after the digest. Read-only review of the last 24
# hours of mail, written to out\audit-<date>.md. It exists so extraction
# quality can actually be iterated on: it surfaces the silent failures
# (prefilter said "extract me" and nothing came out) that the normal pipeline
# reports as success. Never calls the LLM and never touches mail or calendar.
$auditTime = ([datetime]::ParseExact($DigestTime, 'HH:mm', $null)).AddMinutes(30).ToString('HH:mm')
$allOk = (Register-AutoMailTask -Name $AuditTask `
    -Arguments ($TaskRunner + ' audit --hours 24') `
    /sc daily /st $auditTime) -and $allOk

# Weekly backup. Automatic backups only fire when a migration is pending, so a
# regular schedule is needed to actually keep copies.
$allOk = (Register-AutoMailTask -Name $BackupTask `
    -Arguments ($TaskRunner + ' backup') `
    /sc weekly /d SUN /st 03:00) -and $allOk

if ($allOk) {
    Write-Host ''
    Write-Host 'All tasks registered.'
    if ($logonMethod -eq 'onlogon') {
        Write-Host ('  ' + $StartupTask + '  at logon, +' + $LogonDelayMinutes + ' min')
    } elseif ($logonMethod -eq 'startup-folder') {
        Write-Host ('  ' + $StartupTask + '  at logon (Startup folder, no delay)')
    }
    Write-Host ('  ' + $RunTask + '     every ' + $IntervalMinutes + ' minutes')
    Write-Host ('  ' + $DigestTask + '  daily at ' + $DigestTime)
    Write-Host ('  ' + $AuditTask + '   daily at ' + $auditTime + ' (last 24h review)')
    Write-Host ('  ' + $BackupTask + '  weekly, Sunday 03:00')
    Write-Host ''
    Write-Host 'Verify with:  schtasks /query /tn "auto-mail run"'
    Write-Host 'Remove with:  scripts\install-tasks.ps1 -Remove'
    exit 0
} else {
    [Console]::Error.WriteLine('Some tasks failed to register.')
    exit 1
}
