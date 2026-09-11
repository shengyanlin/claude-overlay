@echo off
rem Print one readable report explaining why Claude Overlay won't start, and put a copy
rem on the clipboard so it can be pasted straight into a message.
rem
rem This exists because the overlay runs under pythonw, which has no console: when it
rem fails to launch you see nothing at all. Double-click this, send the output.
cd /d "%~dp0"
setlocal enabledelayedexpansion

echo ============================================================
echo   Claude Overlay - diagnose
echo ============================================================
echo.

rem --- Resolve the SAME interpreter the launcher uses ---------------------------
rem "Start Claude Overlay.cmd" runs whichever `pythonw` it finds first, so that is the
rem Python whose packages matter. Reporting on any other one is how "but I installed it!"
rem happens -- so the search order below is a copy of the launcher's, not an equivalent.
rem Candidates are checked by RUNNING them, in the launcher's own order. pythonw has
rem no console so it cannot print a version, but its exit code comes back -- and a
rem redirected `>` still reaches the file, which is why the report below works even
rem when pythonw is the only interpreter this machine has.
rem Every probe goes through `call`: `where` can return a .bat/.cmd shim (pyenv-win and
rem some conda wrappers install one), and running a batch file from a batch file without
rem `call` transfers control and never returns -- which would end this script mid-check.
rem PATH is not the whole world either: setup.cmd installs into
rem %LOCALAPPDATA%\Programs\Python\Python3xx\ and finds it there by scanning, so this
rem report has to look in the same folders the launcher now looks in. Reporting "no
rem Python" about a machine that has one is the same failure as refusing to launch on it.
rem ---- BEGIN find-pythonw (kept identical in Diagnose.cmd, update.cmd and setup.cmd) ----
set "PYW="
for /f "usebackq delims=" %%i in (`where pythonw 2^>nul`) do if not defined PYW (call "%%i" -c "pass" >nul 2>nul && set "PYW=%%i")
if not defined PYW for /f "delims=" %%p in ('dir /b /s /a-d /o-n "%LOCALAPPDATA%\Programs\Python\pythonw.exe" 2^>nul') do if not defined PYW (call "%%p" -c "pass" >nul 2>nul && set "PYW=%%p")
if not defined PYW for /d %%d in ("%ProgramFiles%\Python3*") do if not defined PYW (call "%%d\pythonw.exe" -c "pass" >nul 2>nul && set "PYW=%%d\pythonw.exe")
if not defined PYW for /d %%d in ("%SystemDrive%\Python3*") do if not defined PYW (call "%%d\pythonw.exe" -c "pass" >nul 2>nul && set "PYW=%%d\pythonw.exe")
rem ---- END find-pythonw ----

set "PY="
if defined PYW set PY="!PYW!"
if not defined PY ( call pyw -3 -c "pass" >nul 2>nul && set "PY=pyw -3" )
if not defined PY ( call python -c "pass" >nul 2>nul && set "PY=python" )
if not defined PY ( call py -3 -c "pass" >nul 2>nul && set "PY=py -3" )
if not defined PY for /f "delims=" %%p in ('dir /b /s /a-d /o-n "%LOCALAPPDATA%\Programs\Python\python.exe" 2^>nul') do if not defined PY (call "%%p" -c "pass" >nul 2>nul && set PY="%%p")
if not defined PY (
  echo [X] Nothing on this PC would run Python. That alone explains the failure.
  echo.
  echo     This is what the machine actually has:
  echo     where pythonw
  where pythonw 2>nul || echo         ^(nothing found^)
  echo     where python
  where python 2>nul || echo         ^(nothing found^)
  echo     where py
  where py 2>nul || echo         ^(nothing found^)
  echo     off PATH ^(the folders setup.cmd installs into^)
  dir /b /s /a-d "%LOCALAPPDATA%\Programs\Python\python*.exe" 2>nul || echo         ^(nothing found^)
  echo.
  echo     Paths under \WindowsApps\ are Windows placeholders, not a Python install:
  echo     running one only prints "Python was not found..." and stops.
  echo.
  echo     Fix: double-click setup.cmd - it installs Python when it is missing
  echo     ^(per-user, no admin^). Manual alternative:
  echo     https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^).
  echo.
  pause & exit /b 1
)
echo Interpreter the launcher resolves to: !PY!
echo.

set "REPORT=%TEMP%\claude-overlay-report.txt"
call !PY! "%~dp0preflight.py" > "%REPORT%" 2>&1
type "%REPORT%"

rem Best-effort clipboard copy so the report can just be pasted into a message. That is the
rem point of it for the person who has a broken install -- but it does replace whatever they
rem had copied, so anyone running this to CHECK something (rather than to report it) can pass
rem --no-clip and keep their clipboard.
if /i "%~1"=="--no-clip" (
  echo ^(Clipboard left alone: --no-clip.^)
) else (
  type "%REPORT%" | clip >nul 2>nul && echo (This report has been copied to your clipboard.)
)
echo.
echo Saved to: %REPORT%
echo.
pause
