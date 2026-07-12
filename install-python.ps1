<#
  install-python.ps1 - best-effort, NON-ADMIN Python 3 install for Claude Overlay's setup.cmd.

  Strategy:
    1) winget (user scope, no admin) if winget is available;
    2) otherwise download the official python.org per-user installer and run it silently
       (includes tkinter + pip + the py launcher).
  Both land in %LOCALAPPDATA%\Programs\Python\Python3xx\, which setup.cmd re-detects without
  needing the PATH to refresh in the current window.

  Exit 0 if Python looks installed (or already was), 1 otherwise.
  -DryRun: print what it WOULD do and verify the download URL is reachable, without installing.

  ASCII-only on purpose (no BOM needed; avoids PS 5.1 cp1252 mangling).
#>
param([switch]$DryRun)

$ErrorActionPreference = 'Continue'
$PinnedVersion = '3.12.10'   # bump freely; any 3.10-3.14 works for the overlay's deps

function Test-PyCmd($file, $pre) {
  # True only if this really runs Python 3 (the Windows Store alias prints a notice and exits != 0,
  # so it can't false-positive here).
  try {
    $out = & $file @pre '--version' 2>$null
    return ($LASTEXITCODE -eq 0 -and "$out" -match 'Python 3')
  } catch { return $false }
}

function Find-Python {
  if (Test-PyCmd 'py'     @('-3')) { return 'py -3' }
  if (Test-PyCmd 'python' @())     { return 'python' }
  $cand = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
          Sort-Object FullName -Descending | Select-Object -First 1
  if ($cand) { return $cand.FullName }
  return $null
}

$existing = Find-Python

$arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' }
        elseif ([Environment]::Is64BitOperatingSystem) { 'amd64' } else { '' }
$exeName = if ($arch) { "python-$PinnedVersion-$arch.exe" } else { "python-$PinnedVersion.exe" }
$url = "https://www.python.org/ftp/python/$PinnedVersion/$exeName"

if ($DryRun) {
  Write-Host "[DRY] python already present = $existing"
  Write-Host "[DRY] arch                   = $arch"
  Write-Host "[DRY] winget available       = $([bool](Get-Command winget -ErrorAction SilentlyContinue))"
  Write-Host "[DRY] installer URL          = $url"
  try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $r = Invoke-WebRequest -Uri $url -Method Head -UseBasicParsing -TimeoutSec 25
    Write-Host "[DRY] URL reachable          = HTTP $($r.StatusCode)"
  } catch { Write-Host "[DRY] URL check failed       = $($_.Exception.Message)" }
  exit 0
}

if ($existing) { Write-Host "[OK] Python already present: $existing"; exit 0 }

# 1) winget, user scope (no admin)
if (Get-Command winget -ErrorAction SilentlyContinue) {
  Write-Host "Installing Python via winget (user scope, no admin)..."
  try {
    & winget install -e --id Python.Python.3.12 --source winget --scope user `
        --accept-package-agreements --accept-source-agreements --disable-interactivity
  } catch { Write-Host "winget attempt errored: $($_.Exception.Message)" }
  if (Find-Python) { Write-Host "[OK] Python installed via winget."; exit 0 }
  Write-Host "winget did not yield a usable Python; falling back to the python.org installer..."
}

# 2) official python.org installer, silent per-user
try {
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  $dst = Join-Path $env:TEMP $exeName
  Write-Host "Downloading $url ..."
  Invoke-WebRequest -Uri $url -OutFile $dst -UseBasicParsing
  Write-Host "Running the installer (per-user, no admin). This can take a minute..."
  $p = Start-Process -FilePath $dst -Wait -PassThru -ArgumentList @(
        '/quiet','InstallAllUsers=0','PrependPath=1','Include_pip=1','Include_tcltk=1','Include_launcher=1')
  Write-Host "Installer exit code: $($p.ExitCode)"
  Remove-Item $dst -ErrorAction SilentlyContinue
} catch {
  Write-Host "[X] Download/install failed: $($_.Exception.Message)"
}

if (Find-Python) { Write-Host "[OK] Python is now installed."; exit 0 }
Write-Host "[X] Python install did not complete."
exit 1
