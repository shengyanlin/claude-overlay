@echo off
rem Set up Codex mode (the titlebar's Claude <-> Codex switch): install the codex CLI if it
rem is missing -- Node.js too when there is no npm -- and sign in to it.
rem
rem ONE script, three callers, so the three can't drift apart:
rem   setup.cmd             --from-setup    a fresh install: installs, then offers sign-in.
rem   (double-clicked)      (no args)       the same, then waits so the result can be read.
rem   update.cmd --finish   --from-update   an update. An install older than Codex mode never
rem                                         got the CLI from setup, which is how an updated
rem                                         overlay ended up with a Codex switch that could
rem                                         not be clicked. Asks before installing, and
rem                                         remembers a "no" so the next update doesn't ask
rem                                         again (the overlay's button still works).
rem   the overlay           --from-app      its "Set up Codex" button: the user already asked,
rem                                         so the CLI and the sign-in aren't asked about
rem                                         again (installing Node.js still is); waits at the
rem                                         end only when something is left to read.
rem
rem Never fatal for a caller -- Claude mode doesn't need any of this. The exit code says how
rem far it got, and the overlay reads it:
rem   0 = codex installed and signed in   2 = installed, not signed in   3 = not installed
rem
rem A setup that was TRIED and didn't finish -- Node.js, npm or the sign-in failed; a "no" is
rem not a failure -- is written to %LOCALAPPDATA%\claude-overlay\codex-setup-result when setup
rem or an update ran it, and the overlay's next launch says so with a retry button. That file,
rem the pause at the end and the no-admin Node.js below all come from one field report: a
rem 1.25.0 -> 1.25.1 update on a managed PC with no Node.js "seemed to install" and then the
rem window closed. winget's Node.js needs an admin to approve it; a failed Codex setup doesn't
rem fail the update, and the app's Update button closes the window on success -- so the reason
rem went with the window, and nothing afterwards said Codex still wasn't set up.
rem
rem DisableDelayedExpansion because update.cmd calls this with delayed expansion ON, and
rem `call` shares the caller's setting: a lone "!" in the messages below would vanish.
rem goto-structured on purpose: PATH changes below and is re-read on the next line, which a
rem parenthesized block would expand at parse time, before the change.
setlocal EnableExtensions DisableDelayedExpansion
set "MODE=%~1"
set "STATEDIR=%LOCALAPPDATA%\claude-overlay"
set "DECLINED=%STATEDIR%\codex-setup-declined"
set "RESULT=%STATEDIR%\codex-setup-result"
rem What failed, when something did: node-failed / npm-failed / login-failed. Stays empty for
rem a success and for a "no", so neither is reported later as a failure.
set "WHY="
rem npm's global bin, and Node's own folder for an install made earlier this session: the
rem machine-wide one winget's MSI makes, then the per-user one install-node.ps1 makes -- in
rem that order, so a winget install made after a per-user attempt is the npm found.
set "PATH=%APPDATA%\npm;%ProgramFiles%\nodejs;%LOCALAPPDATA%\Programs\nodejs;%PATH%"
echo.

where codex >nul 2>nul
if not errorlevel 1 goto found
if /i not "%MODE%"=="--from-update" goto install
if exist "%DECLINED%" goto skipped
echo [NOTE] New in this version: Codex mode -- switch the overlay between Claude and
echo        OpenAI's Codex from its titlebar. It needs the codex CLI, which isn't on
echo        this PC yet. Claude mode works the same either way.
set "ANS=Y"
set /p ANS="Install the codex CLI now (npm install -g @openai/codex)? [Y/n] "
if /i "%ANS:~0,1%"=="n" goto decline

:install
echo [!] 'codex' CLI not found -- installing it for Codex mode.
where npm >nul 2>nul
if not errorlevel 1 goto npm
echo     Installing it needs npm, which comes with Node.js -- not on this PC either.
rem Asked even from the app's button: Node.js is a bigger install than "Codex" suggests.
set "ANS=Y"
set /p ANS="Install Node.js LTS now (into your user folder, no admin needed)? [Y/n] "
if /i "%ANS:~0,1%"=="n" goto decline
set "WHY=node-failed"
rem 1) The official .zip into %LOCALAPPDATA%\Programs\nodejs: no installer, no admin, so it
rem    works for a standard user on a managed PC. Bare `powershell`, not a full path: it is on
rem    every Windows PATH, and the test suite stands in a fake for it.
if not exist "%~dp0install-node.ps1" goto nodewinget
where powershell >nul 2>nul
if errorlevel 1 goto nodewinget
call powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-node.ps1"
rem Its exit code, not just `where npm`: a failure is a reason to try winget even if some
rem npm.cmd turned up.
if errorlevel 1 goto nodewinget
where npm >nul 2>nul
if not errorlevel 1 goto npm
:nodewinget
rem 2) winget's Node.js, the machine-wide MSI. Second because it needs an admin to approve it.
where winget >nul 2>nul
if errorlevel 1 goto manual
echo     Trying winget instead -- Windows may ask for admin approval.
call winget install -e --id OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
where npm >nul 2>nul
if errorlevel 1 goto manual

:npm
set "WHY=npm-failed"
echo     Installing it with npm  ^(npm install -g @openai/codex^) ...
rem `call`: npm and codex are .cmd shims, and a .cmd run without call never returns here.
call npm install -g @openai/codex
where codex >nul 2>nul
if errorlevel 1 goto manual
set "WHY="
echo [OK] codex CLI installed.
goto login

:found
echo [OK] codex CLI found.

:login
call codex login status >nul 2>nul
if not errorlevel 1 goto ready
if /i "%MODE%"=="--from-app" goto dologin
if /i "%MODE%"=="--from-update" if exist "%DECLINED%" goto notsigned
set "ANS=Y"
set /p ANS="Sign in to Codex now with 'codex login' (a browser opens)? [Y/n] "
if /i "%ANS:~0,1%"=="n" goto declinelogin
:dologin
set "WHY=login-failed"
call codex login
call codex login status >nul 2>nul
if not errorlevel 1 goto ready

:notsigned
echo [NOTE] The codex CLI is installed but not signed in. Before you switch the overlay to
echo        Codex, run   codex login   once -- or click Codex in the titlebar, which offers it.
set "RC=2"
goto end

:ready
set "WHY="
echo [OK] codex is signed in -- click Codex in the overlay's titlebar to use it.
if exist "%DECLINED%" del "%DECLINED%" >nul 2>nul
set "RC=0"
goto end

:declinelogin
set "WHY="
if /i "%MODE%"=="--from-update" call :remember
goto notsigned

:decline
set "WHY="
if /i "%MODE%"=="--from-update" call :remember
:manual
echo [NOTE] Codex CLI not installed. Claude mode works without it. For Codex mode later,
echo        click Codex in the overlay's titlebar ^(it offers to set it up^), or run:
echo          npm install -g @openai/codex    ^(needs Node.js 18+ from https://nodejs.org^)
echo          codex login
set "RC=3"
goto end

:skipped
echo [NOTE] Codex mode isn't set up ^(you skipped it before^). To set it up, click Codex in
echo        the overlay's titlebar, or run install-codex.cmd in the app folder.
set "RC=3"
goto end

:remember
if not exist "%STATEDIR%" mkdir "%STATEDIR%" >nul 2>nul
echo declined> "%DECLINED%"
exit /b 0

:record
rem "<mode> <exit code> <what failed>", read by the overlay at its next launch. Only setup's
rem and an update's runs: the app's button reads the exit code itself, and a double-click
rem pauses on the result. The redirection goes first so a trailing digit can't become a
rem handle number.
if /i not "%MODE%"=="--from-update" if /i not "%MODE%"=="--from-setup" exit /b 0
if not exist "%STATEDIR%" mkdir "%STATEDIR%" >nul 2>nul
> "%RESULT%" echo %MODE% %RC% %WHY%
exit /b 0

:end
rem Any finished setup clears an older failure record; a failed one writes a fresh one.
if "%RC%"=="0" if exist "%RESULT%" del "%RESULT%" >nul 2>nul
if defined WHY call :record
rem The app's Update button closes this window as soon as the update succeeds, and a failed
rem Codex setup doesn't fail the update -- so without this pause the reason printed above
rem vanished unread. Only when something was tried and failed: a "no" was the user's choice.
if not defined WHY goto endwait
if /i not "%MODE%"=="--from-update" goto endwait
if not defined OV_UPDATE_AUTO goto endwait
echo.
echo [NOTE] Codex setup didn't finish -- what went wrong is above. The update itself is done:
echo        press a key and the overlay restarts, where clicking Codex offers to try again.
pause
:endwait
rem The overlay's button opened this window and waits for it: keep the window up only when
rem it has something left to say. Double-clicked, it always waits: the window is all there
rem is. setup/update have their own endings.
if /i "%MODE%"=="--from-app" if not "%RC%"=="0" pause
if "%MODE%"=="" pause
endlocal & exit /b %RC%
