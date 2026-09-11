@echo off
rem Update Claude Overlay: pull the latest code, then refresh this install (packages,
rem shortcut icon, and a proof that the app can still load).
rem
rem ONE file the user double-clicks, THREE modes inside it. The split is not tidiness --
rem each mode exists to satisfy a constraint the others cannot:
rem
rem   (no args)     what the user double-clicks. Copies itself to %TEMP% and re-execs from
rem                 there, because `git pull` below REPLACES this very file, and cmd.exe
rem                 reads the script it is executing from disk by BYTE OFFSET as it goes --
rem                 so after the pull it carries on at that offset inside whatever now
rem                 lives there. It came out right the two times it was observed, because
rem                 the line numbers happened to line up; a one-line edit is enough to make
rem                 it resume in the middle of a different command, and nothing would say so.
rem   --from-temp   the driver, executing from %TEMP% where git cannot touch the bytes being
rem                 read. Does the pull, then hands off to --finish.
rem   --finish      everything after the pull. Reached by calling the update.cmd sitting in
rem                 the REPO -- the copy that was just DOWNLOADED -- and never by falling
rem                 through inside the %TEMP% copy, whose body is the version being
rem                 replaced. This half is the one that knows where this release looks for
rem                 Python and what requirements.txt now pins, so it has to be the new one.
rem
rem This was two files (update.cmd + update-finish.cmd) for exactly that last reason. One
rem file with a mode flag keeps the same guarantee -- the hand-off still crosses from the
rem %TEMP% copy into the freshly pulled file -- while leaving the user a single thing to
rem double-click, which was the point. Recovering from an update that pulled the code and
rem then failed is now "double-click update.cmd again": the second pull is a no-op and the
rem run carries straight on into --finish.
rem
rem --finish is dispatched FIRST, before the re-exec check, and that order is load-bearing.
rem If --finish ever fell through to the re-exec instead, the driver would pull, call
rem --finish, land back at the top, copy itself to %TEMP%, pull again, forever. There is a
rem test pinning the order for that reason.
rem
rem The folder is passed as "%~dp0." and not "%~dp0": %~dp0 always ends in a backslash, and
rem a quoted argument ending in \" is the classic Windows quote-escape trap.
setlocal enabledelayedexpansion
if /i "%~1"=="--finish" goto finish

rem Everything below is inside ONE parenthesised block on purpose: cmd parses a block to
rem its closing paren BEFORE running any of it, so the hand-off and the exit are already in
rem memory and are not re-read from a file that may have changed underneath.
if /i not "%~1"=="--from-temp" (
  copy /y "%~f0" "%TEMP%\_ov_update.cmd" >nul
  if errorlevel 1 (
    echo [X] Could not stage the updater in "%TEMP%". Is the disk full?
    pause
    exit /b 1
  )
  cmd /c call "%TEMP%\_ov_update.cmd" --from-temp "%~dp0."
  set "RC=!errorlevel!"
  del "%TEMP%\_ov_update.cmd" >nul 2>nul
  exit /b !RC!
)

rem ---------------------------------------------------------------------------------
rem From here on we ARE the copy in %TEMP%, and %2 is the folder to update.
rem ---------------------------------------------------------------------------------
cd /d "%~2"
if errorlevel 1 ( echo [X] Cannot enter "%~2". & pause & exit /b 1 )
rem Absolute from here on. This script lives in %TEMP% now, so %~dp0 is NOT the app folder
rem -- and `call "update.cmd"` on its own does not work either: cmd looks up a quoted bare
rem filename as a literal program name and reports '"update.cmd"' is not recognized.
rem Caught by running this, not by reading it.
set "REPO=%CD%"
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
  echo     and unzip ALL of it over this folder, replacing every file.
  echo     The app is a folder of modules, not a single script - replacing
  echo     only claude_overlay.py leaves it unable to start.
  pause & exit /b 1
)
git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 (
  echo [X] This folder isn't a git clone, so there's nothing to pull.
  echo     Re-download the latest ZIP from
  echo       https://github.com/shengyanlin/claude-overlay
  echo     and unzip ALL of it over this folder, replacing every file.
  pause & exit /b 1
)

rem --- WHERE to pull from ------------------------------------------------------------
rem `git pull` with no arguments pulls the current branch from ITS tracking remote, and on a
rem clone that is a FORK that remote is the fork -- which is exactly as stale as the install.
rem Found the hard way: the version check asks GitHub for the newest tag on the SOURCE repo,
rem so the overlay correctly announced a new release while every pull here was a no-op
rem against a fork that did not have it. The button worked; there was nothing on the other
rem end of it, and a pull that succeeds while changing nothing is the one failure mode that
rem reports itself as success. So pull from `upstream` when the clone has one -- the
rem conventional name for the repo a fork was made from -- and from `origin` otherwise,
rem which is every normal install.
set "SRC=origin"
git remote get-url upstream >nul 2>nul
if not errorlevel 1 set "SRC=upstream"

rem --- and ONTO WHAT -----------------------------------------------------------------
rem Releases are cut on main. Pulling main while checked out on a topic branch would MERGE
rem the release into somebody's unfinished work and hand them the merge to untangle, which
rem is not a thing an updater gets to decide. An install parked off main is told to switch.
rem (A detached HEAD reports as "HEAD" here and lands in the same message, which is right --
rem it is equally not a branch anything should be pulled into.)
set "BR="
for /f "delims=" %%B in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set "BR=%%B"
if /i not "%BR%"=="main" (
  echo [X] This clone is on branch "%BR%", not main.
  echo     Releases live on main, so updating from here would merge the new release
  echo     into that branch's work. Switch first:
  echo         git switch main
  echo     then re-run update.cmd.
  pause & exit /b 1
)

rem A dirty tree makes the pull fail PART WAY THROUGH, with git naming the files but not
rem what to do about them. Say it before anything moves. Untracked files are not checked:
rem a pull does not overwrite those, and refusing over a stray .log would be a false alarm.
git diff --quiet HEAD
if errorlevel 1 (
  echo [X] You have uncommitted changes here, and the pull would overwrite them.
  echo     Commit them, or set them aside with:
  echo         git stash
  echo     then re-run update.cmd.
  pause & exit /b 1
)

echo Pulling the latest code from %SRC%/main...
rem --ff-only so an update can only ever move this clone FORWARD onto released code. Without
rem it, a clone carrying local commits on main gets a merge commit nobody asked for, made by
rem a script running unattended behind a button.
git pull --ff-only %SRC% main
if errorlevel 1 (
  echo [X] git pull failed ^(see above^). If main here carries commits of its own,
  echo     it cannot fast-forward onto the release; move them to a branch first.
  pause & exit /b 1
)

rem --- hand the rest to the code we JUST downloaded ----------------------------
rem Deliberately the freshly pulled update.cmd rather than the body of this %TEMP% copy:
rem the post-pull half has to match the code it is about to check, and it is the half that
rem knows where this release looks for Python.
if not exist "%REPO%\update.cmd" (
  echo.
  echo [X] The code updated, but update.cmd is no longer in this folder, so the
  echo     packages were NOT refreshed. Double-click setup.cmd to finish.
  pause & exit /b 1
)
call "%REPO%\update.cmd" --finish
exit /b %errorlevel%

rem ---------------------------------------------------------------------------------
rem :finish -- the post-pull half. We are the FRESHLY PULLED file here.
rem ---------------------------------------------------------------------------------
rem Note on notices below: this script runs with delayed expansion on, and a lone `!` in an
rem `echo` is EATEN by it -- `echo [!] x` prints `[] x`, and `^!` does not rescue it either.
rem The old update-finish.cmd shipped several `[!]` notices that every user saw as `[]`. So
rem no notice here contains a bare `!`.
:finish
cd /d "%~dp0"
rem Fresh state, measured here: a SETUPDONE inherited from the calling environment would
rem otherwise skip the run-setup offer below on the very first pass -- same rationale as
rem the `set "PYW="` inside the find block.
set "SETUPDONE="

rem --- refresh Python packages ------------------------------------------------
rem Into the interpreter the LAUNCHER uses, not whichever one `py -3` happens to pick.
rem "Start Claude Overlay.cmd" runs whichever `pythonw` it finds first; on a machine with
rem two Pythons (a common one: python.org plus the Microsoft Store build) upgrading the
rem wrong one leaves the app running against packages nobody refreshed - and the symptom
rem of that is a launch that silently does nothing.
rem Verify by RUNNING each candidate, not by testing a different file: a Win11 box
rem without Python still has the Store alias stub in %LOCALAPPDATA%\...\WindowsApps\,
rem which `where` finds but which only prints "Python was not found".
rem Every probe goes through `call`: `where` can return a .bat/.cmd shim (pyenv-win and
rem some conda wrappers install one), and running a batch file from a batch file without
rem `call` transfers control and never returns -- which would end this script mid-check.
rem And PATH is not the whole world: setup.cmd installs into
rem %LOCALAPPDATA%\Programs\Python\Python3xx\ and finds it there by scanning. Refreshing
rem packages into "whatever is on PATH" while the launcher runs a Python that is not, is
rem how an update reports success and changes nothing the app will load.
rem
rem :findpython is a re-entry point, not decoration -- see the give-up path below, which
rem can install a Python and then has to look again. `set "PYW="` clears the state so the
rem answer comes from re-measuring rather than from whatever setup.cmd said about itself.
:findpython

rem ---- BEGIN find-pythonw (kept identical in Diagnose.cmd, update.cmd and setup.cmd) ----
set "PYW="
for /f "usebackq delims=" %%i in (`where pythonw 2^>nul`) do if not defined PYW (call "%%i" -c "pass" >nul 2>nul && set "PYW=%%i")
if not defined PYW for /f "delims=" %%p in ('dir /b /s /a-d /o-n "%LOCALAPPDATA%\Programs\Python\pythonw.exe" 2^>nul') do if not defined PYW (call "%%p" -c "pass" >nul 2>nul && set "PYW=%%p")
if not defined PYW for /d %%d in ("%ProgramFiles%\Python3*") do if not defined PYW (call "%%d\pythonw.exe" -c "pass" >nul 2>nul && set "PYW=%%d\pythonw.exe")
if not defined PYW for /d %%d in ("%SystemDrive%\Python3*") do if not defined PYW (call "%%d\pythonw.exe" -c "pass" >nul 2>nul && set "PYW=%%d\pythonw.exe")
rem ---- END find-pythonw ----

set "PY="
rem pip's output is invisible under pythonw (no console), so drive pip with the
rem python.exe beside it WHEN that one also runs - same install, same site-packages,
rem readable output. When it doesn't, use pythonw itself anyway: upgrading the right
rem environment matters more than watching it happen, and `if errorlevel 1` below still
rem catches a failure. Only the launcher's own interpreter is ever the right target.
set "SIB="
if defined PYW set "SIB=!PYW:pythonw.exe=python.exe!"
if defined SIB (call "!SIB!" -c "pass" >nul 2>nul && set PY="!SIB!")
if not defined PY if defined PYW set PY="!PYW!"
if not defined PY ( call py -3 -c "pass" >nul 2>nul && set "PY=py -3" )
if not defined PY ( call python -c "pass" >nul 2>nul && set "PY=python" )
if not defined PY for /f "delims=" %%p in ('dir /b /s /a-d /o-n "%LOCALAPPDATA%\Programs\Python\python.exe" 2^>nul') do if not defined PY (call "%%p" -c "pass" >nul 2>nul && set PY="%%p")

if defined PY goto haspython

rem --- no interpreter: RUN the fix rather than name it -------------------------
rem This branch used to print "Double-click setup.cmd" and stop, which is a dead end for
rem the person reading it. Everybody who lands here got here by double-clicking THIS file,
rem very often from a Desktop shortcut, from which the folder -- and therefore setup.cmd --
rem is not visible. And update.cmd never installs Python itself, so "just run Update again"
rem could not clear the wall however many times they tried it. Both routes a stuck user
rem actually takes ended at "go and find a file you cannot see".
rem
rem That is not hypothetical: on a managed corporate machine whose device policy blocks
rem PSF-signed Python installers outright, this is the ONLY branch an affected user ever
rem reaches -- so it had to become the branch that fixes the problem.
rem
rem SETUPDONE latch: a setup that did NOT fix it must end at the instructions, not loop back
rem to the same question forever. `if exist` guard: someone who copied one file out of the
rem ZIP has no setup.cmd, and offering to run something that is not there is worse than the
rem old message.
rem
rem Not inside parentheses on purpose. cmd expands %VAR% for a whole parenthesised block
rem when it PARSES the block, so a value read by `set /p` inside one comes back stale --
rem the same trap setup.cmd documents at its own prompt.
echo.
echo [X] The code updated, but nothing on this PC would run Python -- so the packages
echo     were NOT refreshed and the app cannot start yet.
echo.
if defined SETUPDONE goto manualfix
if not exist "%~dp0setup.cmd" goto manualfix
echo   setup.cmd fixes this: it installs Python for you ^(per-user, no admin needed^).
echo.
set "DOSETUP=Y"
set /p DOSETUP="  Run setup.cmd now? [Y/n] "
rem First character only, so "no" is a refusal too and not an unrecognised answer that
rem gets read as consent. Enter leaves DOSETUP as the default Y.
if /i "!DOSETUP:~0,1!"=="n" goto manualfix
set "SETUPDONE=1"
echo.
rem Through a fresh cmd, because THIS window has delayed expansion on and setup.cmd does
rem not: with it on, cmd eats the lone `!` in setup's own `[!]` notices, silently rewriting
rem the messages a stuck user is relying on. `cmd /c call "..."` (rather than `start`) also
rem keeps the quotes -- the command no longer begins with one -- and returns control here
rem when setup is done.
cmd /c call "%~dp0setup.cmd"
echo.
echo Re-checking for Python ...
goto findpython

:manualfix
echo   The code IS updated - only the packages were skipped, so the app will still
echo   start once a Python is present.
echo   Fix: double-click setup.cmd. It installs Python when it is missing
echo        ^(per-user, no admin^) and installs the packages in the same run.
echo        Manual alternative: https://www.python.org/downloads/
echo        ^(tick "Add python.exe to PATH"^), then run setup.cmd.
echo        On a work PC that refuses that download ^(e.g. HTTP 403^), two routes that
echo        need no working download are described in: "%~dp0offline\README.md"
echo.
pause & exit /b 1

:haspython
echo.
rem requirements.txt is the single source of the version list. Naming packages here as
rem well is how the two drifted: the file said one thing and every user got another.
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
rem A tree without pip cannot be refreshed BY pip, and `%PY% -m pip` then fails with
rem "No module named pip" -- which the error text below would misread as a package
rem problem. setup.cmd has bootstrapped pip via ensurepip since it existed; this half
rem never did, so on a hand-dropped Python (offline\README.md route B blesses exactly
rem that) "run Update again" was a wall however many times it was tried.
call %PY% -m pip --version >nul 2>nul
if errorlevel 1 (
  echo pip is missing from this Python - bootstrapping it with ensurepip ...
  call %PY% -m ensurepip --upgrade
)
echo Refreshing Python packages with !PY! ...
call %PY% -m pip install --upgrade -r "%~dp0requirements.txt"
rem An interrupted or proxy-blocked upgrade can leave a package UNINSTALLED - pip
rem removes the old version before installing the new one. Saying "[OK] Updated" over
rem the top of that is how an update turns into a launch that does nothing, so the
rem failure has to stop the script.
if errorlevel 1 (
  echo.
  echo [X] Refreshing the packages FAILED ^(see the pip output above^).
  echo     Your install may now be incomplete - do not skip this.
  echo     Retry, or run it yourself:
  echo       %PY% -m pip install --upgrade -r requirements.txt
  echo     Then double-click Diagnose.cmd to confirm the app can load.
  pause & exit /b 1
)

rem --- refresh the desktop shortcut icon IF one already exists ---
rem The .lnk is machine-specific (gitignored), so git pull can't touch it. If a "Claude
rem Overlay" shortcut is on the Desktop, re-point it at the current icon. We skip this when
rem there's no shortcut, so an update never creates one the user didn't ask for.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[Environment]::GetFolderPath('Desktop'); if (Test-Path (Join-Path $d 'Claude Overlay.lnk')) { & '.\create-shortcut.ps1'; Write-Host '[OK] Desktop shortcut icon refreshed.' }"

rem --- prove the updated install can actually start ---------------------------
rem Checking here is the whole difference between "it broke and I don't know why" and
rem "the update told me". preflight loads the app exactly the way the launcher will.
echo.
echo Checking that the updated app can load...
call %PY% "%~dp0preflight.py" >nul 2>nul
if errorlevel 1 (
  echo.
  echo [X] The update left this install unable to start.
  echo     Run Diagnose.cmd for the details ^(and what fixes it^).
  echo.
  call %PY% "%~dp0preflight.py"
  pause & exit /b 1
)
echo [OK] The app loads.

echo.
rem OV_UPDATE_AUTO is set by win32utils.run_overlay_update -- i.e. we were started BY a running
rem overlay's one-click Update button, which waits on this process and then restarts itself.
rem The `pause` below would make that wait last until somebody walked over and pressed a key,
rem with the button stuck on "Updating..." the whole time and an update that had ALREADY
rem SUCCEEDED looking hung. So on the automated path we say what happens next and get out of
rem the way. Only THIS success exit is skipped: every failure path above still pauses, because
rem the console is where the fix is written and closing it would take the fix with it.
if defined OV_UPDATE_AUTO (
  echo ============================================================
  echo   [OK] Updated. The overlay restarts itself now.
  echo ============================================================
  exit /b 0
)
echo ============================================================
echo   [OK] Updated. IMPORTANT: close the running overlay and
echo   re-open it ^("Start Claude Overlay.cmd"^) for the changes
echo   to take effect - it does not reload while running.
echo ============================================================
pause
