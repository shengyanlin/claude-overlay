# Windows guide

[← Back to the README](../README.md)

On a Mac? See the **[macOS guide](macos.md)**.

The short version: get the folder, double-click **`setup.cmd`**, then double-click
**`Start Claude Overlay.cmd`**. The rest of this page is the detail.

## Install

Pick whichever you like — all three end with the overlay ready to run.

### 🖱️ One double-click — `setup.cmd` (recommended)

Get the repo, then double-click **`setup.cmd`**. It checks Python,
**auto-installs the `claude` CLI if it's missing** (and offers to log you in), does the same
for the **`codex` CLI** (Codex mode; skipped without fuss if npm can't be had), and
installs the Python packages — so even a fresh machine is one double-click from ready.

```
git clone https://github.com/shengyanlin/claude-overlay.git
```
(or download the ZIP from the green **Code** button and unzip it.)

### ⚡ Let Claude install it (if you already have the CLI)

It's an agent — so it can set itself up. With the `claude` CLI already installed
(see [What you need](#1-claude-code-cli--installed-and-logged-in)), run this from
wherever you want it to live:

```
claude "Set up Claude Overlay for me: clone https://github.com/shengyanlin/claude-overlay, make sure Python 3.10+ is installed (install it if missing), ensure pip is present (python -m ensurepip --upgrade), then run python -m pip install -r requirements.txt, then launch it with pythonw. Tell me when it's running."
```

Claude will ask before each step.

### 🛠️ By hand

```
git clone https://github.com/shengyanlin/claude-overlay.git
cd claude-overlay
python -m ensurepip --upgrade          # only needed if pip is missing; harmless otherwise
python -m pip install -r requirements.txt
```

This installs only the Python packages (`claude-agent-sdk`, `pillow`, `keyboard`) —
you still need the `claude` CLI installed and logged in (see [What you need](#what-you-need)). Use
`python -m pip` (not a bare `pip`): it works even when Python's `Scripts\` folder
isn't on PATH, and `ensurepip` bootstraps `pip` if your Python install shipped without it.

---

## What you need

Windows 10 or 11, plus the three things below. **`setup.cmd`** takes care of all of them:
it installs whichever is missing, so you only need this section if you would rather
install them yourself.

### 1. Claude Code CLI — installed *and logged in*
The overlay has no brain of its own; it drives the `claude` command line.

**Easiest:** just run **`setup.cmd`** (below) — it installs the CLI for you with the
official native installer if you don't already have it. To install it yourself:

- **Native installer — recommended, no Node.js** (PowerShell):
  ```powershell
  irm https://claude.ai/install.ps1 | iex
  ```
  (or `winget install Anthropic.ClaudeCode`). It auto-updates itself.
- **npm** (needs Node.js 18+): `npm install -g @anthropic-ai/claude-code`

**Log in** with your own Claude account (Pro/Max subscription — no API key needed):
run `claude auth login` (in PowerShell or CMD — not Git Bash) and follow the browser prompt once.

**Verify** — this must print a version number:
```
claude --version
```
If it says "command not found", the CLI isn't installed / on PATH yet.

### 2. Python 3.10+
**Install** from <https://www.python.org/downloads/> and tick
**"Add python.exe to PATH"** in the installer.

**Verify:**
```
python --version
```

### 3. Codex CLI — for Codex mode (optional)
The titlebar's **Claude ⇄ Codex** switch puts OpenAI's Codex behind the same window, on
**your own** `codex` login. `setup.cmd` installs it (with npm, offering Node.js if that's
missing too — per-user, no admin, with winget as the fallback); by hand:
```
npm install -g @openai/codex
codex login
```
**Verify:** `codex --version` and `codex login status`. Claude mode doesn't need it.

---

## Run

1. Make sure `claude --version` works and you've logged in (`claude auth login`).
2. Start it (any of):
   - Double-click **`Start Claude Overlay.cmd`** — launches with **no console window**.
   - `pythonw claude_overlay.py` — no console.
   - `python claude_overlay.py` — keeps a console open for logs (good for debugging).
3. The window appears. Type and hit **Enter** — it auto-captures your screen each
   message, so you can ask about whatever's in front of you right away.
4. Not using it? Hit **–** to collapse it to a small floating orb, and click the orb
   to expand it again.

### Put it on your Desktop (optional)

Double-click **`Create Desktop Shortcut.cmd`** to drop a **Claude Overlay** shortcut
— with the orb icon — on your Desktop, so you can launch it like any other app.

> Don't just drag `Start Claude Overlay.cmd` to your Desktop — it's a portable
> launcher that must stay next to `claude_overlay.py`. The shortcut points back to it
> in place, which is why it keeps working.

## If it doesn't start

See **[troubleshooting](troubleshooting.md)**.
