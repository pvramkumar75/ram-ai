# RAM AI installer - per-user, no administrator rights needed.
# Normally started by Install_RAM_AI.bat (which works even where company policy blocks .ps1 scripts).
param([switch]$NoLaunch, [switch]$NoShortcuts, [switch]$Unattended)
$ErrorActionPreference = 'Stop'
$Root = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$Data = Join-Path $env:APPDATA 'RAM_AI'
$Log = Join-Path $Root 'install_log.txt'
try { Start-Transcript -Path $Log -Force | Out-Null } catch {}

function Step($t) { Write-Host "`n==> $t" -ForegroundColor Cyan }
function Ask($q) { if ($Unattended) { return $true }; $a = Read-Host "$q [Y/n]"; return ($a -eq '' -or $a -match '^[Yy]') }
function Fail($msg, $hint) {
    Write-Host "`nSETUP FAILED: $msg" -ForegroundColor Red
    if ($hint) { Write-Host "What to try: $hint" -ForegroundColor Yellow }
    try { Stop-Transcript | Out-Null } catch {}
    exit 1
}

try {
    Write-Host "RAM AI setup  (folder: $Root)" -ForegroundColor Green
    Write-Host ("Windows {0} | {1} | PowerShell {2} | user {3}" -f [Environment]::OSVersion.Version, $env:PROCESSOR_ARCHITECTURE, $PSVersionTable.PSVersion, $env:USERNAME)
    if (-not (Test-Path (Join-Path $Root 'ramai\__main__.py'))) { Fail "The RAM AI files are not next to this installer." "Extract the whole ZIP (right-click > Extract All) and run Install_RAM_AI.bat from the extracted folder." }
    if ($Root -match '\\(OneDrive|Dropbox)[^\\]*\\') { Write-Warning "Installing inside a synced folder (OneDrive/Dropbox) can be slow or lock files. Documents\RAM_AI outside OneDrive is better." }

    # ------------------------------------------------------------ 1. Python 3.11 - 3.13 (newer versions may lack packages)
    Step "Checking Python"
    function Test-Py($exe, $extra) {
        try {
            $out = & $exe @extra -c "import sys,struct; v=sys.version_info; print((3,11)<=v[:2]<=(3,13), struct.calcsize('P')*8, sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out -match '^True 64 (.+)$') { return $Matches[1].Trim() }
        } catch {}
        return $null
    }
    function Get-Py {
        foreach ($v in '3.12', '3.13', '3.11') { $p = Test-Py 'py' @("-$v"); if ($p) { return $p } }
        foreach ($v in '312', '313', '311') {
            $c = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$v\python.exe"
            if (Test-Path $c) { $p = Test-Py $c @(); if ($p) { return $p } }
        }
        $p = Test-Py 'python' @(); if ($p) { return $p }
        return $null
    }
    $py = Get-Py
    if (-not $py) {
        Write-Host "A suitable 64-bit Python (3.11-3.13) was not found."
        if (-not (Ask "Install Python 3.12 for your user only (from python.org, accepts the Python licence)?")) { Fail "Python is required." "Install Python 3.12 (64-bit) from python.org, then run setup again." }
        $ok = $false
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements --disable-interactivity
            $py = Get-Py; $ok = [bool]$py
        }
        if (-not $ok) {
            Write-Host "Downloading the official Python installer from python.org..."
            $exe = Join-Path $env:TEMP 'python-3.12.10-amd64.exe'
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile $exe -UseBasicParsing
            $sig = Get-AuthenticodeSignature $exe
            if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Python Software Foundation') { Fail "The downloaded Python installer is not signed by the Python Software Foundation; not running it." "Install Python 3.12 manually from python.org." }
            Start-Process -FilePath $exe -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=0', 'Include_launcher=1', 'Include_test=0' -Wait
            $py = Get-Py
        }
        if (-not $py) { Fail "Python could not be installed automatically." "Install Python 3.12 (64-bit) from python.org (tick 'Add to PATH'), then run setup again." }
    }
    Write-Host "Using $py"

    # ------------------------------------------------------------ 2. RAM AI environment
    Step "Installing RAM AI (first time: 2-5 minutes, needs internet)"
    $venv = Join-Path $Root '.venv'
    $vpy = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Path $vpy)) { & $py -m venv $venv; if ($LASTEXITCODE -ne 0) { Fail "Could not create the Python environment." "Check that $Root is not read-only, then retry." } }
    & $vpy -m pip install --upgrade pip -q --disable-pip-version-check 2>&1 | Out-Host
    & $vpy -m pip install -r (Join-Path $Root 'requirements.txt') --prefer-binary --disable-pip-version-check 2>&1 | Select-Object -Last 15 | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Fail "Downloading/installing packages failed." ("If this is an office network with a proxy, ask IT for the proxy address and run in this window:  setx HTTPS_PROXY http://proxy:port  - then run setup again. " +
              "Otherwise check the internet connection. Details are in install_log.txt.")
    }
    & $vpy -c "import ramai.server, ramai.app; print('RAM AI modules OK')"
    if ($LASTEXITCODE -ne 0) { Fail "RAM AI was installed but does not start (import check failed)." "Send install_log.txt to whoever shared RAM AI." }

    # ------------------------------------------------------------ 2b. `ramai` terminal command on the user's PATH
    Step "Adding the 'ramai' command"
    $bin = Join-Path $Root 'bin'
    New-Item -ItemType Directory -Force -Path $bin | Out-Null
    # PYTHONPATH points at the install folder so `ramai` works from any current folder (setlocal keeps it out of the user's shell)
    "@echo off`r`nsetlocal`r`nset `"PYTHONPATH=%~dp0..`"`r`n`"%~dp0..\.venv\Scripts\python.exe`" -m ramai.cli %*`r`n" | Set-Content -Encoding ASCII (Join-Path $bin 'ramai.cmd')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not (($userPath -split ';') -contains $bin)) {
        [Environment]::SetEnvironmentVariable('Path', (($userPath.TrimEnd(';') + ';' + $bin).TrimStart(';')), 'User')
        Write-Host "Added $bin to your PATH (open a NEW terminal to use 'ramai')."
    }

    # ------------------------------------------------------------ 3. Agent workspace (Excel/Word/PowerPoint/data libraries)
    Step "Setting up the agent's Python workspace"
    $ws = Join-Path $Data 'workspace\.venv'
    New-Item -ItemType Directory -Force -Path (Split-Path $ws) | Out-Null
    if (-not (Test-Path (Join-Path $ws 'Scripts\python.exe'))) { & $py -m venv $ws }
    & (Join-Path $ws 'Scripts\python.exe') -m pip install -r (Join-Path $Root 'requirements-workspace.txt') --prefer-binary -q --disable-pip-version-check 2>&1 | Select-Object -Last 5 | Out-Host
    if ($LASTEXITCODE -ne 0) { Write-Warning "Workspace packages did not fully install; RAM AI will retry when it first needs them." }

    # ------------------------------------------------------------ 4. Shortcuts
    $pyw = Join-Path $venv 'Scripts\pythonw.exe'
    $ico = Join-Path $Root 'ramai\ui\ramai.ico'
    if (-not $NoShortcuts) {
        Step "Creating shortcuts (Desktop + Start menu)"
        $sh = New-Object -ComObject WScript.Shell
        foreach ($dir in @([Environment]::GetFolderPath('Desktop'), (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))) {
            try {
                $lnk = $sh.CreateShortcut((Join-Path $dir 'RAM AI.lnk'))
                $lnk.TargetPath = $pyw; $lnk.Arguments = '-m ramai'; $lnk.WorkingDirectory = $Root
                if (Test-Path $ico) { $lnk.IconLocation = $ico }
                $lnk.Description = 'RAM AI desktop agent'; $lnk.Save()
            } catch { Write-Warning "Could not create a shortcut in $dir : $_" }
        }
        if (Ask "Start RAM AI automatically when you sign in to Windows (needed for scheduled tasks)?") {
            $start = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup\RAM AI.cmd'
            "@echo off`r`ncd /d `"$Root`"`r`nstart `"`" /min `"$pyw`" -m ramai`r`n" | Set-Content -Encoding ASCII $start
        }
    }

    # ------------------------------------------------------------ 5. Launch
    Write-Host "`nRAM AI is installed." -ForegroundColor Green
    Write-Host "On first start it asks for your name, a name for this computer and your AI key (DeepSeek, OpenAI or Sarvam)."
    Write-Host "Use the 'RAM AI' shortcut on your Desktop, or in a NEW terminal:  ramai `"summarise today's mail`""
    if (-not $NoLaunch) { Start-Process -FilePath $pyw -ArgumentList '-m ramai' -WorkingDirectory $Root }
    try { Stop-Transcript | Out-Null } catch {}
    exit 0
}
catch {
    Fail ("Unexpected error: " + $_.Exception.Message + " (line " + $_.InvocationInfo.ScriptLineNumber + ")") "Send install_log.txt to whoever shared RAM AI."
}
