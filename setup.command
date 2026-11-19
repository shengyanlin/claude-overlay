#!/bin/bash
# Claude Overlay - setup (macOS). Double-click it in Finder.
#
# The macOS counterpart of setup.cmd, step for step: find (or install) a Python that can
# run the app, make sure the `claude` CLI is there and signed in, offer Codex mode,
# install the Python packages, and build the launcher - so a fresh Mac is one
# double-click from ready. Safe to run again at any time; every step checks before it acts.

OV_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$OV_REPO/mac-common.sh"
cd "$OV_REPO" || exit 1
ov_path

ov_rule
echo "  Claude Overlay - setup"
ov_rule
echo

# ── 1. Python ──
if ov_python_ok "$OV_PY"; then
  echo "[OK] Python environment found: $OV_PY ($("$OV_PY" --version 2>&1))"
else
  BASE="$(ov_find_base_python)"
  if [ -z "$BASE" ]; then
    echo "[X] No Python that can run the overlay was found on this Mac."
    echo "    (It needs Python 3.10+ with a current Tk. The one macOS ships,"
    echo "    /usr/bin/python3, is too old on both counts.)"
    if ov_ask "Install Python $OV_PY_VERSION now, automatically? (recommended)" 0; then
      echo
      echo "Installing Python (into your user folder, no admin password needed) ..."
      ov_install_python
      echo
      echo "Re-checking for Python ..."
      BASE="$(ov_find_base_python)"
    fi
  fi
  if [ -z "$BASE" ]; then
    echo
    echo "[X] Python is not available yet."
    echo "    Install it from https://www.python.org/downloads/macos/ (its installer"
    echo "    includes Tk), then double-click setup.command again."
    ov_pause; exit 1
  fi
  echo "[OK] Python found: $BASE ($("$BASE" --version 2>&1))"
  echo "Creating the app's own environment in .venv ..."
  if ! ov_ensure_venv "$BASE"; then
    echo "[X] Could not create the environment in $OV_VENV (see above)."
    ov_pause; exit 1
  fi
fi

# ── 2. the claude CLI ──
if ! command -v claude >/dev/null 2>&1; then
  echo "[!] 'claude' CLI not found. Installing it with the official native installer"
  echo "    (no Node.js required)..."
  curl -fsSL https://claude.ai/install.sh | bash
  ov_path
  if ! command -v claude >/dev/null 2>&1; then
    echo "[X] Install didn't complete. Install manually, then re-run setup.command:"
    echo "      curl -fsSL https://claude.ai/install.sh | bash"
    echo "      or Homebrew:  brew install --cask claude-code"
    echo "      or npm:       npm install -g @anthropic-ai/claude-code   (needs Node 18+)"
    ov_pause; exit 1
  fi
  echo "[OK] claude CLI installed."
else
  echo "[OK] claude CLI found."
fi
echo
echo "If you haven't logged in yet, you need to do it once (a browser opens)."
if ov_ask "Log in now with 'claude auth login'?" 1; then
  claude auth login
fi

# ── 3. Codex mode (optional) ──
/bin/bash "$OV_REPO/install-codex.sh" --from-setup
echo

# ── 4. the Python packages ──
if [ ! -f "$OV_REPO/requirements.txt" ]; then
  echo "[X] requirements.txt is missing, so there is nothing to install from."
  echo "    Re-download the ZIP and unzip ALL of it over this folder."
  ov_pause; exit 1
fi
echo "Installing the Python packages listed in requirements.txt ..."
if ! ov_pip_install; then
  echo "[X] pip install failed. See the error above."
  ov_pause; exit 1
fi

# ── 5. the launcher ──
# Optional, like Codex and the Word/PowerPoint extras: Start Claude Overlay.command runs
# the app without the bundle. So a failed build is a note, not a failed setup - but the
# closing text must then not send the user to an app that isn't there.
echo
HAVE_APP=1
if ! /bin/bash "$OV_REPO/mac-app.sh" "$OV_REPO"; then
  HAVE_APP=""
  echo
  echo "[NOTE] The \"Claude Overlay.app\" launcher was not built (see above)."
  echo "    Everything else is fine: \"Start Claude Overlay.command\" starts the app"
  echo "    without it. To retry just this part:"
  echo "      /bin/bash \"$OV_REPO/mac-app.sh\""
fi

# ── 6. does it load? ──
echo
echo "Checking that the app can load..."
if ! "$OV_PY" "$OV_REPO/preflight.py" >/dev/null 2>&1; then
  echo
  echo "[X] Setup finished, but the app cannot start yet. The details:"
  echo
  "$OV_PY" "$OV_REPO/preflight.py"
  ov_pause; exit 1
fi
echo "[OK] The app loads."
echo
ov_rule
echo "  [OK] Setup complete."
if [ -n "$HAVE_APP" ]; then
  echo "  Start it: double-click \"Claude Overlay.app\" in this folder"
  echo "  (drag it to the Dock or to Applications if you like), or"
  echo "  \"Start Claude Overlay.command\"."
else
  echo "  Start it: double-click \"Start Claude Overlay.command\" in this"
  echo "  folder. (The app launcher was not built - see the [NOTE] above.)"
fi
echo
echo "  The first time, macOS asks to let it record the screen - that is"
echo "  how Claude sees what you're looking at. Allow it in System Settings"
echo "  > Privacy & Security > Screen & System Audio Recording, then start"
echo "  the overlay again."
ov_rule
if ov_ask "Start Claude Overlay now?" 1; then
  if [ -n "$HAVE_APP" ]; then
    open "$OV_REPO/Claude Overlay.app"
  else
    /bin/bash "$OV_REPO/Start Claude Overlay.command"
  fi
fi
exit 0
