#!/bin/bash
# Claude Overlay - update (macOS). Double-click it in Finder; the overlay's own Update
# button runs this same file.
#
# The macOS counterpart of update.cmd, with the same shape and the same exit codes:
# pull the latest release, refresh the Python packages, prove the new code still loads.
# Every way it can stop ends on the reason and a "press Return", because this window is
# the error UI - 0 is returned only when the update really landed.
#
# It runs in three steps, as update.cmd does:
#   (no argument)   stage a copy of this script in the temp folder and run THAT. bash reads
#                   a script as it goes, so pulling a new update.command over the one
#                   being executed would continue from the wrong byte of a different file.
#   --from-temp     (the staged copy) the git checks and the pull.
#   --finish        (the NEW update.command, after the pull) packages, launcher, Codex,
#                   and the check that the app loads - so a release can change this step
#                   and have its own version of it run.

pause_exit() { ov_pause; exit "${1:-1}"; }

if [ "$1" != "--from-temp" ] && [ "$1" != "--finish" ]; then
  OV_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  . "$OV_REPO/mac-common.sh"
  STAGE="$(mktemp -d)" || { echo "[X] Could not stage the updater in the temp folder."; pause_exit 1; }
  if ! cp "$OV_REPO/update.command" "$STAGE/update.command" \
     || ! cp "$OV_REPO/mac-common.sh" "$STAGE/mac-common.sh"; then
    echo "[X] Could not stage the updater in \"$STAGE\". Is the disk full?"
    pause_exit 1
  fi
  /bin/bash "$STAGE/update.command" --from-temp "$OV_REPO"
  RC=$?
  rm -rf "$STAGE"
  exit $RC
fi

if [ "$1" = "--from-temp" ]; then
  # Running from the staged copy: the helpers come from beside it, the app folder is $2.
  OV_REPO="$2"
  . "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/mac-common.sh"
  cd "$OV_REPO" || { echo "[X] Cannot enter \"$OV_REPO\"."; pause_exit 1; }
  ov_path

  ov_rule
  echo "  Claude Overlay - update"
  ov_rule
  echo

  if ! command -v git >/dev/null 2>&1 || ! git --version >/dev/null 2>&1; then
    echo "[X] git not found. You probably installed via the ZIP download."
    echo "    (If macOS just offered to install the command line developer tools, accept,"
    echo "    wait for it to finish, and run update.command again.)"
    echo "    Otherwise re-download the latest ZIP from the green \"Code\" button at"
    echo "      https://github.com/shengyanlin/claude-overlay"
    echo "    and unzip ALL of it over this folder, replacing every file."
    echo "    The app is a folder of modules, not a single script - replacing"
    echo "    only claude_overlay.py leaves it unable to start."
    pause_exit 1
  fi
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "[X] This folder isn't a git clone, so there's nothing to pull."
    echo "    Re-download the latest ZIP from"
    echo "      https://github.com/shengyanlin/claude-overlay"
    echo "    and unzip ALL of it over this folder, replacing every file."
    pause_exit 1
  fi
  # A fork pulls the release from upstream, not from its own (stale) origin.
  SRC="origin"
  git remote get-url upstream >/dev/null 2>&1 && SRC="upstream"
  BR="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
  if [ "$BR" != "main" ]; then
    echo "[X] This clone is on branch \"$BR\", not main."
    echo "    Releases live on main, so updating from here would merge the new release"
    echo "    into that branch's work. Switch first:"
    echo "        git switch main"
    echo "    then re-run update.command."
    pause_exit 1
  fi
  if ! git diff --quiet HEAD; then
    echo "[X] You have uncommitted changes here, and the pull would overwrite them."
    echo "    Commit them, or set them aside with:"
    echo "        git stash"
    echo "    then re-run update.command."
    pause_exit 1
  fi
  echo "Pulling the latest code from $SRC/main..."
  if ! git pull --ff-only "$SRC" main; then
    echo "[X] git pull failed (see above). If main here carries commits of its own,"
    echo "    it cannot fast-forward onto the release; move them to a branch first."
    pause_exit 1
  fi
  if [ ! -f "$OV_REPO/update.command" ]; then
    echo
    echo "[X] The code updated, but update.command is no longer in this folder, so the"
    echo "    packages were NOT refreshed. Double-click setup.command to finish."
    pause_exit 1
  fi
  /bin/bash "$OV_REPO/update.command" --finish
  exit $?
fi

# ── --finish: run by the update.command the pull just delivered ──
OV_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$OV_REPO/mac-common.sh"
cd "$OV_REPO" || exit 1
ov_path
echo

if ! ov_python_ok "$OV_PY"; then
  echo "[X] The code updated, but this folder has no working Python environment (.venv)"
  echo "    -- so the packages were NOT refreshed and the app cannot start yet."
  echo
  if [ -f "$OV_REPO/setup.command" ] && ov_ask "  Run setup.command now? It builds the environment." 1; then
    echo
    /bin/bash "$OV_REPO/setup.command"
    echo
    echo "Re-checking for Python ..."
  fi
  if ! ov_python_ok "$OV_PY"; then
    echo "  The code IS updated - only the packages were skipped, so the app will still"
    echo "  start once the environment exists."
    echo "  Fix: double-click setup.command. It installs Python when it is missing"
    echo "       (into your user folder, no admin password) and the packages in the same run."
    echo
    pause_exit 1
  fi
fi

if [ ! -f "$OV_REPO/requirements.txt" ]; then
  echo "[X] requirements.txt is missing, so there is nothing to install from."
  echo "    Re-download the ZIP and unzip ALL of it over this folder."
  pause_exit 1
fi
echo "Refreshing Python packages with $OV_PY ..."
if ! ov_pip_install; then
  echo
  echo "[X] Refreshing the packages FAILED (see the pip output above)."
  echo "    Your install may now be incomplete - do not skip this."
  echo "    Retry, or run it yourself:"
  echo "      \"$OV_PY\" -m pip install --upgrade -r requirements.txt"
  echo "    Then double-click Diagnose.command to confirm the app can load."
  pause_exit 1
fi

# Bring the launchers that exist (this folder's, and a Desktop one if the user made it)
# up to this release. Mostly that is nothing: mac-app.sh leaves a launcher alone unless
# it has to change, because macOS forgets the permissions of one that is rebuilt.
# Optional, as in setup.command: Start Claude Overlay.command runs the app without the
# bundle, so a failed build is a note, not a failed update - but it is said, with
# mac-app.sh's own reason above it. mac-app.sh swaps a new bundle in only once it is
# complete, which is why the note can say the launcher that was there is as it was.
rebuild_launcher() {      # $1 = the folder the bundle is in, $2 = that folder in words
  /bin/bash "$OV_REPO/mac-app.sh" "$1" && return 0
  echo
  echo "[NOTE] The \"Claude Overlay.app\" launcher $2 was not rebuilt (see above)."
  echo "    The one that was there, if any, was left as it was. Everything else is"
  echo "    fine: \"Start Claude Overlay.command\" starts the app without it. To retry"
  echo "    just this part:"
  echo "      /bin/bash \"$OV_REPO/mac-app.sh\" \"$1\""
}
echo
rebuild_launcher "$OV_REPO" "in this folder"
if [ -d "$HOME/Desktop/Claude Overlay.app" ]; then
  rebuild_launcher "$HOME/Desktop" "on the Desktop"
fi

/bin/bash "$OV_REPO/install-codex.sh" --from-update

echo
echo "Checking that the updated app can load..."
if ! "$OV_PY" "$OV_REPO/preflight.py" >/dev/null 2>&1; then
  echo
  echo "[X] The update left this install unable to start."
  echo "    Run Diagnose.command for the details (and what fixes it)."
  echo
  "$OV_PY" "$OV_REPO/preflight.py"
  pause_exit 1
fi
echo "[OK] The app loads."
echo
if [ -n "$OV_UPDATE_AUTO" ]; then
  # Started by the overlay's Update button: it is waiting on this exit code to restart
  # itself, so the success path must not sit at a prompt nobody knows to answer.
  ov_rule
  echo "  [OK] Updated. The overlay restarts itself now."
  ov_rule
  exit 0
fi
ov_rule
echo "  [OK] Updated. IMPORTANT: quit the running overlay and"
if [ -x "$OV_REPO/Claude Overlay.app/Contents/MacOS/ClaudeOverlay" ]; then
  echo "  re-open it (\"Claude Overlay.app\") for the changes"
  echo "  to take effect - it does not reload while running."
else                      # no launcher in this folder (the [NOTE] above): don't name one
  echo "  re-open it (\"Start Claude Overlay.command\") for the"
  echo "  changes to take effect - it does not reload while running."
fi
ov_rule
ov_pause
exit 0
