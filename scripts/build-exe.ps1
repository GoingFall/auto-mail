# Build the portable Windows executable.
#
# IMPORTANT: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 using
# the system code page (GBK on a zh-CN machine), so UTF-8 comments become
# mojibake -- and a swallowed newline can merge a comment into the following
# code line, silently deleting statements.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-exe.ps1
#
# Output: dist\auto-mail\  (a self-contained folder; copy it anywhere)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $Python)) {
    [Console]::Error.WriteLine("Virtualenv interpreter not found: " + $Python)
    [Console]::Error.WriteLine("Run: python -m venv .venv; .venv\Scripts\python.exe -m pip install -e .")
    exit 2
}

Set-Location $ProjectRoot

# PyInstaller must be installed
& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "PyInstaller not found, installing..."
    & $Python -m pip install --quiet pyinstaller
    if ($LASTEXITCODE -ne 0) {
        [Console]::Error.WriteLine("Failed to install PyInstaller")
        exit 2
    }
}

Write-Host "Building..."
& $Python -m PyInstaller automail.spec --noconfirm --log-level WARN
if ($LASTEXITCODE -ne 0) {
    [Console]::Error.WriteLine("Build failed")
    exit 1
}

$Exe = Join-Path $ProjectRoot 'dist\auto-mail\auto-mail.exe'
if (-not (Test-Path $Exe)) {
    [Console]::Error.WriteLine("Expected output not found: " + $Exe)
    exit 1
}

$Size = [math]::Round((Get-Item $Exe).Length / 1MB, 1)
Write-Host ""
Write-Host "Build OK: " -NoNewline
Write-Host $Exe -ForegroundColor Green
Write-Host ("Size: " + $Size + " MB")

# Smoke test: the exe must at least report its version without crashing.
Write-Host "Smoke test..."
$out = & $Exe --version 2>&1
if ($LASTEXITCODE -ne 0) {
    [Console]::Error.WriteLine("Smoke test failed: " + ($out -join ' '))
    exit 1
}
Write-Host ("  OK: " + ($out -join ' '))

# GUI self-test: actually build the window inside the frozen bundle.
#
# This exists because "--version works" proves nothing about the GUI. The same
# trap bit us before: excluding `unittest` from the build broke the calendar at
# runtime because httplib2 imports it at module level -- build succeeded, and
# only the feature itself failed. A missing Tcl/Tk data file behaves identically.
#
# Exit codes are meaningful here:
#   0  GUI builds fine
#   3  no display / no Tk on this machine -- SKIP, do not fail the build.
#      Build machines are often headless; failing there would be a false alarm.
#   2  genuine crash -- MUST fail the build
$GuiExe = Join-Path $ProjectRoot 'dist\auto-mail\auto-mail-gui.exe'
if (Test-Path $GuiExe) {
    Write-Host "GUI self-test..."
    $guiOut = & $GuiExe --selftest 2>&1
    $guiCode = $LASTEXITCODE
    if ($guiCode -eq 0) {
        Write-Host ("  OK: " + ($guiOut -join ' '))
    } elseif ($guiCode -eq 3) {
        Write-Host "  SKIPPED (no usable display on this machine):"
        Write-Host ("    " + ($guiOut -join ' ')) -ForegroundColor Yellow
        Write-Host "  The GUI may still work on a normal desktop." -ForegroundColor Yellow
    } else {
        [Console]::Error.WriteLine("GUI self-test FAILED (exit " + $guiCode + "):")
        [Console]::Error.WriteLine("  " + ($guiOut -join ' '))
        exit 1
    }
} else {
    Write-Host "GUI self-test: skipped (auto-mail-gui.exe not built)" -ForegroundColor Yellow
}

$GuiSize = 0
if (Test-Path $GuiExe) {
    $GuiSize = [math]::Round((Get-Item $GuiExe).Length / 1MB, 1)
}
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Copy dist\auto-mail to where you want it (e.g. D:\Tools\auto-mail)"
Write-Host "  2. Put credentials.json there (and run auto-mail.exe auth)"
Write-Host "  3. Fill in .env (generated on first run from .env.example)"
Write-Host "  4. Optional: register scheduled tasks with scripts\install-tasks-exe.ps1"
Write-Host "  5. Double-click auto-mail-gui.exe for the graphical interface"
if ($GuiSize -gt 0) {
    Write-Host ("     (console exe " + $Size + " MB, GUI exe " + $GuiSize + " MB)")
}
exit 0
