# Changelog

All notable changes to Claude Overlay are documented here.
This project follows [Semantic Versioning](https://semver.org/).

## [1.2.3] — 2026-06-06

### Changed
- **`update.cmd` now refreshes your Desktop shortcut's icon** (only if you already have one).
  The shortcut is a machine-specific `.lnk` that `git pull` can't touch, so after an update an
  existing shortcut kept showing the old icon; `update.cmd` now re-points it at the current icon
  automatically. (Updated by hand with `git pull`? Re-run `Create Desktop Shortcut.cmd` once.)

## [1.2.2] — 2026-06-06

### Changed
- **Docs & setup: clearer, consistent install + login.** `SETUP.md`, `README.md`, and
  `setup.cmd` now lead with the native installer (no Node.js) and log in with
  `claude auth login` run in **PowerShell or CMD** — with an explicit warning that the
  sign-in screen renders blank in **Git Bash / MINGW** (which made it look frozen). Also
  noted that pip's "not on PATH" warnings are harmless and that reopening the terminal picks
  up the freshly-installed CLI.

## [1.2.1] — 2026-06-06

### Changed
- The bundled desktop-shortcut icon now uses the **Clawd sprite** (matching the v1.2.0
  default orb look) instead of the old glossy sphere. Added `claude_overlay_2.ico`
  (multi-resolution, generated from the sprite); `create-shortcut.ps1` points at it.

## [1.2.0] — 2026-06-05

### Added
- **Custom collapsed-orb artwork (`ORB_IMAGE`).** The collapsed orb can now render an
  image instead of the procedural glossy sphere. Point `ORB_IMAGE` at a PNG/ICO (relative
  to the script or absolute) and it's auto-scaled + centred so the whole opaque shape fits.
  Leave it `""` for the original sphere. Ships with a pixel-art "Clawd" sprite as the new
  default look.
- **Free-floating sprite mode (`ORB_FLOAT`).** With artwork set, the collapsed window is
  clipped to the *artwork's own silhouette* (built from its alpha via `CreateRectRgn`/
  `CombineRgn`) rather than a circle — so the orb floats as the raw sprite and clicks
  outside the shape pass through. Binary edges keep pixel art crisp; the expanded window
  keeps its rounded corners (no layered-window/colour-key tricks). `ORB_ALPHA_THRESHOLD`
  tunes the silhouette tightness. Set `ORB_FLOAT = False` for a circular badge instead.

### Changed
- **The send/stop button is now rendered with Pillow (×4 supersampled + LANCZOS) instead
  of a Tk `create_oval` + font glyph.** Tk canvas ovals aren't anti-aliased, so the old
  button looked jagged/low-res; the circle is now smoothly anti-aliased and the arrow is a
  crisp vector chevron. Cached per (diameter, state) with idle/hover/busy variants.

## [1.1.9] — 2026-06-04

### Fixed
- **The window no longer intermittently freezes / refuses to scroll.** The rounded-corner
  window region was re-applied on *every* `<Configure>` event. Because `SetWindowRgn(…,
  bRedraw=True)` forces a repaint that itself emits another `<Configure>`, this self-fed a
  ~50 ms loop, and each pass also ran `update_idletasks()` (a full layout flush). On a busy
  window that intermittently monopolized the UI thread: scrolling locked up and a streamed
  reply only rendered in the gaps. The region depends only on the window **size** (and
  collapsed/expanded state), not its position, so it's now re-applied only when the size
  actually changes — measured idle CPU dropped from ~9% to ~2%. (Found by sampling the live
  process with `py-spy`: `_apply_region`/`SetWindowRgn` was ~56% of the UI thread's active
  time. Transcript rendering was ruled out — inserts stay ~0.16 ms even on a 300K-char chat.)

## [1.1.8] — 2026-06-04

### Fixed
- **Clear now actually drops the context gauge instead of leaving the old conversation's
  usage on screen.** Clicking Clear interrupts the in-flight turn, whose cleanup schedules a
  context-usage refresh against the *old* session. That refresh round-trips to the CLI and
  could land *after* the new session was already up, overwriting the fresh (low) baseline with
  the old conversation's high %. Now: (1) the worker discards a usage reading if the client was
  swapped out mid-flight (the core race fix); (2) Clear blanks the shown % immediately on click
  rather than waiting for the async reset; (3) the reset-complete handler no longer nulls the
  freshly-reported new-session baseline. (The underlying session was always reset correctly —
  this was a stale-display race, not a failure to start a new conversation.)

## [1.1.7] — 2026-06-04

### Fixed
- **The overlay no longer burns a third of the context window on MCP tools you never use.**
  With the `claude_code` preset, the spawned CLI loaded *every* MCP server configured in your
  `~/.claude.json` and injected all of their tool schemas into the context — measured at
  ~72K tokens (36% of a 200K window) on a machine with many MCP servers, materialized on the
  first message even for a tiny text-only prompt. A single short message could appear to jump
  the context gauge ~30%. The overlay is a lightweight screen-chat that only needs the core
  Claude Code tools, so it now sets `strict_mcp_config=True` and does not inherit your MCP
  servers. Measured before → after (Haiku): a text turn went from 53% → **19%** of context;
  a 2-screenshot turn from 58% → **20%**. (New top-of-file `STRICT_MCP_CONFIG` constant —
  flip to `False` if you *want* your MCP tools available in the overlay.) Note: this is not a
  gauge bug — `get_context_usage()` was reporting real usage; and screenshots were never the
  culprit (two downscaled monitors cost only ~3K tokens).

## [1.1.6] — 2026-06-04

### Fixed
- **`Create Desktop Shortcut.cmd` no longer fails on install.** The launcher passed its
  own folder to PowerShell as `"%~dp0"`, which always ends in a backslash — so the closing
  `\"` was parsed as an escaped literal quote (CommandLineToArgvW rules), baking a stray `"`
  into the path. `Test-Path` then threw `Illegal characters in path` and no shortcut was
  created. The script now relies on PowerShell's built-in `$PSScriptRoot` (no path argument),
  and `create-shortcut.ps1` additionally strips any stray quote / trailing backslash from
  `-Dir` so it tolerates any caller. (The other `.cmd` files use `cd /d "%~dp0"`, a cmd
  builtin that isn't affected.)

## [1.1.5] — 2026-06-03

### Fixed
A full crash sweep by four parallel independent auditors (one per subsystem: threading,
asyncio/transport, Tk/Win32, external inputs), most findings reproduced with a runnable
test. 17 residual defects fixed — including a few introduced by the v1.1.4 changes:

- **The response stream is now closed on every turn exit.** v1.1.4's idle-timeout cancelled
  the stream read but never closed the async generator, which could leave the SDK's reader /
  pipe half-open (a leak, or a later disconnect that hangs). Now `aclose()`d (bounded).
- **Type-ahead capture can't get stuck off.** If monitor enumeration threw inside the
  background pre-capture, the "busy" flag was never cleared and pre-capture silently stopped
  for the rest of the session. It now always clears.
- **A streamed reply can't get visually detached from its "Claude" header.** Transcript
  pruning could delete the active header while the code still thought it was present; it now
  re-arms so the next chunk re-adds the header. Pruning also caps by characters now, so one
  giant unbroken line can't slip past the line cap.
- **Stop / Clear can't crash after the worker has stopped.** Hitting Stop or Clear once the
  background worker's event loop had closed raised straight into the UI; the interrupt path
  now tolerates a closed loop.
- **Quit can't leave an orphaned `claude` process.** Quitting while the worker was stuck
  connecting now cancels that connect so it can shut down cleanly. Quit is also idempotent
  (a fast double-close won't error).
- **A long, *silent* tool run is no longer mistaken for a dead connection** — once a tool is
  running, the no-activity timeout is much longer, so a quiet build/test isn't cut off.
- **Pasting is bounded and never blocks the window.** The clipboard read itself now happens
  off the UI thread (after a cheap "is there an image?" check), one paste runs at a time, the
  file count per paste and the total queued attachments are capped, and a "decompression
  bomb" image (tiny file, enormous decoded size) is rejected before it can blow up memory.
- **A turn's attachments are bounded in aggregate** (count + total bytes + de-duplicated), not
  just per file, so many accumulated images can't exhaust memory.
- **The update check can't be abused** to make the app read a huge response — the body and
  number of tags are capped before parsing.
- **Zooming no longer garbles existing chat bubbles / tool chips** (they keep their own font
  snapshot), the input box can't be sized negative during a tiny-width transient, the
  screenshot folder degrades to a fallback if it can't be created, and the worker's restart
  budget resets after a stable stretch (so rare failures spread over days don't add up to a
  permanent stop).

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

[1.2.3]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.2.3
[1.2.2]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.2.2
[1.2.1]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.2.1
[1.2.0]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.2.0
[1.1.9]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.1.9
[1.1.8]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.1.8
[1.1.7]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.1.7
[1.1.6]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.1.6
[1.1.5]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.1.5
[1.1.4]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.1.4
[1.1.3]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.1.3
[1.1.2]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.1.2
[1.1.1]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.1.1
[1.1.0]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.1.0
[1.0.0]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.0.0
