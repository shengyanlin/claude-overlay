#!/bin/bash
# Claude Overlay - diagnose (macOS). Double-click it in Finder.
#
# The same report as Diagnose.cmd: which Python runs the app, which packages it has,
# whether the app loads - printed here AND copied to the clipboard, ready to paste into a
# bug report.

OV_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$OV_REPO/mac-common.sh"
cd "$OV_REPO" || exit 1
ov_path

ov_rule
echo "  Claude Overlay - diagnose"
ov_rule
echo

PY=""
if [ -x "$OV_PY" ] && "$OV_PY" -c "pass" >/dev/null 2>&1; then
  PY="$OV_PY"                      # the environment the app really runs in, even if broken
else
  PY="$(ov_find_base_python)"
fi
if [ -z "$PY" ]; then
  echo "[X] Nothing on this Mac would run the overlay. That alone explains the failure."
  echo
  echo "    This is what the machine actually has:"
  for c in python3 python3.13 python3.12 python3.11 python3.10; do
    p="$(command -v "$c" 2>/dev/null)" && echo "      $c -> $p ($("$p" --version 2>&1))"
  done
  [ -e "$OV_VENV" ] && echo "      .venv exists but its Python doesn't run" \
                    || echo "      no .venv in the app folder (setup.command builds it)"
  echo
  echo "    The Python macOS ships (/usr/bin/python3) is too old for the overlay."
  echo "    Fix: double-click setup.command - it installs a current Python when one is"
  echo "    missing (into your user folder, no admin password)."
  echo
  ov_pause; exit 1
fi

REPORT="$(mktemp)"
"$PY" "$OV_REPO/preflight.py" 2>&1 | tee "$REPORT"
RC=${PIPESTATUS[0]}
if command -v pbcopy >/dev/null 2>&1 && pbcopy < "$REPORT" 2>/dev/null; then
  echo
  echo "(This report is now on your clipboard - paste it into a bug report.)"
fi
rm -f "$REPORT"
echo
ov_pause
exit "$RC"
