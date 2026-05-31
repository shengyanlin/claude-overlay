# Claude Overlay — setup for a new machine

A floating, always-on-top chat window for **Claude Code** that can **see your
screen**. It drives *your own* `claude` CLI login, so **each person uses their
own Claude subscription — no API key, no shared cost, nothing billed to anyone
else.**

> 每個人都是用自己電腦上、自己登入的 `claude` CLI（自己的訂閱），不會用到別人的額度，也不用 API key。

## Prerequisites (per person, one time)

1. **Claude Code CLI**, logged in with your own subscription
   ```
   npm install -g @anthropic-ai/claude-code
   claude            # then run /login  (use your Claude account)
   ```
   (Needs Node.js. If `npm` is missing, install Node LTS from nodejs.org first.)
2. **Python 3.10+** — https://www.python.org/downloads/
   (In the installer, tick **"Add python.exe to PATH"**.)

## Install

1. Copy this whole `claude-overlay` folder to the new machine (anywhere).
2. Double-click **`setup.cmd`** — it checks Python + the `claude` CLI and
   installs the three Python packages (`claude-agent-sdk`, `pillow`, `keyboard`).
3. Make sure you've already run `claude` once and logged in (step 1 above).

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

## Notes / fallbacks

- Fonts default to **Noto Sans/Serif TC**; if they aren't installed it falls back
  to Segoe UI / Georgia automatically (no action needed).
- The global hotkey uses the `keyboard` package; if it can't register, the app
  still runs (you just lose the hotkey) and prints a note.
- Working folder, model, theme, etc. are all constants at the top of
  `claude_overlay.py` — see `README.md` for the full list.
