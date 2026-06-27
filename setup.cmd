@echo off
rem One-time setup for Claude Overlay on a new machine.
cd /d "%~dp0"
echo ============================================================
echo   Claude Overlay - setup
echo ============================================================
echo.

rem --- 1. Python (must be a REAL interpreter, not the Microsoft Store alias) ---
rem `where python` is NOT enough: Windows 11 ships a 0-byte "App execution alias"
rem stub at %LOCALAPPDATA%\Microsoft\WindowsApps\python.exe that `where` finds even
rem when Python is NOT installed -- running it just prints "Python was not found..."
rem and exits 9009. So VERIFY by actually running --version, and prefer the `py`
rem launcher (which the Store alias never shadows).
rem Goto-structured (NOT nested parentheses) on purpose: a parenthesized block expands
rem %PY%/%DOPY% at parse time, so a value set by `set /p` inside it would read stale. Labels
rem let each line expand when reached.
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY ( python --version >nul 2>nul && set "PY=python" )
if defined PY goto pyfound

echo [X] Python 3 was not found on this PC.
echo     ^(The Microsoft Store "python" shortcut does NOT count as a real install.^)
set "DOPY=Y"
set /p DOPY="Install Python 3 now, automatically? (recommended) [Y/n] "
if /i "%DOPY%"=="n" goto pymanual
echo.
echo Installing Python 3 ^(per-user, no admin needed^) -- this can take a minute...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-python.ps1"
echo.
rem Re-detect. The PATH is NOT refreshed inside this window, so after the install also look in
rem the per-user dir where winget / the python.org installer place Python.
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY ( python --version >nul 2>nul && set "PY=python" )
if defined PY goto pyfound
rem (recurse from Programs\Python for python.exe -- a wildcard MID-path like Python3*\python.exe
rem  is NOT matched by `dir /s`, so search the base dir for the filename instead)
for /f "delims=" %%p in ('dir /b /s /a-d /o-n "%LOCALAPPDATA%\Programs\Python\python.exe" 2^>nul') do if not defined PY set PY="%%p"
if defined PY goto pyfound

:pymanual
echo.
echo [X] Python is not available in this window yet.
echo     If you just installed it, CLOSE this window and run setup.cmd again -- a fresh
echo     window picks up the updated PATH. Or install manually from
echo     https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), then re-run.
pause & exit /b 1

:pyfound
rem Read the version robustly for EVERY form of %PY% -- including a quoted full path with spaces,
rem which would break `for /f ... in ('%PY% ...')`; a temp file sidesteps the quoting entirely.
%PY% --version > "%TEMP%\_ov_pyver.txt" 2>&1
set "PYVER="
set /p PYVER=<"%TEMP%\_ov_pyver.txt"
del "%TEMP%\_ov_pyver.txt" >nul 2>nul
echo [OK] Python found: %PY% ^(%PYVER%^)

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

rem --- 2a. npm-shim heads-up (PowerShell + Windows' default Restricted ExecutionPolicy) ---
rem An npm install exposes %APPDATA%\npm\claude.ps1; PowerShell resolves `claude` to that
rem .ps1, and the default Restricted policy blocks it, so typing `claude` in PowerShell fails
rem with "running scripts is disabled on this system". CMD, this script, and the overlay's
rem SDK all use claude.cmd and are unaffected -- so we only warn (and point at the native build).
if exist "%APPDATA%\npm\claude.ps1" if not exist "%USERPROFILE%\.local\bin\claude.exe" (
  echo.
  echo [!] Heads-up: you have the npm 'claude' ^(claude.ps1^). In PowerShell, typing
  echo     'claude' may fail with "running scripts is disabled on this system" -- that's
  echo     Windows blocking .ps1 by default, NOT a broken install. Any one of these fixes it:
  echo       1^) just use CMD instead of PowerShell to run claude, or
  echo       2^) in PowerShell once:  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
  echo       3^) ^(recommended^) install the native build -- a real .exe, no policy gate:
  echo            irm https://claude.ai/install.ps1 ^| iex
  echo     The overlay app itself is unaffected; it launches claude via claude.cmd.
)

rem --- 2b. make sure you're logged in (uses YOUR subscription, no API key) ---
claude --version >nul 2>nul
echo.
echo If you haven't logged in yet, you need to do it once (a browser opens).
echo Tip: run setup in PowerShell or CMD, NOT Git Bash (the sign-in screen is blank there).
set /p DOLOGIN="Log in now with 'claude auth login'? [Y/n] "
if /i not "%DOLOGIN%"=="n" ( claude auth login )

rem --- 3. Python packages ---
echo.
rem Make sure pip exists FIRST. Some Python installs ship without it, or `pip` isn't on
rem PATH even though `python` is; `python -m pip` + ensurepip is the robust path.
%PY% -m pip --version >nul 2>nul
if errorlevel 1 (
  echo pip not found - bootstrapping it with ensurepip ...
  %PY% -m ensurepip --upgrade
  if errorlevel 1 (
    echo [X] Could not bootstrap pip. Reinstall Python from https://www.python.org/downloads/
    echo     ^(make sure the "pip" optional feature stays ticked^), then re-run setup.cmd.
    pause & exit /b 1
  )
)
echo Installing Python packages: claude-agent-sdk, pillow, keyboard ...
echo (Any "installed in ... which is not on PATH" warnings below are harmless.)
%PY% -m pip install --upgrade claude-agent-sdk pillow keyboard
if errorlevel 1 (
  echo [X] pip install failed. See the error above.
  pause & exit /b 1
)

echo.
echo ============================================================
echo   Done. Before first launch make sure you have logged in
echo   with YOUR OWN Claude subscription (claude auth login).
echo   Then double-click:  "Start Claude Overlay.cmd"
echo ============================================================
pause
