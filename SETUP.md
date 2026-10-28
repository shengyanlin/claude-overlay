# Claude Overlay — setup for a new machine

A floating, always-on-top chat window for **Claude Code** that can **see your
screen**. It drives *your own* `claude` CLI login, so **each person uses their
own Claude subscription — no API key, no shared cost, nothing billed to anyone
else.**

> 每個人都是用自己電腦上、自己登入的 `claude` CLI（自己的訂閱），不會用到別人的額度，也不用 API key。

## Prerequisites (per person, one time)

1. **Claude Code CLI** — installed and logged in with your own subscription.

   **Install** (native installer, no Node.js needed) — run in **PowerShell**:
   ```powershell
   irm https://claude.ai/install.ps1 | iex
   ```
   (or `winget install Anthropic.ClaudeCode`; or, if you prefer npm and have Node 18+,
   `npm install -g @anthropic-ai/claude-code`.)

   **Log in** — run in **PowerShell or CMD (not Git Bash / MINGW)**:
   ```
   claude auth login
   ```
   A browser opens once; sign in with your own Claude account (Pro/Max — no API key).
   > ⚠️ Don't log in from **Git Bash / MINGW** — the sign-in screen renders blank there and
   > looks frozen (nothing happens when you press a key). Use **PowerShell** or **CMD**.

   **Verify** — both should succeed:
   ```
   claude --version
   claude auth status
   ```
   > If `claude` says "command not found", **close and reopen your terminal** — the native
   > installer drops it in `%USERPROFILE%\.local\bin` and a fresh terminal picks up the PATH.
2. **Python 3.10+** — https://www.python.org/downloads/
   (In the installer, tick **"Add python.exe to PATH"**.)
3. **Codex CLI** — for the titlebar's **Claude ⇄ Codex** switch (Codex mode drives your own
   `codex` login the same way Claude mode drives `claude`). `setup.cmd` installs it for you
   (so does `update.cmd` on an install that predates it, and the overlay's **Install Codex
   CLI & sign in** button); by hand it is:
   ```
   npm install -g @openai/codex      (needs Node.js 18+ — https://nodejs.org)
   codex login
   ```
   Verify with `codex --version` and `codex login status`. Claude mode works without it.

## Install

1. Copy this whole `claude-overlay` folder to the new machine (anywhere).
2. Double-click **`setup.cmd`** — it checks Python + the `claude` CLI (auto-installing the
   CLI if it's missing), then the `codex` CLI (installed with npm; if Node.js is missing too
   it offers to install it with winget, which may ask for admin approval — say no and Codex
   is simply skipped), offers both sign-ins, and installs the Python packages
   (`claude-agent-sdk`, `pillow`, `keyboard`, …).
   > pip may print a few `… is installed in … which is not on PATH` warnings here —
   > **they're harmless**; the overlay doesn't use those scripts.
3. Make sure you've already logged in (`claude auth login`, step 1 above — and
   `codex login` for Codex mode).

## Run

- Double-click **`Start Claude Overlay.cmd`** (finds `pythonw`/`pyw` automatically,
  launches with no console window — it may flash a cmd box for a split second).
- **No-flash option:** make a shortcut to your `pythonw.exe` with the argument
  `"<this folder>\claude_overlay.py"` (that's what `Claude Overlay.lnk` is — it is
  machine-specific, so recreate it rather than copying it).
- Global hotkey to show/hide from anywhere: **Ctrl+Alt+Space**.

## Heads-up: permission mode

By default `PERMISSION_MODE = "bypassPermissions"` (top of `claude_overlay.py`):
a **fully autonomous agent** that can edit files and run commands in your home
folder **without asking**, and it can see your screen. If a colleague isn't
comfortable with that, change it to:
- `acceptEdits` — runs, but asks before edits, or
- `default` — asks before most actions, or
- `plan` — read-only.

**Codex mode** asks for the same full access (`CODEX_SANDBOX = "danger-full-access"`:
no sandbox, no prompts) — but only where the machine allows it. A managed machine's Codex
policy can forbid that; the overlay then runs the strongest sandbox the policy permits
(and, if Windows' `workspace-write` sandbox can't start on that PC, read-only), says so in
the chat, and shows an **Allow / Deny** card whenever Codex wants to step outside it.
Read-only always means Codex's `read-only` sandbox.

## Notes / fallbacks

- Fonts default to **Noto Sans/Serif TC**; if they aren't installed it falls back
  to Segoe UI / Georgia automatically (no action needed).
- The global hotkey uses the `keyboard` package; if it can't register, the app
  still runs (you just lose the hotkey) and prints a note.
- Working folder, model, theme, etc. are all constants at the top of
  `claude_overlay.py` — see `README.md` for the full list.
