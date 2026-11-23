# RAM AI one-line installer / updater
#   irm https://raw.githubusercontent.com/pvramkumar75/ram-ai/main/get.ps1 | iex
# Downloads the latest GitHub release, verifies its SHA-256 checksum, installs to %LOCALAPPDATA%\RAM_AI\app and runs setup.
# Options (set before running): $env:RAMAI_INSTALL_DIR = 'D:\Tools\RAM_AI'   $env:RAMAI_VERSION = 'v0.2.3'   $env:RAMAI_UNATTENDED = '1'
& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $Repo = 'pvramkumar75/ram-ai'
    $Dir = if ($env:RAMAI_INSTALL_DIR) { $env:RAMAI_INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA 'RAM_AI\app' }
    $Tag = $env:RAMAI_VERSION
    Write-Host "RAM AI installer" -ForegroundColor Green

    # 1. find the release
    $api = if ($Tag) { "https://api.github.com/repos/$Repo/releases/tags/$Tag" } else { "https://api.github.com/repos/$Repo/releases/latest" }
    $rel = Invoke-RestMethod $api -Headers @{ 'User-Agent' = 'ramai-installer' }
    $zipAsset = $rel.assets | Where-Object { $_.name -like 'RAM_AI_v*.zip' } | Select-Object -First 1
    $sumAsset = $rel.assets | Where-Object { $_.name -eq 'SHA256SUMS.txt' } | Select-Object -First 1
    if (-not $zipAsset -or -not $sumAsset) { throw "Release $($rel.tag_name) is missing its ZIP or SHA256SUMS.txt" }
    Write-Host "Latest release: $($rel.tag_name)"

    # 2. download + verify checksum
    $tmp = Join-Path $env:TEMP ("ramai_" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    $zip = Join-Path $tmp $zipAsset.name
    Invoke-WebRequest $zipAsset.browser_download_url -OutFile $zip -UseBasicParsing
    $sums = (Invoke-WebRequest $sumAsset.browser_download_url -UseBasicParsing).Content
    if ($sums -is [byte[]]) { $sums = [Text.Encoding]::UTF8.GetString($sums) }
    $expected = ($sums -split "`n" | Where-Object { $_ -match [regex]::Escape($zipAsset.name) } | ForEach-Object { ($_ -split '\s+')[0] }) | Select-Object -First 1
    $actual = (Get-FileHash $zip -Algorithm SHA256).Hash
    if (-not $expected -or $actual -ne $expected.Trim().ToUpper()) { Remove-Item $tmp -Recurse -Force; throw "Checksum mismatch - download refused (expected $expected, got $actual)" }
    Write-Host "Checksum verified." -ForegroundColor Green

    # 3. extract and copy over the install folder (keeps the existing .venv so updates are quick)
    Expand-Archive $zip -DestinationPath $tmp -Force
    $src = Join-Path $tmp 'RAM_AI'
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    $full = [IO.Path]::GetFullPath($Dir).TrimEnd('\') + '\'
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($full, [StringComparison]::OrdinalIgnoreCase) -and $_.CommandLine -match '-m ramai(\s|$)' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }   # stop only the copy being updated (other installs keep running)
    robocopy $src $Dir /E /NFL /NDL /NJH /NJS /NP /XD .venv /XF install_log.txt | Out-Null
    Get-ChildItem -LiteralPath $Dir -Recurse -File | Unblock-File
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Files installed to $Dir"

    # 4. run setup (works even where company policy disables .ps1 scripts)
    Push-Location $Dir
    try {
        $opts = @{}   # hashtable splat: switches must be passed by name (an array would pass them as plain strings)
        if ($env:RAMAI_UNATTENDED) { $opts = @{ Unattended = $true; NoShortcuts = $true; NoLaunch = $true } }
        & ([scriptblock]::Create((Get-Content -Raw -LiteralPath (Join-Path $Dir 'Install_RAM_AI.ps1')))) @opts
    } finally { Pop-Location }
}
