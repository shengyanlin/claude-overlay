# Contributing to Claude Overlay

Thanks for taking a look! This is a small, single-file app, so contributing is easy.

## Ways to help

- **Report bugs** — open an issue with your Windows or macOS version, Python version
  (`python --version`), and `claude --version`, plus what you did and what happened.
- **Suggest features** — open an issue describing the use case.
- **Linux support** — the biggest open need. Windows and macOS each have a platform
  layer (`win32utils.py`, `macutils.py`) that answers the same small set of calls — a
  window region, capture exclusion, monitor enumeration, the foreground window, a global
  hotkey — and the UI imports whichever matches. A third one for Linux (X11 / Wayland)
  would be hugely welcome; `macutils.py`'s module docstring lists what it has to provide.

## Project layout

The UI is one `Overlay` class in **`claude_overlay.py`**, plus a background
`ClaudeWorker` thread (`worker.py`) that drives the `claude` CLI via `claude-agent-sdk`;
configuration constants live in `config.py`. Anything that talks to the operating system
goes through the platform layer — `win32utils.py` on Windows, `macutils.py` on macOS —
so a change to window or capture behaviour usually needs both, and the test suite runs
on both in CI.

## Dev setup

```
git clone https://github.com/shengyanlin/claude-overlay.git
cd claude-overlay
pip install -r requirements.txt
python claude_overlay.py        # run with a console so you see logs/errors
```

On macOS use a Python 3.10+ with a current Tk (not `/usr/bin/python3`) — easiest is
`./setup.command`, which builds `.venv` for you:

```
.venv/bin/python claude_overlay.py
```

You'll need the `claude` CLI installed and logged in — see the
[Windows guide](docs/windows.md#what-you-need) or the [macOS guide](docs/macos.md).

## Pull requests

- Keep it dependency-light (currently just `claude-agent-sdk`, `pillow`, `keyboard`).
- Match the existing style: terse comments that explain *why*, not *what*.
- Don't commit machine-specific or personal data. Local-only scratch files use the
  `_*` prefix and are gitignored; please keep generators/experiments under that.
- If you touch the screen-capture or agent-permission behavior, call it out in the PR
  description — those affect user privacy and safety.

By contributing, you agree your contributions are licensed under the MIT License.
