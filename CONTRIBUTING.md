# Contributing to Claude Overlay

Thanks for taking a look! This is a small, single-file app, so contributing is easy.

## Ways to help

- **Report bugs** — open an issue with your Windows version, Python version
  (`python --version`), and `claude --version`, plus what you did and what happened.
- **Suggest features** — open an issue describing the use case.
- **Cross-platform support** — the biggest open need. The app is Windows-only today
  because of Win32 calls for DPI awareness, rounded/region windows, and per-monitor
  capture (see `enumerate_monitors`, `_apply_region`, `set_dpi_awareness`). A macOS or
  Linux capture + windowing backend would be hugely welcome.

## Project layout

Everything lives in **`claude_overlay.py`** — configuration constants are grouped at
the top of the file, and the UI is one `Overlay` class plus a background
`ClaudeWorker` thread that drives the `claude` CLI via `claude-agent-sdk`.

## Dev setup

```
git clone https://github.com/shengyanlin/claude-overlay.git
cd claude-overlay
pip install -r requirements.txt
python claude_overlay.py        # run with a console so you see logs/errors
```

You'll need the `claude` CLI installed and logged in — see the README's Prerequisites.

## Pull requests

- Keep it dependency-light (currently just `claude-agent-sdk`, `pillow`, `keyboard`).
- Match the existing style: terse comments that explain *why*, not *what*.
- Don't commit machine-specific or personal data. Local-only scratch files use the
  `_*` prefix and are gitignored; please keep generators/experiments under that.
- If you touch the screen-capture or agent-permission behavior, call it out in the PR
  description — those affect user privacy and safety.

By contributing, you agree your contributions are licensed under the MIT License.
