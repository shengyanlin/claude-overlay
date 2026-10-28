<#
  install-node.ps1 - NON-ADMIN Node.js install for install-codex.cmd. Codex mode needs the
  codex CLI, the codex CLI comes from npm, and npm comes with Node.js.

  WHY THIS EXISTS - reported from an install updated 1.25.0 -> 1.25.1 on a managed PC with no
  Node.js: the update asked to set Codex up, "seemed to install", and the window closed with
  Codex still unusable. The only Node.js route then was winget, and winget's Node.js is the
  machine-wide MSI: it needs an admin to approve its UAC prompt, which a standard user on a
  managed PC cannot give. The official .zip needs no installer and no admin, so it goes first
  and winget is the fallback (install-codex.cmd decides the order; this script only does the
  zip).

  What it does, stopping at the first failure with the reason on screen:
    1) the newest few LTS releases that have a Windows .zip for this CPU, from
       nodejs.org/dist/index.json, newest first;
    2) download a release's SHASUMS256.txt and .zip, and refuse the .zip unless its SHA-256
       matches -- a release that is incomplete or doesn't match is skipped for the next;
    3) unpack it into a staging folder beside %LOCALAPPDATA%\Programs\nodejs (the same
       per-user Programs folder the overlay's Python goes into);
    4) give npm the global prefix the MSI gives it, ${APPDATA}\npm, so `npm install -g` puts
       the codex CLI where the overlay, update.cmd and Diagnose already look for it;
    5) prove it in staging: node.exe and npm must both RUN, or nothing is moved anywhere;
    6) move it to %LOCALAPPDATA%\Programs\nodejs;
    7) add both folders to the USER Path (no admin), as the MSI does for the machine Path, so
       new terminals and the overlay's one-click CLI update find `npm`.

  Exit 0 if node + npm work from %LOCALAPPDATA%\Programs\nodejs (or already did), 1 otherwise.
  -DryRun: print what it WOULD do and check the URLs are reachable, without installing.
  -NoUserPath: leave the user Path alone (the test suite, and anyone who manages Path).

  ASCII-only on purpose (no BOM needed; avoids PS 5.1 cp1252 mangling).
#>
param([switch]$DryRun, [switch]$NoUserPath)

$ErrorActionPreference = 'Continue'
$Dist     = 'https://nodejs.org/dist'
$NodeRoot = Join-Path $env:LOCALAPPDATA 'Programs\nodejs'
$NpmBin   = Join-Path $env:APPDATA 'npm'

function Invoke-Download($url, $dst) {
  # Same client choice as install-python.ps1's Invoke-Download, for the same reasons: curl.exe
  # uses the Windows certificate store (a TLS-inspecting proxy's root CA just works) and
  # reports the HTTP status; Invoke-WebRequest is the floor for Windows without curl.exe.
  Remove-Item $dst -Force -ErrorAction SilentlyContinue
  if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
    $code = & curl.exe -sS -L -o "$dst" -w '%{http_code}' "$url"
    $curlRc = $LASTEXITCODE
    if ($curlRc -eq 0 -and "$code" -match '^2\d\d$' -and (Test-Path $dst)) {
      return @{ ok = $true; code = "$code" }
    }
    Remove-Item $dst -Force -ErrorAction SilentlyContinue
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
    $st = $null
    try { $st = [int]$_.Exception.Response.StatusCode } catch { }
    if ($st) { return @{ ok = $false; code = "HTTP $st (via Invoke-WebRequest)" } }
    return @{ ok = $false; code = "no HTTP status - $($_.Exception.Message)" }
  }
}

function Test-Node($root) {
  # Both have to RUN: node.exe alone could be a half-unpacked tree, and npm.cmd is what
  # install-codex.cmd is about to call.
  $node = Join-Path $root 'node.exe'
  $npm  = Join-Path $root 'npm.cmd'
  if (-not ((Test-Path $node) -and (Test-Path $npm))) { return $false }
  try {
    & $node --version *> $null
    if ($LASTEXITCODE -ne 0) { return $false }
    & $npm --version *> $null
    return ($LASTEXITCODE -eq 0)
  } catch { return $false }
}

function Fail($msg) {
  Write-Host "[X] $msg"
  Write-Host "    Node.js was not installed. It can also be installed by hand from"
  Write-Host "      https://nodejs.org   (the LTS .msi needs admin; the .zip does not - unpack"
  Write-Host "      it into $NodeRoot)"
  exit 1
}

function Add-UserPath($dirs) {
  # Through the registry rather than [Environment]::SetEnvironmentVariable('Path', ..,'User'):
  # that one reads the Path EXPANDED and writes it back as plain REG_SZ, which freezes every
  # %VAR% entry the user already had. Read raw, keep the value kind, append what's missing.
  $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
  if (-not $key) { return }
  try {
    $raw = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    $have = @($raw -split ';' | Where-Object { $_ } |
              ForEach-Object { [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\').ToLowerInvariant() })
    $add = @($dirs | Where-Object { $have -notcontains $_.TrimEnd('\').ToLowerInvariant() })
    if (-not $add) { return }
    $new = (@($raw.TrimEnd(';')) + $add | Where-Object { $_ }) -join ';'
    $key.SetValue('Path', $new, [Microsoft.Win32.RegistryValueKind]::ExpandString)
  } finally { $key.Close() }
  # Tell Explorer the environment changed, so a program started from it after this sees the
  # new Path. SetEnvironmentVariable(.., 'User') broadcasts WM_SETTINGCHANGE itself; setting
  # and clearing a throwaway variable is the way to get that broadcast without Add-Type
  # (which compiles C#, and is blocked on some locked-down machines).
  [Environment]::SetEnvironmentVariable('CLAUDE_OVERLAY_PATH_PING', '1', 'User')
  [Environment]::SetEnvironmentVariable('CLAUDE_OVERLAY_PATH_PING', $null, 'User')
  Write-Host "[OK] Added to your user Path: $($add -join ', ')"
}

if (Test-Node $NodeRoot) {
  Write-Host "[OK] Node.js already present: $NodeRoot"
  if (-not $NoUserPath -and -not $DryRun) { Add-UserPath @($NodeRoot, $NpmBin) }
  exit 0
}

# PROCESSOR_ARCHITEW6432 is the real CPU when this runs in a 32-bit PowerShell on 64-bit Windows.
$cpu = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
$arch = if ($cpu -eq 'ARM64') { 'arm64' } elseif ([Environment]::Is64BitOperatingSystem) { 'x64' } else { 'x86' }

$tmp = Join-Path $env:TEMP "claude-overlay-node-$PID"
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
try {
  # --- 1) which release ---------------------------------------------------------------------
  Write-Host "Looking up the current Node.js LTS ..."
  $idx = Join-Path $tmp 'index.json'
  $dl = Invoke-Download "$Dist/index.json" $idx
  if (-not $dl.ok) { Fail "Could not read $Dist/index.json ($($dl.code))." }
  try { $releases = Get-Content $idx -Raw | ConvertFrom-Json } catch { Fail "nodejs.org sent a release list that isn't JSON." }
  # index.json is newest first; `lts` is false or the LTS line's name. The newest FEW, not just
  # the newest: a release is listed there while its files are still being published --
  # measured the day after 26.x became LTS, its .zip answered 200 and its SHASUMS256.txt 404,
  # and a newest-only lookup failed the whole install over it. An incomplete release is
  # skipped for the one before it.
  $cands = @($releases | Where-Object { $_.lts -and ($_.files -contains "win-$arch-zip") } |
             Select-Object -First 3)
  if (-not $cands) { Fail "nodejs.org lists no LTS .zip for Windows $arch." }

  if ($DryRun) {
    Write-Host "[DRY] arch         = $arch"
    Write-Host "[DRY] install root = $NodeRoot"
    Write-Host "[DRY] npm prefix   = $NpmBin"
    foreach ($rel in $cands) {
      Write-Host "[DRY] candidate    = $($rel.version) ($($rel.lts))"
      if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
        foreach ($u in @("$Dist/$($rel.version)/node-$($rel.version)-win-$arch.zip",
                         "$Dist/$($rel.version)/SHASUMS256.txt")) {
          $probe = & curl.exe -sIL -o NUL -w '%{http_code}' --max-time 25 "$u"
          Write-Host "[DRY]   HTTP $probe  <- $u"
        }
      }
    }
    exit 0
  }

  # --- 2) download + verify -----------------------------------------------------------------
  # Only a .zip whose SHA-256 matches the release's published list is ever unpacked. A
  # mismatch moves on to the next release like a missing file does -- what's installed is
  # always a verified one.
  $ver = $null
  $tried = @()
  foreach ($rel in $cands) {
    $v = $rel.version
    $n = "node-$v-win-$arch"
    # The checksum list first: it is tiny, and without it the .zip can't be trusted anyway.
    $sums = Join-Path $tmp "SHASUMS256-$v.txt"
    $dl = Invoke-Download "$Dist/$v/SHASUMS256.txt" $sums
    if (-not $dl.ok) { $tried += "${v}: SHASUMS256.txt - $($dl.code)"; continue }
    $want = $null
    foreach ($line in (Get-Content $sums)) {
      $parts = $line -split '\s+'
      if ($parts.Count -ge 2 -and $parts[1] -eq "$n.zip") { $want = $parts[0].ToLowerInvariant() }
    }
    if (-not $want) { $tried += "${v}: SHASUMS256.txt lists no $n.zip"; continue }
    Write-Host "Downloading Node.js $v ($n.zip, about 35 MB) ..."
    $z = Join-Path $tmp "$n.zip"
    $dl = Invoke-Download "$Dist/$v/$n.zip" $z
    if (-not $dl.ok) { $tried += "${v}: $n.zip - $($dl.code)"; continue }
    $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $z).Hash.ToLowerInvariant()
    if ($got -ne $want) {
      Remove-Item $z -Force -ErrorAction SilentlyContinue
      $tried += "${v}: $n.zip does not match its published SHA-256"
      continue
    }
    Write-Host "[OK] Checksum matches."
    $ver, $name, $zip = $v, $n, $z
    break
  }
  if (-not $ver) {
    foreach ($t in $tried) { Write-Host "    $t" }
    Fail "No Node.js LTS could be downloaded and verified (what each one reported is above)."
  }

  # --- 3) unpack ----------------------------------------------------------------------------
  # Into a staging folder next to the target, then one rename: a half-unpacked tree must never
  # sit at $NodeRoot, where the next run would find a node.exe and trust it.
  $programs = Split-Path $NodeRoot -Parent
  if (-not (Test-Path $programs)) { New-Item -ItemType Directory -Path $programs -Force | Out-Null }
  $stage = Join-Path $programs "nodejs.partial-$PID"
  Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Path $stage -Force | Out-Null
  Write-Host "Unpacking into $NodeRoot ..."
  # tar.exe (Windows 10 1803+) unpacks this ~6000-file zip in seconds; Expand-Archive on PS 5.1
  # takes minutes. ZipFile's 2-argument overload exists on .NET Framework too, and the
  # staging folder is empty, so no overwrite overload is needed.
  $unpacked = $false
  if (Get-Command tar.exe -ErrorAction SilentlyContinue) {
    & tar.exe -xf "$zip" -C "$stage" 2>$null
    $unpacked = ($LASTEXITCODE -eq 0) -and (Test-Path (Join-Path $stage "$name\node.exe"))
  }
  if (-not $unpacked) {
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    try {
      Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
      [System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $stage)
      $unpacked = Test-Path (Join-Path $stage "$name\node.exe")
    } catch { $unpacked = $false }
  }
  if (-not $unpacked) { Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue; Fail "Could not unpack $name.zip." }
  $tree = Join-Path $stage $name

  # --- 4) npm's global prefix, as the MSI sets it -------------------------------------------
  # Without this, npm from a .zip installs globals next to node.exe, and the codex CLI would
  # land in a folder the overlay's own lookup (%APPDATA%\npm, then PATH) never searches from a
  # session started before this install.
  $npmrc = Join-Path $tree 'node_modules\npm\npmrc'
  if (-not (Test-Path $npmrc) -or -not (Select-String -LiteralPath $npmrc -Pattern '^\s*prefix\s*=' -Quiet)) {
    Add-Content -LiteralPath $npmrc -Value 'prefix=${APPDATA}\npm' -Encoding ascii
  }

  # --- 5) prove it, while it is still in staging -------------------------------------------
  # Before it goes anywhere anything looks: a tree whose node.exe a device policy blocks would
  # otherwise sit at $NodeRoot with an npm.cmd that `where npm` finds -- install-codex.cmd
  # would skip its winget fallback for it, and every later run would trust it too.
  if (-not (Test-Node $tree)) {
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
    Fail "Unpacked, but node/npm from it don't run (a device policy blocking node.exe?)."
  }

  # --- 6) move it in -----------------------------------------------------------------------
  if (Test-Path $NodeRoot) {
    # Something is there but didn't pass Test-Node. Moved aside, not deleted: it isn't
    # necessarily ours.
    $aside = "$NodeRoot.old-$(Get-Date -Format yyyyMMddHHmmss)"
    Rename-Item -LiteralPath $NodeRoot -NewName (Split-Path $aside -Leaf) -ErrorAction SilentlyContinue
    if (Test-Path $NodeRoot) { Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue; Fail "$NodeRoot exists, doesn't work, and can't be moved aside." }
    Write-Host "Moved the existing, non-working $NodeRoot aside to $aside"
  }
  # A few tries: node.exe has just run, and an antivirus scanning it can hold a handle that
  # makes the folder rename fail for a moment.
  $moved = $false
  for ($i = 0; $i -lt 5 -and -not $moved; $i++) {
    try { Move-Item -LiteralPath $tree -Destination $NodeRoot -ErrorAction Stop; $moved = $true }
    catch { Start-Sleep -Seconds 1 }
  }
  Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
  if (-not $moved) { Fail "Could not move Node.js into $NodeRoot." }
  if (-not (Test-Node $NodeRoot)) {
    # Ran in staging, not here: don't leave it where `where npm` would find it.
    Rename-Item -LiteralPath $NodeRoot -NewName "nodejs.failed-$(Get-Date -Format yyyyMMddHHmmss)" -ErrorAction SilentlyContinue
    Fail "Node.js ran from staging but not from $NodeRoot."
  }
  if (-not (Test-Path $NpmBin)) { New-Item -ItemType Directory -Path $NpmBin -Force | Out-Null }

  # --- 7) user Path, last: only a Node.js that runs is put on it ----------------------------
  if (-not $NoUserPath) { Add-UserPath @($NodeRoot, $NpmBin) }
  Write-Host "[OK] Node.js $ver installed in $NodeRoot (no admin)."
  exit 0
} finally {
  Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
