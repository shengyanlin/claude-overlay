# macOS guide

[← Back to the README](../README.md)

The same overlay, feature for feature — the window, the screenshots, Codex mode, the
collapsed Clawd, updates. It is the same code: only the layer that talks to the operating
system differs (`macutils.py` on a Mac, `win32utils.py` on Windows).

## Install

You need the same things as on Windows — the `claude` CLI, logged in, and Python 3.10+ —
and **`setup.command`** gets all of them for you:

```
git clone https://github.com/shengyanlin/claude-overlay.git
```

then double-click **`setup.command`** in the folder. It:

- builds the app's own Python environment in `.venv`. The Python that comes with macOS
  (`/usr/bin/python3`) is too old to run the overlay, so if no suitable one is installed
  it offers to install Python into your user folder — no admin password;
- installs the `claude` CLI if it's missing (`curl -fsSL https://claude.ai/install.sh | bash`)
  and offers to log you in;
- offers Codex mode (the `codex` CLI), exactly as on Windows;
- installs the Python packages and builds **`Claude Overlay.app`** in the folder.

Start it by double-clicking **`Claude Overlay.app`** (drag it to the Dock or to
Applications if you like — it points back at this folder, so leave the folder where it
is), or **`Start Claude Overlay.command`**. **`Create Desktop Shortcut.command`** puts a
copy of the app on your Desktop.

> Downloaded the ZIP instead of cloning? macOS marks downloaded scripts as untrusted: the
> first time, right-click `setup.command` → **Open** → **Open**, rather than
> double-clicking it.

## The two permissions macOS asks for

- **Screen Recording** — how Claude sees your screen. The first launch says so in the
  chat and macOS shows its prompt; allow **Claude Overlay** (or **Terminal**, if you
  started it from a `.command` file) in **System Settings › Privacy & Security › Screen &
  System Audio Recording**, then start the overlay again. Until then no screenshot is
  sent at all: without the permission macOS hands back a picture of your wallpaper, and a
  wrong picture is worse than none.
- **Automation** — only if you ask Claude to *change* something in an app you have open
  (fix a slide, fill a cell). macOS asks once per app the first time; say yes there, or
  later in **Privacy & Security › Automation**.

macOS remembers these for the exact `Claude Overlay.app` it granted them to. Updates leave
that launcher untouched unless it has to change (it only points at this folder; the app's
code is not inside it), so the permissions carry over. Moving the folder, or deleting and
rebuilding the launcher, makes it a new app to macOS, and it asks once more.

## What's different on a Mac

| | Windows | macOS |
|---|---|---|
| Show / hide hotkey (`HOTKEY`) | `Ctrl+Alt+Space` | `⌥⇧Space` — macOS keeps `⌃⌥Space` for switching input sources |
| Bring it back when it's behind | the taskbar button | the Clawd icon in the **menu bar** (`TASKBAR_BUTTON` turns it off) |
| Dock / taskbar | a taskbar button | no Dock icon: it runs as a menu-bar app, which is what lets it float over **full-screen** apps and follow you across Spaces |
| Zoom, paste, copy | `Ctrl` `+`/`−`/`0`, `Ctrl+V`, `Ctrl+C` | `⌘` `+`/`−`/`0`, `⌘V`, `⌘C` (the `Ctrl` ones work too) |
| Quit | **✕** | **✕** or `⌘Q` |
| Scroll | mouse wheel | mouse wheel or two fingers on the trackpad |
| Hidden from screen shares (**Shareable** off) | guaranteed by Windows (display affinity) | **best effort**: the window is marked non-shareable, which current macOS honours (checked on macOS 26) but Apple doesn't guarantee. The overlay tests it on first use and tells you if your Mac ignores it — check your share's preview before relying on it. Claude's own screenshots leave the overlay out either way |
| Editing the app you have open | PowerShell + COM | AppleScript (`osascript`) |
| `config.json`, `state.json`, `crash.log` | `%LOCALAPPDATA%\claude-overlay\` | `~/Library/Application Support/claude-overlay/` |
| Setup · update · diagnose | `setup.cmd` · `update.cmd` · `Diagnose.cmd` | `setup.command` · `update.command` · `Diagnose.command` |
| Set up Codex by hand | `install-codex.cmd` | `install-codex.sh` |
| The stored login the allowance ring reads | `~/.claude/.credentials.json` | the same record in your login keychain, where the CLI keeps it on a Mac |

Everything else — the [controls](controls.md), the [`config.json` settings](configuration.md),
[updating](updating.md), the [security note](security-note.md) — applies to both. Tested on macOS 26 (Apple silicon);
it needs macOS 11 or later.
