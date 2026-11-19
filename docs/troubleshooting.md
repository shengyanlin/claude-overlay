# Troubleshooting

[← Back to the README](../README.md)

This page uses the Windows file names. On a Mac, use `Diagnose.command`, `setup.command`
and `update.command`; the crash log is `~/Library/Application Support/claude-overlay/crash.log`.

## It doesn't open / it vanishes

The overlay runs under `pythonw`, which has no console — so if it fails to start there
is nothing to read. It tells you anyway:

- **A dialog appears** naming what broke and the one command that fixes it.
- **The details are saved** to `%LOCALAPPDATA%\claude-overlay\crash.log`.
- **Double-click `Diagnose.cmd`** for a full report — which Python is running it, which
  packages are installed, whether the app loads — copied to your clipboard, ready to
  paste into a bug report. It's the fastest way to get help from someone who isn't at
  your machine.

The usual causes, all of which `Diagnose.cmd` names outright:

| What happened | Fix |
|---|---|
| A `pip install` was interrupted (flaky network, proxy) and left a package uninstalled | Re-run `setup.cmd` |
| Two Pythons — packages installed into the one that *isn't* launching the app | Use the exact `pip` command `Diagnose.cmd` prints |
| `claude-agent-sdk` older than the app | Run `update.cmd` — it installs the pinned version from `requirements.txt` |
| A ZIP "update" that replaced only `claude_overlay.py` | Unzip **all** files over the folder |

**On v1.15.1 and v1.15.2 specifically:** if double-clicking the launcher opens a console
window saying no Python was found — on a machine where the overlay used to work — that is
a launcher bug, not your install. v1.15.1 checked the wrong file; v1.15.2 only looked at
`PATH`, so it also walled machines whose Python is where `setup.cmd` puts it
(`%LOCALAPPDATA%\Programs\Python\`) without `PATH` ever catching up. Update to
**v1.15.3 or later** (`update.cmd`, or `git pull`). If you can't launch anything at all,
the fix needs no Python: re-download the latest ZIP and replace
`Start Claude Overlay.cmd`.

That screen now tells you which of two different problems you have. If every path it lists
sits under `\WindowsApps\` **and** it finds nothing off `PATH`, this PC genuinely has no
Python — those `\WindowsApps\` entries are Windows placeholders that only print
*"Python was not found…"* when run. Double-click **`setup.cmd`**: it installs Python for
you, per-user, no admin needed. (`update.cmd` offers to run it for you too, so on a machine
with no Python you can double-click either one and it gets sorted.)

## Locked-down work laptop? (`403`, or an installer that won't run)

If `setup.cmd` can't install Python and the reason it prints is an **HTTP 403** — or the
installer downloads and then silently refuses to run — that's your employer's proxy or
endpoint-security software, not a bug here. `setup.cmd` already tries three routes (winget →
python.org → [uv](https://github.com/astral-sh/uv), whose CPython build isn't blocked by the
signature rules that stop the others), and it prints what each one reported so you have
something specific to send IT.

When all three are refused, **[`offline/README.md`](../offline/README.md) has two routes that
need no working download at all** — pre-stage one `.zip` from any machine that *can* reach
GitHub, or just drop any Python 3.10+ folder into `%LOCALAPPDATA%\Programs\Python\` (every
script here *scans* that folder and uses whatever runs, no matter how it got there).
