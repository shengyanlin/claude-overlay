@echo off
rem Update Claude Overlay to the latest version (git pull + refresh packages).
cd /d "%~dp0"
echo ============================================================
echo   Claude Overlay - update
echo ============================================================
echo.

rem --- needs git + a clone to pull into ---
where git >nul 2>nul
if errorlevel 1 (
  echo [X] git not found. You probably installed via the ZIP download.
  echo     Re-download the latest ZIP from the green "Code" button at
  echo       https://github.com/shengyanlin/claude-overlay
  echo     and unzip it over this folder ^(replace claude_overlay.py^).
  pause & exit /b 1
)
git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 (
  echo [X] This folder isn't a git clone, so there's nothing to pull.
  echo     Re-download the latest ZIP from
  echo       https://github.com/shengyanlin/claude-overlay
  pause & exit /b 1
)

echo Pulling the latest code...
git pull
if errorlevel 1 (
  echo [X] git pull failed ^(see above^). If you edited files locally, stash or
  echo     revert them first, then re-run update.cmd.
  pause & exit /b 1
)

rem --- refresh Python packages (best-effort: catches occasional SDK fixes) ---
rem Verify by running --version (not `where`): a Win11 machine without Python still
rem has the Microsoft Store alias stub %LOCALAPPDATA%\...\WindowsApps\python.exe that
rem `where` finds but that only prints "Python was not found" -- so trust --version.
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY ( python --version >nul 2>nul && set "PY=python" )
if defined PY (
  echo.
  echo Refreshing Python packages ^(claude-agent-sdk, pillow, keyboard^)...
  %PY% -m pip install --upgrade --quiet claude-agent-sdk pillow keyboard
)

rem --- refresh the desktop shortcut icon IF one already exists ---
rem The .lnk is machine-specific (gitignored), so git pull can't touch it. If a "Claude
rem Overlay" shortcut is on the Desktop, re-point it at the current icon. We skip this when
rem there's no shortcut, so update.cmd never creates one the user didn't ask for.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[Environment]::GetFolderPath('Desktop'); if (Test-Path (Join-Path $d 'Claude Overlay.lnk')) { & '.\create-shortcut.ps1'; Write-Host '[OK] Desktop shortcut icon refreshed.' }"

echo.
echo ============================================================
echo   [OK] Updated. IMPORTANT: close the running overlay and
echo   re-open it ^("Start Claude Overlay.cmd"^) for the changes
echo   to take effect - it does not reload while running.
echo ============================================================
pause
