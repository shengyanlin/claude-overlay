# Creates a "Claude Overlay" shortcut on the current user's Desktop, pointing to
# this folder's launcher, with the orb icon. Portable: derives paths from its own
# location, so it works wherever you cloned the repo.
param([string]$Dir = $PSScriptRoot)

# Defensive: tolerate a caller that passes a quoted path or a trailing backslash
# (e.g. "%~dp0" expands to "...\claude-overlay\" and CommandLineToArgvW turns the
# closing \" into a literal quote, which would otherwise poison the path).
$Dir = $Dir.Trim('"').TrimEnd('\')

$launcher = Join-Path $Dir 'Start Claude Overlay.cmd'
if (-not (Test-Path $launcher)) {
    Write-Host "ERROR: 'Start Claude Overlay.cmd' not found next to this script ($Dir)." -ForegroundColor Red
    Write-Host "Run this from inside your claude-overlay folder." -ForegroundColor Red
    exit 1
}

$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'Claude Overlay.lnk'
$icon = Join-Path $Dir 'claude_overlay.ico'

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath = $launcher
$sc.WorkingDirectory = $Dir
if (Test-Path $icon) { $sc.IconLocation = "$icon,0" }
$sc.WindowStyle = 7   # minimized: the launcher window only flashes briefly
$sc.Description = 'Claude Overlay - screen-aware floating Claude Code chat'
$sc.Save()

Write-Host "Created 'Claude Overlay' shortcut on your Desktop." -ForegroundColor Green
Write-Host "  -> $launcher"
