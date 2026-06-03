# Changelog

All notable changes to Claude Overlay are documented here.
This project follows [Semantic Versioning](https://semver.org/).

## [1.1.4] — 2026-06-03

### Fixed
Residual crash/hang/freeze hardening from a second, independent adversarial audit (none
of these overlap the v1.1.1–v1.1.3 fixes):
- **A fast reply can no longer freeze the window.** The UI event pump used to drain its
  whole queue in one go, so a rapid stream could monopolize the main thread for seconds
  (no repaint, no Stop, no hotkey). The drain is now time-sliced (~12 ms budget) and
  adjacent text deltas are coalesced into a single insert.
- **A wedged connection can't hang the app forever.** A hang isn't an exception, so the
  reconnect/restart guards (which only fire on a *raised* error) couldn't reach an SDK
  call that never returns. `connect`, `query`, `disconnect`, and the response stream now
  each have a hard timeout; a timeout is treated as a dead transport and triggers a clean
  reconnect instead of a permanent "thinking…".
- **A pasted file that isn't a real image is no longer inlined.** When normalizing a
  pasted image failed, the original path was still attached — so a multi-GB file with a
  `.png` name could be read whole into memory. Failures are now skipped (with a notice),
  and `_build_query` caps the per-image byte size before reading.
- **Pasting and pre-capture no longer block the UI thread.** Opening/decoding/downscaling a
  pasted image, and the type-ahead screen pre-capture, now run on a background thread and
  post their result back — a slow/remote/cloud-placeholder file or a wedged display stack
  can't freeze typing.
- **The transcript is now bounded.** A very long session used to keep growing one Tk text
  widget and an embedded canvas per message; the oldest content is now pruned so layout
  stays fast and the embedded canvases are freed.
- **Switching model is serialized** through the worker queue, so it can't interleave with a
  reset/disconnect tearing down the same client.
- Minor: screenshot pruning is fully best-effort (a concurrent deleter can't make it throw
  out of capture/paste), and a failed window-region call frees its GDI region instead of
  leaking it.

## [1.1.3] — 2026-06-03

### Fixed
- **Pasting a long unbroken string no longer freezes the UI.** A whitespace-free blob
  (URL / base64 / minified JSON / hash) sent as a message hit Tk's ~O(n²) canvas
  word-wrap in the chat bubble — a 1 MB paste froze the window for 25–75 s. The bubble
  echo is now length-capped and long runs are broken (1 MB → 0.07 s).
- **`asyncio.CancelledError` no longer kills the worker.** It's a `BaseException`, so it
  slipped past every `except Exception` (in the turn loop, `_amain`, `run`, `_open`) —
  a cancelled receive (Stop / transport teardown) permanently zombied the worker. The
  turn loop, reconnect, and bounded restart now handle `BaseException`/`CancelledError`.
- **Malformed CLI stream frames can't abort a turn.** `_dispatch` is hardened against a
  corrupted block table, an unhashable block index, and `content=None`, and skips a bad
  frame instead of raising (which previously also skipped the reconnect path).
- **Pasted images are now downscaled** to the same long-edge cap as screenshots, so a
  pasted 4K/8K image (hundreds of MB of base64) can't overflow the stream buffer.
- **Old `claude-agent-sdk` installs load with a clear message.** Option kwargs the
  installed SDK doesn't support are stripped one-by-one (not just `max_buffer_size`), and
  an SDK-too-old failure now says to upgrade instead of "CLI not installed".
- Minor hardening: 0-byte images are skipped, `None` chat text is coerced, the update
  check tolerates absurd version strings, and a malformed theme colour degrades to grey.

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

[1.1.4]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.1.4
[1.1.3]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.1.3
[1.1.2]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.1.2
[1.1.1]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.1.1
[1.1.0]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.1.0
[1.0.0]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.0.0
