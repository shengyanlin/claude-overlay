#!/bin/bash
# Start Claude Overlay (macOS). Double-click it in Finder - or use "Claude Overlay.app",
# which setup.command builds and which starts it without this Terminal window.
#
# A portable launcher, like Start Claude Overlay.cmd: it must stay next to
# claude_overlay.py, and it always runs the environment in this folder's .venv - the one
# setup.command and update.command install into - so the app and its packages can never
# end up in two different Pythons.

OV_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$OV_REPO/mac-common.sh"
cd "$OV_REPO" || exit 1

if ! ov_python_ok "$OV_PY"; then
  echo "[X] Claude Overlay isn't set up in this folder yet (no working .venv)."
  echo "    Double-click setup.command - it installs what's missing, then starts it."
  ov_pause; exit 1
fi

# Detached from this window: its own session, no terminal attached, so closing the
# window (or Terminal itself) doesn't take the overlay down with it.
nohup "$OV_PY" "$OV_REPO/claude_overlay.py" >/dev/null 2>&1 </dev/null &
disown 2>/dev/null || true
echo "Claude Overlay is starting - you can close this window."
echo "(If nothing appears, double-click Diagnose.command for the reason.)"
exit 0
