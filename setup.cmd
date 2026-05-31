@echo off
rem One-time setup for Claude Overlay on a new machine.
cd /d "%~dp0"
echo ============================================================
echo   Claude Overlay - setup
echo ============================================================
echo.

rem --- 1. Python ---
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY ( where py >nul 2>nul && set "PY=py -3" )
if not defined PY (
  echo [X] Python 3 not found. Install it from https://www.python.org/downloads/
  echo     ^(tick "Add python.exe to PATH" in the installer^), then re-run setup.cmd.
  pause & exit /b 1
)
echo [OK] Python found: %PY%

rem --- 2. claude CLI ---
where claude >nul 2>nul
if errorlevel 1 (
  echo [!] 'claude' CLI not found on PATH.
  echo     Install it:   npm install -g @anthropic-ai/claude-code
  echo     Then run:     claude        ^(and use /login with YOUR subscription^)
) else (
  echo [OK] claude CLI found.
)

rem --- 3. Python packages ---
echo.
echo Installing Python packages: claude-agent-sdk, pillow, keyboard ...
%PY% -m pip install --upgrade claude-agent-sdk pillow keyboard
if errorlevel 1 (
  echo [X] pip install failed. See the error above.
  pause & exit /b 1
)

echo.
echo ============================================================
echo   Done. Before first launch make sure you have run 'claude'
echo   and logged in with YOUR OWN Claude subscription.
echo   Then double-click:  "Start Claude Overlay.cmd"
echo ============================================================
pause
