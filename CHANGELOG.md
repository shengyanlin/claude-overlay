# Changelog

All notable changes to Claude Overlay are documented here.
This project follows [Semantic Versioning](https://semver.org/).

## [1.1.2] — 2026-06-03

### Fixed
- **No more hard crashes / frozen windows** — hardened the app so a single hiccup can't
  take it down:
  - **Stream buffer raised from the SDK default 1 MB to 64 MB** (`max_buffer_size`).
    Inline screenshots (base64, one per monitor) routinely pushed a single stream line
    past 1 MB, which raised `CLIJSONDecodeError` and killed the worker — the most common
    crash. (Passed only if the installed SDK supports it, so older installs still load.)
  - **The UI event pump now survives any rendering error** and always reschedules itself.
    A stray exception used to skip the next tick and permanently freeze the window —
    still drawn, but never responding again.
  - **The worker auto-reconnects on a dead transport** (decode / connection / process
    errors) with a fresh session instead of erroring forever, and the worker thread
    **auto-restarts (bounded)** instead of exiting for good.
  - A failed initial connection is retried on the next message.

## [1.1.1] — 2026-06-03

### Added
- **In-app version + update check** — the status line shows the running version (e.g.
  `v1.1.1`), and on startup the overlay checks GitHub for a newer tag in the background.
  When one exists you get a 🔔 note and a `⬆` next to the version. Best-effort and silent
  on failure (offline / corporate TLS interception), so it never nags or blocks.
- **`update.cmd`** — one double-click updater: `git pull` + refresh the Python packages,
  with a clear "restart the overlay" reminder (and ZIP-install fallback guidance).

### Fixed
- **Works when bypass-permissions mode is disabled by policy** — a `can_use_tool`
  callback auto-approves tools, so the overlay no longer hangs forever on the first tool
  call in managed/enterprise installs (a GUI has no TTY to answer the permission prompt).
- **Screenshots no longer include the overlay itself** — the window is excluded from
  screen capture at the OS level (`WDA_EXCLUDEFROMCAPTURE`), so it can't obscure the
  content you send Claude. This also removes the `withdraw()` + 150 ms sleep on every
  capture (no flicker, no UI freeze); falls back to the old hide path if exclusion isn't
  available.
- **Clear** interrupts the in-flight turn before resetting, so the tail of the old reply
  no longer streams into the just-cleared chat.
- **Hotkey** now raises + focuses a visible-but-unfocused window instead of hiding it
  (the old toggle made the hotkey feel like it "couldn't summon" the app).
- Turns that end in error (`ResultMessage`) are surfaced instead of dropped; switching
  model reports when not connected and clears the stuck "switching model…" status.

### Changed
- The global hotkey is no longer advertised in the first-run greeting (it still works).

## [1.1.0] — 2026-06-01

### Added
- **Inline screenshots** — the screen is attached directly as image blocks, skipping
  the per-turn `Read` round-trip for noticeably faster replies. (`IMAGE_INPUT="read"`
  keeps the legacy save-PNG-and-Read path as a fallback.)
- **Pre-capture while typing** — a fresh frame is grabbed off the send path (debounced)
  and reused at send time, so sending no longer waits on a screenshot.
- **Glossy 3-D orb** — the collapsed bubble is now a rendered terracotta sphere
  (directional gradient, specular highlight, beveled rim, Claude spark), crisp at any
  DPI. Ships as `claude_overlay.ico` for the app/desktop icon.
- **Desktop shortcut creator** — `Create Desktop Shortcut.cmd` drops a launcher (with
  the orb icon) on your Desktop.

### Changed
- Default model is now standard 200K-context **Opus 4.8**; the 1M-context variant is
  one click away in the model switcher.
- Screenshots are downscaled to a 1568px long edge before sending (smaller upload,
  fewer vision tokens; Claude downsamples larger images anyway).
- The static system-prompt prefix is kept byte-stable (`exclude_dynamic_sections`) so
  prompt-cache hits survive across turns; context-usage % now updates off the critical
  path so the UI leaves "thinking…" the instant a reply ends.

### Fixed
- Quitting now lets the agent disconnect cleanly (bounded wait) instead of hard-killing
  a possibly mid-write turn.
- Screen-capture and image-attach failures now surface in-chat instead of silently
  sending no image.
- Switching model mid-stream is blocked (it was undefined against the SDK).
- The anyio "no console window" patch degrades gracefully if anyio changes.

## [1.0.0] — 2026-05-31

Initial public release.

- Frameless, always-on-top floating chat window for **Claude Code** on Windows.
- Screen vision: captures each monitor separately and labels primary vs. secondary.
- Drives your own `claude` CLI via `claude-agent-sdk` — uses your existing
  subscription, no API key.
- Live token streaming, tool-call chips, in-place model switcher, context-usage meter.
- Claude-desktop-style warm paper theme, DPI-aware, collapsible to a draggable orb,
  edge/corner resize, paste images (Ctrl+V), text zoom (Ctrl +/−), global hotkey
  (Ctrl+Alt+Space).

[1.1.2]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.1.2
[1.1.1]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.1.1
[1.1.0]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.1.0
[1.0.0]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.0.0
