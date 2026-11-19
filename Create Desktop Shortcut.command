#!/bin/bash
# Double-click to put "Claude Overlay" (with the Clawd icon) on your Desktop, so you can
# start it like any other app. It points back at this folder, which is why it keeps
# working across updates - don't move the folder afterwards (or just run this again).
OV_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$OV_REPO/mac-common.sh"
/bin/bash "$OV_REPO/mac-app.sh" "$HOME/Desktop"
RC=$?
echo
if [ $RC -ne 0 ]; then
  # This window is the only place the failure shows - say it, don't just close on it.
  echo "[X] The Desktop shortcut was NOT created (the reason is above)."
  echo "    The app itself is unaffected: start it from this folder with"
  echo "    \"Start Claude Overlay.command\"."
fi
ov_pause
exit $RC
