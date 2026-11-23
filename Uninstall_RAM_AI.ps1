# RAM AI uninstaller - removes shortcuts, autostart and the program's Python environment.
# Your data (settings, encrypted keys, history, memory, schedules) is kept unless you choose to delete it.
$ErrorActionPreference = 'Continue'
$Root = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$Data = Join-Path $env:APPDATA 'RAM_AI'

Write-Host "Stopping RAM AI..."
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -match '-m ramai' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Write-Host "Removing shortcuts and autostart..."
foreach ($p in @((Join-Path ([Environment]::GetFolderPath('Desktop')) 'RAM AI.lnk'),
                 (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\RAM AI.lnk'),
                 (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\RAM AI.cmd'))) {
    if (Test-Path $p) { Remove-Item $p -Force }
}

$bin = Join-Path $Root 'bin'
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath) { [Environment]::SetEnvironmentVariable('Path', (($userPath -split ';' | Where-Object { $_ -and $_ -ne $bin }) -join ';'), 'User') }

Write-Host "Removing the program environment..."
if (Test-Path (Join-Path $Root '.venv')) { Remove-Item (Join-Path $Root '.venv') -Recurse -Force }
if (Test-Path (Join-Path $Data 'workspace')) { Remove-Item (Join-Path $Data 'workspace') -Recurse -Force }

$a = Read-Host "Also delete your RAM AI data (settings, encrypted API keys, history, memory, schedules)? This cannot be undone. [y/N]"
if ($a -match '^[Yy]') { Remove-Item $Data -Recurse -Force; Write-Host "Data deleted." } else { Write-Host "Data kept in $Data" }
Write-Host "RAM AI has been uninstalled. You can now delete the folder: $Root" -ForegroundColor Green
