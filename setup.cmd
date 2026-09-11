@echo off
rem One-time setup for Claude Overlay on a new machine.
cd /d "%~dp0"
echo ============================================================
echo   Claude Overlay - setup
echo ============================================================
echo.

rem --- 1. Python: THE interpreter the launcher will run, found the way IT finds it ---
rem This section used to answer a different question ("is any Python on this PC?") with a
rem different search: `py -3` (the REGISTRY, PEP 514) first, then a PATH `python`, then the
rem folder scan -- while "Start Claude Overlay.cmd" asks `where pythonw` (PATH) first and
rem then scans. On a machine where those two searches disagree -- say a registry-registered
rem Anaconda or an old python.org install that was never added to PATH, next to the
rem standalone tree this script installs under %LOCALAPPDATA%\Programs\Python\ -- every
rem package below went into an interpreter the launcher never runs, and the preflight at
rem the end of this file then proved THE WRONG PYTHON loads: setup printed "[OK] The app
rem loads." and the next double-click died on `import PIL` with both packages reported
rem "not installed". Nothing on screen connected the two.
rem So the search below is the launcher's own, byte for byte (the marked block is kept
rem identical across the four .cmd files -- a test compares them), and only after it comes
rem the sibling python.exe swap for readable pip output, exactly as update.cmd does it.
rem Still goto-structured (NOT nested parentheses): a parenthesized block expands
rem %PY%/%DOPY% at parse time, so a value set by `set /p` inside it would read stale --
rem and this file deliberately runs WITHOUT delayed expansion (its `[!]` notices depend
rem on that), so the swap goes through `call set` instead of `!PYW:...!`.
rem PYTRIED is cleared, not trusted: a value inherited from the calling environment would
rem skip the install offer on the very first pass -- update.cmd documents the same trap.
set "PYTRIED="

:findpython

rem ---- BEGIN find-pythonw (kept identical in Diagnose.cmd, update.cmd and setup.cmd) ----
set "PYW="
for /f "usebackq delims=" %%i in (`where pythonw 2^>nul`) do if not defined PYW (call "%%i" -c "pass" >nul 2>nul && set "PYW=%%i")
if not defined PYW for /f "delims=" %%p in ('dir /b /s /a-d /o-n "%LOCALAPPDATA%\Programs\Python\pythonw.exe" 2^>nul') do if not defined PYW (call "%%p" -c "pass" >nul 2>nul && set "PYW=%%p")
if not defined PYW for /d %%d in ("%ProgramFiles%\Python3*") do if not defined PYW (call "%%d\pythonw.exe" -c "pass" >nul 2>nul && set "PYW=%%d\pythonw.exe")
if not defined PYW for /d %%d in ("%SystemDrive%\Python3*") do if not defined PYW (call "%%d\pythonw.exe" -c "pass" >nul 2>nul && set "PYW=%%d\pythonw.exe")
rem ---- END find-pythonw ----

rem pip's output is invisible under pythonw (no console), so drive everything below with
rem the python.exe BESIDE the launcher's pythonw when that one also runs -- same install,
rem same site-packages, readable output -- and with pythonw itself when it does not.
rem Only when NO pythonw exists anywhere does the launcher itself fall back to a console
rem `python`, so only then may the PATH/py-launcher forms decide where the packages go.
set "PY="
set "SIB="
if defined PYW call set "SIB=%%PYW:pythonw.exe=python.exe%%"
if defined SIB call "%SIB%" -c "pass" >nul 2>nul && set PY="%SIB%"
if not defined PY if defined PYW set PY="%PYW%"
if not defined PY ( call py -3 -c "pass" >nul 2>nul && set "PY=py -3" )
if not defined PY ( call python -c "pass" >nul 2>nul && set "PY=python" )
if not defined PY for /f "delims=" %%p in ('dir /b /s /a-d /o-n "%LOCALAPPDATA%\Programs\Python\python.exe" 2^>nul') do if not defined PY (call "%%p" -c "pass" >nul 2>nul && set PY="%%p")
if defined PY goto pyfound

if defined PYTRIED goto pymanual
echo [X] Python 3 was not found on this PC.
echo     ^(The Microsoft Store "python" shortcut does NOT count as a real install.^)
set "DOPY=Y"
set /p DOPY="Install Python 3 now, automatically? (recommended) [Y/n] "
rem First character only, so "no" is a refusal too and not an unrecognised answer that
rem gets read as consent. Enter leaves DOPY as the default Y.
if /i "%DOPY:~0,1%"=="n" goto pymanual
echo.
echo Installing Python 3 ^(per-user, no admin needed^) -- this can take a minute...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-python.ps1"
echo.
rem Re-measure with the SAME search rather than re-detecting some other way: the PATH in
rem this window is stale for its whole life, which is exactly why the shared block scans
rem %LOCALAPPDATA%\Programs\Python\ -- where install-python.ps1 just put the interpreter.
set "PYTRIED=1"
echo Re-checking for Python ...
goto findpython

:pymanual
echo.
echo [X] Python is not available in this window yet.
echo     If you just installed it, CLOSE this window and run setup.cmd again -- a fresh
echo     window picks up the updated PATH. Or install manually from
echo     https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), then re-run.
echo.
rem Sending a blocked user to python.org is sending them back at the wall they just hit: on a
rem managed PC that download is exactly what gets refused (403), and even when it arrives the
rem installer can be blocked by signature. The routes that do not need it are in offline\.
echo     On a work PC that REFUSED the download above ^(e.g. HTTP 403^), python.org will
echo     refuse it again. Two routes that need no working download are described in:
echo       "%~dp0offline\README.md"
pause & exit /b 1

:pyfound
rem Read the version robustly for EVERY form of %PY% -- including a quoted full path with spaces,
rem which would break `for /f ... in ('%PY% ...')`; a temp file sidesteps the quoting entirely.
call %PY% --version > "%TEMP%\_ov_pyver.txt" 2>&1
set "PYVER="
set /p PYVER=<"%TEMP%\_ov_pyver.txt"
del "%TEMP%\_ov_pyver.txt" >nul 2>nul
rem A pythonw without a python.exe sibling prints nothing at all (no console), and an
rem empty "()" here reads like something broke when nothing did.
if not defined PYVER set "PYVER=version not printable: pythonw has no console"
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
call %PY% -m pip --version >nul 2>nul
if errorlevel 1 (
  echo pip not found - bootstrapping it with ensurepip ...
  call %PY% -m ensurepip --upgrade
  if errorlevel 1 (
    echo [X] Could not bootstrap pip. Reinstall Python from https://www.python.org/downloads/
    echo     ^(make sure the "pip" optional feature stays ticked^), then re-run setup.cmd.
    pause & exit /b 1
  )
)
rem requirements.txt is the single source of the version list. This used to name the
rem packages here instead, so the file constrained nobody and every user got whatever PyPI
rem shipped that morning - while this machine stayed on the version it first installed.
if not exist "%~dp0requirements.txt" (
  echo [X] requirements.txt is missing, so there is nothing to install from.
  echo     Re-download the ZIP and unzip ALL of it over this folder.
  pause & exit /b 1
)
rem python-build-standalone trees ship Lib\EXTERNALLY-MANAGED, and while it is present pip
rem refuses the interpreter with "externally-managed-environment". install-python.ps1
rem strips it at uv-install time, but a Python dropped in BY HAND still carries it -- and
rem offline\README.md route B blesses exactly that. Re-establish the invariant here, every
rem run, scoped to the folder the overlay owns (a system/conda interpreter elsewhere on
rem PATH keeps its marker) -- same sweep install-python.ps1 performs after a uv install.
for /f "delims=" %%e in ('dir /b /s /a-d "%LOCALAPPDATA%\Programs\Python\EXTERNALLY-MANAGED" 2^>nul') do del /f /q "%%e" >nul 2>nul
echo Installing the Python packages listed in requirements.txt ...
echo (Any "installed in ... which is not on PATH" warnings below are harmless.)
call %PY% -m pip install --upgrade -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo [X] pip install failed. See the error above.
  pause & exit /b 1
)

rem --- 4. prove it can actually start ---------------------------------------
rem "Setup completed" and "the app runs" are not the same claim, and the gap between
rem them is invisible: the overlay launches under pythonw, so a bad install shows up as
rem a double-click that does nothing at all. Check it here, while someone is watching.
echo.
echo Checking that the app can load ...
call %PY% "%~dp0preflight.py" >nul 2>nul
if errorlevel 1 (
  echo.
  echo [X] Setup finished, but the app cannot start yet. Details:
  echo.
  call %PY% "%~dp0preflight.py"
  echo.
  echo     Fix what's listed above, then run Diagnose.cmd to re-check.
  pause & exit /b 1
)
echo [OK] The app loads.

echo.
echo ============================================================
echo   Done. Before first launch make sure you have logged in
echo   with YOUR OWN Claude subscription (claude auth login).
echo   Then double-click:  "Start Claude Overlay.cmd"
echo.
echo   If it ever fails to open, double-click:  Diagnose.cmd
echo ============================================================
pause
