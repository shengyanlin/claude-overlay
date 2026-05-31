# Claude Overlay

A frameless, **always-on-top floating chat window** for [Claude Code](https://docs.claude.com/en/docs/claude-code)
that can **see your screen**. Ask Claude about whatever you're looking at without
switching apps — it captures your monitors, reads them, and answers in a small
window that floats over everything.

> Built with Tkinter + the Claude Agent SDK. It drives **your own** `claude` CLI
> login, so it uses **your existing Claude subscription — no API key, no extra cost.**

```
 ┌──────────────────────────────────────────┐
 │ ✦ Claude                          —    ✕  │  ← drag to move
 ├──────────────────────────────────────────┤
 │                          what's this error? │  ← you (right)
 │  ✦ Claude                                   │
 │     ▤ Read  screen_1.png                    │  ← it read your screen
 │  The stack trace points to a null ref on …  │  ← Claude (left)
 ├──────────────────────────────────────────┤
 │  Reply to Claude…                      ↑   │
 │  ◉ auto-screenshot   Snap   Clear        ◢ │
 │  claude-opus-4-8[1m] ▾   ·   context 3%    │
 └──────────────────────────────────────────┘
```

## How it works

```
Overlay (Tkinter UI)  →  claude-agent-sdk  →  spawns the `claude` CLI  →  Anthropic
        ▲ screenshots (Pillow ImageGrab, one image per monitor)
```

- **UI** — Tkinter (ships with Python; no extra GUI runtime).
- **Brain** — `claude-agent-sdk` spawns your installed `claude` CLI as a subprocess
  and talks to it. It is **not** a direct API client, so the CLI is required.
- **Eyes** — Pillow `ImageGrab` snapshots **each monitor separately**; the prompt
  labels which is the **primary** vs **secondary** screen, and Claude reads each
  with its `Read` tool. The window hides itself during capture.

---

## Prerequisites

You need three things. Each has a one-time install below.

### 1. Windows 10 / 11
The app uses Win32 APIs (DPI awareness, rounded corners, multi-monitor capture),
so it currently runs **on Windows only**.

### 2. Claude Code CLI — installed *and logged in*
The overlay has no brain of its own; it drives the `claude` command line.

**Install** (pick one):
- **npm** (needs [Node.js](https://nodejs.org/) 18+):
  ```
  npm install -g @anthropic-ai/claude-code
  ```
- **native installer** (no Node required): see the
  [Claude Code install docs](https://docs.claude.com/en/docs/claude-code/setup).

**Log in** with your own Claude account (Pro/Max subscription — no API key needed):
```
claude
```
then run `/login` inside it once.

**Verify** — this must print a version number:
```
claude --version
```
If it says "command not found", the CLI isn't installed / on PATH yet.

### 3. Python 3.10+
**Install** from <https://www.python.org/downloads/> and tick
**"Add python.exe to PATH"** in the installer.

**Verify:**
```
python --version
```

---

## Install

```
git clone https://github.com/shengyanlin/claude-overlay.git
cd claude-overlay
pip install -r requirements.txt
```

On Windows you can instead just double-click **`setup.cmd`**, which checks Python
and the `claude` CLI and installs the packages for you.

Dependencies: `claude-agent-sdk`, `pillow`, `keyboard`.

---

## Run

1. Make sure `claude --version` works and you've logged in (`claude` → `/login`).
2. Start it (any of):
   - Double-click **`Start Claude Overlay.cmd`** — launches with **no console window**.
   - `pythonw claude_overlay.py` — no console.
   - `python claude_overlay.py` — keeps a console open for logs (good for debugging).
3. Press **Ctrl+Alt+Space** anytime to show / hide the window.

---

## Controls

| Action | How |
|---|---|
| Send message | `Enter` (or click the **↑** button) |
| New line | `Shift+Enter` |
| Stop a running reply | click **Stop** (the ↑ becomes ■ while busy) |
| Attach screen to next msg | **Snap** |
| Paste an image | **Ctrl+V** (click **📎** to clear) |
| Toggle auto-screenshot | **◉ / ○ auto-screenshot** (orange = on) |
| Switch model | click the **statusline** (`model ▾`) |
| Zoom text in / out | **Ctrl +** / **Ctrl −** (or **Ctrl + mouse-wheel**); **Ctrl 0** resets |
| New conversation | **Clear** |
| Collapse to a Claude orb | **–**, or double-click the title bar |
| Expand from the orb | click the orb (drag it to move) |
| Quit | **✕** |
| Show / hide from anywhere | **Ctrl+Alt+Space** (global hotkey) |
| Move | drag the title bar |
| Resize | drag **any edge or corner** (or the **◢** grip) |

---

## Configuration

All settings are constants at the top of `claude_overlay.py`:

- `MODEL` — pinned to `"claude-opus-4-8[1m]"` (Opus 4.8, **1M context**). The `[1m]`
  suffix selects the 1M variant; drop it for the standard 200K. Don't use `None`:
  the Agent SDK resolves `None` to an older model, not the CLI's interactive default.
- `PERMISSION_MODE` — `"bypassPermissions"` by default (see security note below).
  Use `"acceptEdits"`, `"default"`, or `"plan"` to add confirmation / read-only.
- `WORKING_DIR` — folder Claude operates in (default: your home directory).
- `THEME` — `"light"` (warm paper) or `"dark"`.
- `HOTKEY` — global show/hide hotkey (default `ctrl+alt+space`).
- `AUTO_SCREENSHOT_DEFAULT`, `FONT_SANS/SERIF/MONO`, `CORNER_RADIUS`, `ORB_SIZE`,
  `HIDE_SCREENSHOT_TOOL`, `WINDOW_ALPHA` — see inline comments.

## ⚠️ Security note

The default `PERMISSION_MODE = "bypassPermissions"` makes this a **fully
autonomous agent**: Claude can edit files and run commands in `WORKING_DIR`
**without asking**, and it can see your screen. If you don't want that, set
`PERMISSION_MODE` to `"acceptEdits"` (asks before edits), `"default"` (asks before
most actions), or `"plan"` (read-only) before running.

## License

[MIT](LICENSE) © shengyanlin
