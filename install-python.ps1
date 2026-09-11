<#
  install-python.ps1 - best-effort, NON-ADMIN Python 3 install for Claude Overlay's setup.cmd.

  Strategy, in order, stopping at the first one that yields a WORKING interpreter:
    1) winget (user scope, no admin), if winget is available;
    2) the official python.org per-user installer, downloaded and run silently;
    3) uv (Astral) - see "WHY 3 EXISTS" below.
  All three land under %LOCALAPPDATA%\Programs\Python\, which setup.cmd and the three shipped
  .cmd scripts locate by SCANNING that folder for the filename. So none of them has to know
  which strategy won, or how it named its directory.

  WHY 3 EXISTS - reported from a managed corporate Windows PC where 1) and 2) BOTH fail:
    * BeyondTrust/Avecto Privilege Management blocks any Python-Software-Foundation-signed
      python.exe. The block is by SIGNATURE, not filename, so renaming does not help - and it
      covers the python.org installer, the Microsoft Store build and the official embeddable
      zip alike, i.e. every artifact strategies 1) and 2) can possibly produce.
    * Zscaler answers 403 (sandboxed) for .exe downloads from python.org, so 2) cannot even
      fetch its installer. .zip and GitHub-release downloads pass.
    uv sidesteps both: its own exe arrives inside a GitHub-release .zip, and the CPython it
    installs is Astral's python-build-standalone, which is not PSF-signed and therefore runs.

  ASSUME THE 403 HAPPENS TO OTHER PEOPLE TOO
    Do not read strategy 3 as "the fix". This kind of policy is per-user/per-group, and measured
    on two machines inside the SAME company it already differs: on one, python.org's .exe is
    refused with 403; on the other the same URL answers 200. So the honest assumption is that ANY
    of these downloads can be refused on someone's machine - including uv's own, whose URL
    redirects to release-assets.githubusercontent.com, a DIFFERENT host from github.com that a
    proxy can categorise separately. "GitHub is allowed" does not imply this download is.

    Because of that, the two routes that need NO download are first-class here, not footnotes:
      * offline\uv-*.zip   - anyone whose machine CAN reach GitHub downloads that zip once and
                             drops it in the offline\ folder next to this script. Strategy 3
                             prefers it over the network every time, so a share drive or a USB
                             stick is a complete answer for a fleet.
      * UV_PYTHON_INSTALL_MIRROR - honoured, never overwritten. Point it at an internal mirror
                             of python-build-standalone and uv fetches CPython from there.
      * and if all else fails, no script is needed at all: drop any Python 3.10+ (with tkinter)
                             into %LOCALAPPDATA%\Programs\Python\. Every one of the overlay's
                             scripts scans that folder and uses whatever runs there - it does
                             not care how it arrived or who signed it, so a zipped copy of a
                             colleague's working install is enough.
    The give-up path prints what each route actually reported, because "403 on the .zip" is
    what makes an IT ticket actionable and "install failed" is not.

  Exit 0 if Python looks installed (or already was), 1 otherwise.
  -DryRun: print what it WOULD do and check the download URLs are reachable, without installing.

  ASCII-only on purpose (no BOM needed; avoids PS 5.1 cp1252 mangling).
#>
param([switch]$DryRun)

$ErrorActionPreference = 'Continue'
$PinnedVersion = '3.12.10'   # bump freely; any 3.10-3.14 works for the overlay's deps
$UvPyVersion   = '3.12'      # what strategy 3 asks uv for

# Where every strategy is expected to leave an interpreter, and the one folder the launcher,
# Diagnose.cmd, update.cmd and setup.cmd all scan. Keep these in step or a successful install
# becomes invisible to the app - which is exactly the v1.15.3 defect.
$PyRoot = Join-Path $env:LOCALAPPDATA 'Programs\Python'
$UvHome = Join-Path $env:LOCALAPPDATA 'Programs\uv'

# Pre-staged artifacts, checked BEFORE any download. This is the answer for a machine whose
# proxy refuses the download outright: someone else fetches uv-*.zip once and drops it here.
# $PSScriptRoot, not the CWD - setup.cmd may be invoked from anywhere.
$OfflineDir = Join-Path $PSScriptRoot 'offline'

# One line per strategy, printed together if they all fail. A user who is blocked needs to
# hand IT a specific refusal ("403 on the .zip"), not "install failed".
$Report = [ordered]@{ winget = 'not attempted'; 'python.org' = 'not attempted'; uv = 'not attempted' }

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
  if (-not (Test-Path $PyRoot)) { return $null }
  # Recurse by FILENAME rather than matching a Python3* directory, because strategy 3 installs
  # under uv's own naming (cpython-3.12.13-windows-x86_64-none) which no Python3* glob matches.
  # This mirrors `dir /b /s ... python.exe` in the .cmd scripts, so both sides consider the
  # same candidates. (One deliberate difference: the .cmd scans do not exclude Lib\venv --
  # they probe every hit, so a template launcher costs them one slow FAILED probe rather
  # than a wrong answer, while this script skips it outright.)
  #
  # Lib\venv\scripts\nt is skipped deliberately: every CPython ships venv TEMPLATE launchers
  # there, they are not usable interpreters, and one was measured taking 17 seconds to answer
  # a --version probe. Nothing should wait on that to decide an install succeeded.
  $cand = Get-ChildItem $PyRoot -Recurse -Filter 'python.exe' -File -ErrorAction SilentlyContinue |
          Where-Object { $_.FullName -notmatch '\\Lib\\venv\\' } |
          Sort-Object FullName -Descending
  foreach ($c in $cand) { if (Test-PyCmd $c.FullName @()) { return $c.FullName } }
  return $null
}

function Invoke-Download($url, $dst) {
  # PREFER curl.exe, but never REQUIRE it. Two different environments to satisfy:
  #
  #   * On a managed machine, curl.exe is the better client: it uses schannel and therefore
  #     the WINDOWS certificate store, so a TLS-intercepting proxy whose root CA is installed
  #     by policy just works -- while PS 5.1's Invoke-WebRequest fails on that same box with
  #     "Could not create SSL/TLS secure channel". curl also reports the HTTP status, which is
  #     the datum that separates "the proxy refused this file type" from "no network".
  #   * But curl.exe only ships with Windows 10 1803 and later. Earlier Windows 10 -- which
  #     this project still claims to support -- has none, and Invoke-WebRequest is what used
  #     to do this job successfully. Replacing it outright would have broken those machines to
  #     fix a different one, so the old path stays as the floor.
  #
  # No `-f` / `--fail-with-body`: --fail-with-body only exists in curl 7.76+, while Windows 10
  # 1803 shipped curl 7.55, where an unknown option makes curl exit non-zero and every download
  # look "refused". The status code is read directly instead, which needs no modern flag.
  Remove-Item $dst -Force -ErrorAction SilentlyContinue
  if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
    $code = & curl.exe -sS -L -o "$dst" -w '%{http_code}' "$url"
    $curlRc = $LASTEXITCODE
    # Without -f, curl writes the error PAGE to the file and still exits 0, so file existence
    # proves nothing on its own -- the status has to decide, and a rejected body must go.
    if ($curlRc -eq 0 -and "$code" -match '^2\d\d$' -and (Test-Path $dst)) {
      return @{ ok = $true; code = "$code" }
    }
    Remove-Item $dst -Force -ErrorAction SilentlyContinue
    # The code string must stand on its own in the per-route report. Two distinctions to
    # keep: a non-zero curl exit WITH a 2xx status is a transfer that broke mid-body, not a
    # refusal -- reporting it as "HTTP 200" would send the user chasing a proxy that
    # answered fine; and a code that is not an HTTP status at all must not be printed
    # after the word "HTTP".
    if ($curlRc -ne 0) {
      if ("$code" -match '^\d{3}$' -and "$code" -ne '000') { return @{ ok = $false; code = "curl exit $curlRc after HTTP $code" } }
      return @{ ok = $false; code = "curl exit $curlRc, no HTTP status (could not connect?)" }
    }
    if ("$code" -match '^\d{3}$' -and "$code" -ne '000') { return @{ ok = $false; code = "HTTP $code" } }
    return @{ ok = $false; code = 'curl exit 0, but no HTTP status' }
  }
  try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $dst -UseBasicParsing
    if (Test-Path $dst) { return @{ ok = $true; code = 'via Invoke-WebRequest' } }
    return @{ ok = $false; code = 'Invoke-WebRequest reported success but wrote no file' }
  } catch {
    Remove-Item $dst -Force -ErrorAction SilentlyContinue
    # $_.Exception.Response is present for an HTTP error and absent for a TLS/DNS failure, so
    # this still distinguishes "refused" from "could not connect" without curl.
    $st = $null
    try { $st = [int]$_.Exception.Response.StatusCode } catch { }
    if ($st) { return @{ ok = $false; code = "HTTP $st (via Invoke-WebRequest)" } }
    return @{ ok = $false; code = "no HTTP status - $($_.Exception.Message)" }
  }
}

function Test-UvCmd($exe) {
  # True only if this uv actually RUNS. A truncated uv.exe left by an interrupted extract,
  # or a stale/blocked one on PATH, would otherwise be trusted outright -- short-circuiting
  # the pre-staged zip AND the download below, and losing the whole strategy to a binary
  # nothing ever verified. Same rule as Test-PyCmd: measure, don't assume.
  try { & $exe --version *> $null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}

function Get-UvExe {
  $onPath = Get-Command uv -ErrorAction SilentlyContinue
  if ($onPath -and (Test-UvCmd $onPath.Source)) { return $onPath.Source }
  $local = Join-Path $UvHome 'uv.exe'
  if ((Test-Path $local) -and (Test-UvCmd $local)) { return $local }
  return $null
}

function Expand-UvZip($zip) {
  if (-not (Test-Path $UvHome)) { New-Item -ItemType Directory -Path $UvHome -Force | Out-Null }
  try {
    Expand-Archive -LiteralPath $zip -DestinationPath $UvHome -Force
  } catch {
    # Expand-Archive comes from a module that can be absent or blocked in a locked-down or
    # trimmed PowerShell. ZipFile is part of .NET itself on every Windows this app supports,
    # so it is the floor -- and losing the unzip step would waste a download that succeeded.
    # Entry by entry, because the one-call overwrite overload
    # ExtractToDirectory(zip, dir, $true) exists only on .NET Core: Windows PowerShell 5.1
    # runs on .NET Framework, whose ZipFile has just (String,String) and
    # (String,String,Encoding) -- measured, the 3-argument call throws 'Cannot find an
    # overload' on exactly the trimmed machines this fallback exists for.
    # ExtractToFile(entry, path, overwrite) exists on both runtimes.
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    $archive = [System.IO.Compression.ZipFile]::OpenRead($zip)
    try {
      foreach ($entry in $archive.Entries) {
        if ($entry.FullName -match '\.\.') { continue }   # no path escape from $UvHome
        $target = Join-Path $UvHome $entry.FullName
        if ($entry.FullName -match '[/\\]$') {
          if (-not (Test-Path $target)) { New-Item -ItemType Directory -Path $target -Force | Out-Null }
          continue
        }
        $parent = Split-Path $target -Parent
        if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
      }
    } finally { $archive.Dispose() }
  }
  return (Get-UvExe)
}

function Get-OfflineUvZip {
  # Any uv*.zip in offline\ will do; newest first, so replacing the file is how you upgrade.
  if (-not (Test-Path $OfflineDir)) { return $null }
  $z = Get-ChildItem $OfflineDir -Filter 'uv*.zip' -File -ErrorAction SilentlyContinue |
       Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if ($z) { return $z.FullName }
  return $null
}

$existing = Find-Python

$arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' }
        elseif ([Environment]::Is64BitOperatingSystem) { 'amd64' } else { '' }
$exeName = if ($arch) { "python-$PinnedVersion-$arch.exe" } else { "python-$PinnedVersion.exe" }
$url = "https://www.python.org/ftp/python/$PinnedVersion/$exeName"

# All three Windows targets, not just the common one. $arch is '' on 32-bit x86, where handing
# out the x86_64 build would download fine and then refuse to run -- a failure that looks like
# a broken archive rather than a wrong architecture.
$uvZip = if ($arch -eq 'arm64') { 'uv-aarch64-pc-windows-msvc.zip' }
         elseif ($arch -eq 'amd64') { 'uv-x86_64-pc-windows-msvc.zip' }
         else { 'uv-i686-pc-windows-msvc.zip' }
$uvUrl = "https://github.com/astral-sh/uv/releases/latest/download/$uvZip"

if ($DryRun) {
  Write-Host "[DRY] python already present = $existing"
  Write-Host "[DRY] arch                   = $arch"
  Write-Host "[DRY] winget available       = $([bool](Get-Command winget -ErrorAction SilentlyContinue))"
  Write-Host "[DRY] python install root    = $PyRoot"
  Write-Host "[DRY] installer URL          = $url"
  Write-Host "[DRY] uv already present     = $(Get-UvExe)"
  Write-Host "[DRY] uv URL                 = $uvUrl"
  Write-Host "[DRY] offline uv archive     = $(Get-OfflineUvZip)"
  Write-Host "[DRY] CPython mirror         = $env:UV_PYTHON_INSTALL_MIRROR"
  foreach ($u in @($url, $uvUrl)) {
    # Same floor as Invoke-Download: prefer curl.exe, but pre-1803 Windows 10 has none,
    # and a dry run that errors there would misreport the very machine it is diagnosing.
    if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
      $probe = & curl.exe -sIL -o NUL -w '%{http_code}' --max-time 25 "$u"
      Write-Host "[DRY] HTTP $probe  <- $u"
    } else {
      try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $r = Invoke-WebRequest -Uri $u -Method Head -UseBasicParsing -TimeoutSec 25
        Write-Host "[DRY] HTTP $($r.StatusCode)  <- $u"
      } catch { Write-Host "[DRY] check failed ($($_.Exception.Message))  <- $u" }
    }
  }
  exit 0
}

if ($existing) { Write-Host "[OK] Python already present: $existing"; exit 0 }

# --- 1) winget, user scope (no admin) ------------------------------------------------------
if (Get-Command winget -ErrorAction SilentlyContinue) {
  Write-Host "Installing Python via winget (user scope, no admin)..."
  try {
    & winget install -e --id Python.Python.3.12 --source winget --scope user `
        --accept-package-agreements --accept-source-agreements --disable-interactivity
    $Report['winget'] = "ran, exit $LASTEXITCODE, no usable interpreter after it"
  } catch {
    Write-Host "winget attempt errored: $($_.Exception.Message)"
    $Report['winget'] = "errored: $($_.Exception.Message)"
  }
  if (Find-Python) { Write-Host "[OK] Python installed via winget."; exit 0 }
  Write-Host "winget did not yield a usable Python; falling back to the python.org installer..."
} else {
  $Report['winget'] = 'not available on this PC'
}

# --- 2) official python.org installer, silent per-user -------------------------------------
try {
  $dst = Join-Path $env:TEMP $exeName
  Write-Host "Downloading $url ..."
  $dl = Invoke-Download $url $dst
  if (-not $dl.ok) {
    # A managed proxy commonly answers 403 for .exe specifically. Saying so is what tells the
    # next reader that the network is fine and the FILE TYPE was refused. The code string
    # already names itself ("HTTP 403" vs "curl exit 6, no HTTP status"), so it is printed
    # as-is -- prefixing it with "HTTP" here turned connect failures into "HTTP curl exit 6".
    $Report['python.org'] = "download failed - $($dl.code)"
    Write-Host "[X] Download failed ($($dl.code)). Falling back to uv..."
  } else {
    Write-Host "Running the installer (per-user, no admin). This can take a minute..."
    $p = Start-Process -FilePath $dst -Wait -PassThru -ArgumentList @(
          '/quiet','InstallAllUsers=0','PrependPath=1','Include_pip=1','Include_tcltk=1','Include_launcher=1')
    Write-Host "Installer exit code: $($p.ExitCode)"
    # Avecto blocks this binary by signature, and a blocked launch still returns an exit code.
    $Report['python.org'] = "installer ran, exit $($p.ExitCode), no usable interpreter after it"
    Remove-Item $dst -ErrorAction SilentlyContinue
  }
} catch {
  Write-Host "[X] Download/install failed: $($_.Exception.Message)"
  $Report['python.org'] = "failed: $($_.Exception.Message)"
}

if (Find-Python) { Write-Host "[OK] Python is now installed."; exit 0 }

# --- 3) uv (Astral) ------------------------------------------------------------------------
# The route that survives a device policy which blocks PSF-signed Python and 403s .exe
# downloads. Reported working on exactly such a machine; see the header.
try {
  $uv = Get-UvExe
  # Pre-staged zip BEFORE the network, so a machine whose proxy refuses the download is not
  # stuck behind one. Trying it first (rather than as a fallback) also means the offline path
  # is the one that gets exercised routinely instead of only on the day it is needed.
  if (-not $uv) {
    $offlineZip = Get-OfflineUvZip
    if ($offlineZip) {
      Write-Host "Using the pre-staged uv archive: $offlineZip"
      # A corrupt staged zip must DEGRADE to the download below, not abort the strategy:
      # without this catch an extract error would jump to the outer catch and skip the
      # network attempt that was designed to follow it.
      try { $uv = Expand-UvZip $offlineZip } catch { $uv = $null }
      if (-not $uv) { $Report['uv'] = "offline\$(Split-Path $offlineZip -Leaf) held no working uv.exe" }
    }
  }
  if (-not $uv) {
    Write-Host "Downloading uv ($uvZip) ..."
    $zip = Join-Path $env:TEMP $uvZip
    $dl = Invoke-Download $uvUrl $zip
    # Append rather than assign below: the staged-zip note (if any) has to survive into the
    # give-up report, or the user re-runs against the same bad offline file with no clue.
    if (-not $dl.ok) {
      $note = "download failed - $($dl.code) (redirects to release-assets.githubusercontent.com, which a proxy can block separately from github.com)"
      if ($Report['uv'] -ne 'not attempted') { $note = "$($Report['uv']); then $note" }
      $Report['uv'] = $note
      throw "could not download uv ($($dl.code))"
    }
    $uv = Expand-UvZip $zip
    Remove-Item $zip -ErrorAction SilentlyContinue
    if (-not $uv) {
      $note = 'downloaded, but no working uv.exe inside the archive'
      if ($Report['uv'] -ne 'not attempted') { $note = "$($Report['uv']); then $note" }
      $Report['uv'] = $note
      throw 'uv.exe not found after extract'
    }
  }
  Write-Host "Installing Python $UvPyVersion with uv ($uv) ..."

  # UV_SYSTEM_CERTS: uv bundles rustls, which does NOT read the Windows certificate store by
  # default, so behind a TLS-intercepting proxy it fails with
  # "invalid peer certificate: UnknownIssuer". This makes it trust the store instead.
  $env:UV_SYSTEM_CERTS = '1'
  # Install straight into the folder every script already scans, instead of installing
  # elsewhere and copying. A copy step would have to know uv's directory naming, would
  # duplicate ~100 MB, and is one more thing that can half-succeed.
  $env:UV_PYTHON_INSTALL_DIR = $PyRoot
  # Deliberately NOT set here: UV_PYTHON_INSTALL_MIRROR. uv reads it itself, and it is the
  # documented way to fetch CPython from an internal mirror when the public host is refused.
  # Overwriting it would silently undo the one setting an IT department can hand a whole fleet.
  if ($env:UV_PYTHON_INSTALL_MIRROR) {
    Write-Host "Honouring UV_PYTHON_INSTALL_MIRROR = $env:UV_PYTHON_INSTALL_MIRROR"
  }

  # --no-bin keeps uv from dropping a python3.12.exe shim into %USERPROFILE%\.local\bin, which
  # is outside everything this installer owns and which nothing here needs -- the overlay finds
  # interpreters by scanning, not by PATH. Observed on a machine that already had that file: uv
  # printed a "Failed to install executable" warning that reads like the install broke when it
  # had not.
  # The support is PROBED rather than assumed, because whoever already has uv on PATH may have
  # any version, and an unknown flag is a hard error that would lose the whole strategy. An env
  # var would be safer still (unknown ones are ignored) but uv documents no equivalent.
  $uvArgs = @('python', 'install', $UvPyVersion)
  $uvHelp = & $uv python install --help 2>$null
  if ("$uvHelp" -match '--no-bin') { $uvArgs += '--no-bin' }
  & $uv @uvArgs
  $uvExit = $LASTEXITCODE
  if ($uvExit -ne 0) { $Report['uv'] = "uv python install exited $uvExit"; throw "uv python install exited $uvExit" }

  # python-build-standalone SHIPS Lib\EXTERNALLY-MANAGED on Windows (measured - it is not a
  # Linux-only thing), and while it is there pip refuses to install into this interpreter:
  # setup.cmd's `pip install -r requirements.txt` would fail with
  # "externally-managed-environment" right after a successful install. uv puts it there to
  # steer people to `uv pip`; the overlay installs with pip, so it goes.
  Get-ChildItem $PyRoot -Recurse -Filter 'EXTERNALLY-MANAGED' -File -ErrorAction SilentlyContinue |
    ForEach-Object {
      Write-Host "Removing $($_.FullName) so pip can install into this interpreter."
      Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
    }

  if (Find-Python) { Write-Host "[OK] Python installed via uv."; exit 0 }
  $Report['uv'] = 'uv reported success but no usable interpreter was found afterwards'
} catch {
  Write-Host "[X] uv route failed: $($_.Exception.Message)"
  if ($Report['uv'] -eq 'not attempted') { $Report['uv'] = "failed: $($_.Exception.Message)" }
}

# --- all three failed ----------------------------------------------------------------------
Write-Host ""
Write-Host "[X] Python install did not complete. What each route reported:"
foreach ($k in $Report.Keys) { Write-Host ("      {0,-11}: {1}" -f $k, $Report[$k]) }
Write-Host ""
Write-Host "    A refused download is NOT unusual on a managed PC, and it is not something this"
Write-Host "    script can talk its way past. Two routes below need no working download at all;"
Write-Host "    either one finishes the job."
Write-Host ""
Write-Host "    A) Pre-stage uv (best for more than one machine)"
Write-Host "       On any PC that CAN reach GitHub, download:"
Write-Host "         $uvUrl"
Write-Host "       and put that .zip in:"
Write-Host "         $OfflineDir\"
Write-Host "       Then re-run setup.cmd. It uses the file instead of the network."
Write-Host "       (An internal mirror works too: set UV_PYTHON_INSTALL_MIRROR and re-run.)"
Write-Host ""
Write-Host "    B) Bring your own Python (works with no network whatsoever)"
Write-Host "       Put any Python 3.10+ that includes tkinter into:"
Write-Host "         $PyRoot\"
Write-Host "       Every one of the overlay's scripts scans that folder and uses whatever runs"
Write-Host "       there - it does not care how it arrived or who signed it, so a zipped copy of"
Write-Host "       a colleague's working install is enough."
Write-Host ""
Write-Host "    Send the per-route lines above to IT if you would rather have the block lifted:"
Write-Host "    they name which artifact was refused and with which HTTP status, which is what"
Write-Host "    makes a ticket actionable where 'the installer failed' does not."
exit 1
