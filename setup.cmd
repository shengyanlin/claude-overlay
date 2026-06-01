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

rem --- 2. claude CLI (auto-install via the native installer if missing; no Node needed) ---
set "PATH=%USERPROFILE%\.local\bin;%APPDATA%\npm;%PATH%"
where claude >nul 2>nul
if errorlevel 1 (
  echo [!] 'claude' CLI not found. Installing it with the official native installer
  echo     ^(no Node.js required^)...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"
  set "PATH=%USERPROFILE%\.local\bin;%PATH%"
  where claude >nul 2>nul
  if errorlevel 1 (
    echo [X] Install didn't complete. Install manually, then re-run setup.cmd:
    echo       PowerShell:  irm https://claude.ai/install.ps1 ^| iex
    echo       or WinGet:   winget install Anthropic.ClaudeCode
    echo       or npm:      npm install -g @anthropic-ai/claude-code   ^(needs Node 18+^)
    pause & exit /b 1
  )
  echo [OK] claude CLI installed.
) else (
  echo [OK] claude CLI found.
)

rem --- 2b. make sure you're logged in (uses YOUR subscription, no API key) ---
claude --version >nul 2>nul
echo.
echo If you haven't logged in yet, a browser login will be needed once.
echo This opens 'claude' so you can run /login with YOUR Claude subscription.
echo (Close it with Ctrl+C or /exit when done — then setup continues.)
set /p DOLOGIN="Open 'claude' to log in now? [Y/n] "
if /i not "%DOLOGIN%"=="n" ( claude )

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
