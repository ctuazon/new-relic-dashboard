<#
Sets up the New Relic Anomaly Monitor on this machine:
  - creates the virtual environment and installs dependencies (if missing)
  - creates .env from .env.example (if missing)
  - (re)generates the futuristic app icon
  - creates/updates the Desktop shortcut that launches the app with no console window

Safe to re-run any time (e.g. after moving the project folder, or to refresh
the icon/shortcut) - it will not overwrite an existing .env or venv.

Run from the project folder:
    powershell -ExecutionPolicy Bypass -File install.ps1
or just double-click install.bat.
#>

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

Write-Host "== New Relic Anomaly Monitor setup ==" -ForegroundColor Cyan

# 1. Virtual environment
$venvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating virtual environment..." -ForegroundColor Cyan
    python -m venv .venv
} else {
    Write-Host "Virtual environment already exists, skipping creation." -ForegroundColor DarkGray
}

# 2. Dependencies
Write-Host "Installing dependencies..." -ForegroundColor Cyan
& $venvPython -m pip install --quiet --upgrade pip
& $venvPython -m pip install --quiet -r requirements.txt

# 3. .env
$envPath = Join-Path $ProjectDir ".env"
$envExamplePath = Join-Path $ProjectDir ".env.example"
if (-not (Test-Path $envPath)) {
    Copy-Item $envExamplePath $envPath
    Write-Host "Created .env from template - add your New Relic API key before launching." -ForegroundColor Yellow
} else {
    Write-Host ".env already exists, leaving it untouched." -ForegroundColor DarkGray
}

# 4. Icon
Write-Host "Generating app icon..." -ForegroundColor Cyan
& $venvPython (Join-Path $ProjectDir "assets\generate_icon.py")

# 5. Desktop shortcut
Write-Host "Creating Desktop shortcut..." -ForegroundColor Cyan
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop "New Relic Anomaly Monitor.lnk"
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($shortcutPath)
$s.TargetPath = Join-Path $ProjectDir ".venv\Scripts\pythonw.exe"
$s.Arguments = '"main.py"'
$s.WorkingDirectory = $ProjectDir
$s.IconLocation = (Join-Path $ProjectDir "assets\app_icon.ico") + ",0"
$s.Description = "Launch the New Relic Anomaly Monitor"
$s.WindowStyle = 1
$s.Save()

Write-Host ""
Write-Host "Setup complete!" -ForegroundColor Green
Write-Host "Desktop shortcut: $shortcutPath"

$envContent = Get-Content $envPath -Raw
if ($envContent -match "NEW_RELIC_API_KEY=\s*(\r?\n|$)") {
    Write-Host "Reminder: NEW_RELIC_API_KEY is empty in .env - add it before launching." -ForegroundColor Yellow
}
