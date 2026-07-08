# Changelog

All notable changes to Claude Overlay are documented here.
This project follows [Semantic Versioning](https://semver.org/).

## [1.11.0] — 2026-07-08

### Added
- **The overlay now tells you when the Claude CLI it runs on is out of date — and updates
  it in one click.** The overlay is a thin layer over the `claude` command-line tool, and
  which models you can use is decided by *that tool*, not the overlay. So you could update
  the overlay to the newest version and still be stuck on an older model, simply because the
  CLI underneath hadn't been updated (its own auto-updater doesn't run for the npm install
  the overlay uses, so it can quietly fall many versions behind). Now, on launch, the overlay
  checks in the background whether your installed CLI is behind the latest release; if it is,
  it shows a one-line notice with an **Update** button that runs the upgrade for you
  (`npm install -g @anthropic-ai/claude-code@latest`). Nothing happens silently or without a
  click, and after it finishes you just restart the overlay to pick up the newest models. The
  check is best-effort and quiet on any failure (no npm, offline, corporate proxy), and it's
  throttled to once a day so it costs nothing on normal launches. Turn it off by setting
  `CLAUDE_OVERLAY_CLI_UPDATE_CHECK=0` (e.g. a locked-down machine where global npm installs
  aren't allowed). Once the update finishes, the same button turns into **"✓ Updated — click
  to restart"** — one click relaunches the overlay for you so the new models take effect,
  no manual close-and-reopen. If the update can't complete because a running Claude process is
  holding the CLI (Windows won't let an executable be replaced while it's in use — often the
  overlay's own session, or another open Claude Code window), the button says so in plain
  language and lets you **click to retry** after closing the other window, instead of showing
  npm's raw error.

## [1.10.4] — 2026-07-08

### Fixed
- **Pinning the overlay to the taskbar now works like a normal app — the pin relaunches
  it and keeps the Clawd icon.** Previously, if you pinned the overlay's taskbar button
  and then closed the app, clicking the pin wouldn't reopen it, and the pinned icon
  turned into a generic Python icon. Root cause: the overlay declares its own app identity
  (an AppUserModelID) so the taskbar groups it and shows the Clawd icon — but Windows will
  only let you *pin* such a window if there's a matching **Start Menu shortcut** carrying
  the same identity. There wasn't one, so pinning fell back to the bare `pythonw.exe`
  Python launcher: nothing to relaunch, and Python's own icon. The overlay now
  **automatically creates that Start Menu shortcut on launch** (pointing at itself, with
  the Clawd icon and the matching identity), so pinning behaves correctly for everyone with
  no manual step. It's a one-time thing — the check on every later launch is a cheap no-op,
  and it quietly re-creates the shortcut if you move the folder. If you already have a
  broken pin from before, unpin it and pin again after this update.

## [1.10.3] — 2026-07-02

### Fixed
- **The overlay no longer freezes when Claude wants to ask you a multiple-choice question.**
  Claude Code has an interactive "pick one of these options" question tool (AskUserQuestion). In
  the full CLI it pops up a little chooser; in this overlay there's no such chooser to answer it,
  so when Claude reached for that tool the turn just hung — stuck "thinking…" for up to half an
  hour before it gave up. (A recent CLI update made Claude start using that tool where older
  versions didn't, which is why it began happening.) Now the overlay tells Claude that tool isn't
  available here, so instead of stalling it simply **asks its question inline as normal text** —
  and you answer by typing your reply, the way any other message works. Belt-and-suspenders: the
  tool is both removed up front *and* refused at run time, so a question can never hang the window
  again.

## [1.10.2] — 2026-07-02

### Fixed
- **The overlay was quietly running one Opus version behind (statusline showed 4.7, not 4.8).**
  v1.10.0 switched the model config to family aliases (`opus`) so the overlay would always run
  the *latest* model with no updates — but it turns out the CLI, when driven the way the overlay
  drives it (the Agent SDK's streaming transport), resolves a bare alias to a **version-behind**
  model: `opus` came back as `claude-opus-4-7` even though the same CLI in one-shot mode — and
  Claude Code itself — resolve `opus` to `claude-opus-4-8`. So "always latest via alias" silently
  gave you last-generation-latest. The overlay now resolves the family alias to the concrete
  latest id at startup (by asking the CLI's honest resolution path) before it connects, and does
  the same when you switch models from the statusline menu — so you actually get 4.8. The alias
  stays in the config (auto-update on new releases is preserved), and the lookup is cached per CLI
  version, so it costs at most one quick probe after a CLI upgrade and nothing on normal launches.
  If the probe can't run (offline, not logged in), it falls back to the old behaviour rather than
  failing to start.

## [1.10.1] — 2026-07-01

### Fixed
- **Unplugging a monitor could leave the overlay impossible to bring to the front.** If the
  overlay was sitting on a screen that then got disconnected (or you changed your display
  layout), its position could end up outside every remaining monitor — so it was there, but
  drawn where you couldn't see it. Clicking its taskbar button, Alt-Tabbing to it, or pressing
  the hotkey correctly *activated* it but only ever changed its stacking order, never its
  position, so it stayed stranded off-screen and never appeared. The overlay now makes sure it's
  on a connected monitor whenever you summon it (taskbar click / Alt-Tab / restore / hotkey /
  expand), and also notices a display change on its own (a monitor plugged or unplugged, a
  resolution change) and pulls itself back onto a visible screen — placed within the monitor's
  work area, so it never lands under the taskbar. A window you've deliberately parked slightly
  off an edge is left alone; only a fully off-screen window is moved.

## [1.10.0] — 2026-07-01

### Changed
- **The model switcher now always offers the *latest* model of each family — automatically.**
  The in-app model menu (click the statusline) used to list pinned versions like *"Opus 4.8"*,
  so a newly released model wouldn't show up until you updated the app. Its entries are now
  **family aliases** — **Opus**, **Opus (1M)**, **Sonnet**, **Haiku** — which the Claude CLI
  resolves to the newest model of each family at run time. So when Anthropic ships a new model
  (say a future Sonnet 5), the overlay picks it up with **no update needed**: click the family
  and you're on its latest. The statusline still shows the concrete version each alias resolved
  to (e.g. `claude-opus-4-8`), so you can always see exactly what you're running, and the
  startup default is likewise the latest Opus.

## [1.9.0] — 2026-06-29

### Added
- **Show the overlay in screen shares when you want to — a new "Shareable" toggle.** By design the
  overlay is invisible to screen capture (Teams / Zoom / Meet / OBS screen share, PrintScreen, even
  its own screenshots), so your private chat with Claude never leaks onto a shared screen — which is
  also exactly why you couldn't share it on purpose. The new **◉ / ○ Shareable** switch in the status
  bar lets you flip that per meeting: turn it **on** to make the overlay appear in your screen share
  (e.g. to demo it, or to reference an answer while presenting), turn it **off** to go private again.
  Default is **off (private)**, no restart needed, and a one-line confirmation tells you it took —
  handy because the change is invisible on your *own* screen (the window looks identical either way;
  only what others see in the share changes). When it's on, screenshots the overlay sends Claude still
  never contain the overlay itself.

### Changed
- **Tidier status bar.** Removed the **Snap** button — your screen is already captured automatically
  on every message — and renamed the **auto-screenshot** toggle to **Auto-shot** so it reads
  consistently next to the **Compact** and **Clear** buttons.

## [1.8.0] — 2026-06-29

### Added
- **Compact the conversation to free up context — with a live animation.** A new **Compact** button
  in the status bar (next to Snap/Clear) summarizes the conversation so far and drops the older
  turns, so a long session stops eating into your context window — the same thing the Claude Code
  CLI's `/compact` does. While it runs, the chat shows an animated line (a pulsing ✦ sparkle,
  "Compacting conversation…", and an elapsed timer) that then turns into a one-line result reporting
  how much was reclaimed — e.g. *"✦ Compacted — 43,196 → 4,970 tokens (saved 88%)."* Your earlier
  context is **summarized, not lost**, so you can keep going. You can **Stop** it mid-run, and if the
  result can't be confirmed it says so rather than claiming success.

## [1.7.2] — 2026-06-28

### Changed
- **The collapsed "task done" badge now sticks around until you follow up.** Previously the green ✓
  cleared as soon as you expanded the overlay, so expanding to read the reply and then re-collapsing
  lost it. It now means *"the last turn finished — awaiting your next message"*: it appears when a
  reply completes, **persists across expand/collapse**, and only clears when you send the next
  message (or clear the chat). It still shows only while collapsed.

## [1.7.1] — 2026-06-27

### Added
- **A "task done" badge on the collapsed orb.** When a reply finishes while the overlay is
  collapsed to its orb, a small green ✓ now appears at the orb's top-right — so if you sent it off
  to work and minimized it, you can see at a glance that the answer is ready. It clears when you
  expand the overlay or start a new turn. (The floating-sprite clip region is rebuilt to include the
  badge, so it isn't clipped away; it composes cleanly with the session name label too.)
- **`setup.cmd` can now install Python for you.** If no real Python is found, setup offers to
  install it automatically (a new `install-python.ps1`): it uses winget (user scope, no admin) when
  available, otherwise downloads the official python.org per-user installer (which includes tkinter,
  pip, and the `py` launcher). It then continues straight to installing the overlay's packages —
  instead of the old dead-end that just told you Python was missing. Declining, or a failed install,
  still prints clear manual steps.

### Fixed
- **Pasting copied text no longer turns into a pasted image.** Many apps (browsers, Office,
  screenshot tools) put a bitmap on the clipboard *alongside* the text you copied, and the overlay's
  Ctrl+V was treating any clipboard image as an image paste — so plain text came in as a picture. Text
  now wins: if the clipboard has text, it pastes as text; an image is only attached when there's
  image/file content and no text.

## [1.7.0] — 2026-06-27

### Added
- **Name each overlay — tell several apart at a glance.** Click the **"Claude"** title to give this
  overlay a name (type inline, Enter or click away to save, Esc to cancel); an unnamed overlay shows
  a faint **"Click to name this session"** hint next to the title to point the way. The name also
  becomes the window's taskbar/Alt-Tab title. Most useful **collapsed**: when you minimize a named
  overlay to its orb, the name now floats **beneath the orb** as crisp black text with a soft white
  halo around the letters (no box, no frame) — so if you keep several overlays open, one per task,
  you can tell which orb is which without expanding them. The name is per session (it isn't saved
  across restarts). Unnamed overlays collapse to just the orb, exactly as before.

### Fixed
- **Clicking the taskbar button now brings the overlay to the front.** Because the window is
  always-on-top *and* frameless, a taskbar-button click activated it but didn't re-order it above
  other always-on-top windows, so it could stay buried (or just unfocused). It now raises itself to
  the very front on a taskbar click, Alt-Tab, or restore. (Pure z-order — no `<Configure>`/region
  churn, so it stays clear of the v1.1.9 freeze class.)

## [1.6.0] — 2026-06-23

### Added
- **A real taskbar button — like any other app.** The overlay is a frameless, always-on-top
  window, which on Windows means it had *no* taskbar button at all: no way to click it back to the
  front, no Alt-Tab entry, no at-a-glance "it's running". It now shows a proper taskbar button with
  the Clawd icon — click it to focus/raise the overlay, find it in Alt-Tab, and see that it's
  running. The window stays frameless and rounded; only the taskbar presence changes. Set the new
  `TASKBAR_BUTTON` config constant to `False` for the original no-taskbar floating-only behaviour.
  (Under the hood: `WS_EX_APPWINDOW` forces the button onto the borderless window, an explicit
  AppUserModelID makes the taskbar show the overlay's own icon instead of Python's, and every
  show/restore re-asserts the frameless look so a taskbar restore never flashes a title bar.)
- **Skills are now available to the overlay.** A new `SKILLS` config constant exposes your enabled
  Claude Code skills to the overlay (default `"all"` — every skill installed on the machine; or pass
  a list to enable only specific ones, or `None` to disable). Previously the overlay wired up no
  skill discovery at all. Enabling skills also lets the underlying CLI load your `~/.claude` user
  settings; MCP servers stay blocked by `STRICT_MCP_CONFIG`, and the added context cost is minimal
  (~1% of a 200K window for ~16 skills).

## [1.5.3] — 2026-06-17

### Fixed
- **Setup no longer claims to find Python on a machine that doesn't have it.** Windows 11 ships a
  0-byte "App execution alias" stub (`%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe`) that is
  present even when Python isn't installed — running it just prints *"Python was not found…"* and
  exits. The old check used `where python`, which that stub satisfies, so `setup.cmd` printed
  *"[OK] Python found"* and only fell over later at the package step. Detection now **verifies the
  interpreter actually runs** (`py -3 --version` / `python --version`, preferring the `py` launcher,
  which the Store alias never shadows) instead of trusting `where`; it reports the real version it
  found, and when none is present it spells out that the Microsoft Store `python` shortcut doesn't
  count and how to turn the alias off. `update.cmd` had the same `where`-based check and got the
  same fix.

## [1.5.2] — 2026-06-14

### Changed
- **"Last turn ended with an error" now tells you *why*.** When the Claude Code CLI reports a turn's
  result as an error, the overlay used to show a bare *"The last turn ended with an error."* It now
  surfaces the CLI's actual reason — e.g. *overloaded error* (the model was briefly overloaded),
  *max turns*, or *rate limit error* — pulled from the result's `subtype`/`result`, and adds *"Your
  next message is unaffected"* (the error is per-turn; the session stays healthy, which is why the
  next message works). The full detail is also written to the activity log when
  `CLAUDE_OVERLAY_DEBUG_LOG` is set, so a past occurrence can be diagnosed after the fact.

## [1.5.1] — 2026-06-14

### Fixed
- **Nothing in the chat gets clipped when you drag the window narrower any more.** Embedded items
  were sized when first drawn and weren't re-laid-out on resize, so making the window narrower cut
  them off on the right:
  - **Your message bubbles** are sized to the chat width and right-aligned, so a narrower window
    slid them partly off the right edge — worst for short messages, which hug the far right (hence
    it only happened *sometimes*). Bubbles now re-fit to the new width on resize.
  - **Tool-call chips** (the `❯ Bash …` pills) are sized to their text, so a long command/path
    overflowed a narrow window. They now cap their width to the chat and ellipsize the argument
    (`…`) so they always fit, and grow the text back when you widen the window.
  - Tables re-fit to the new width too.

  The re-layout is debounced (a drag settles into a single pass) and fires only on an actual width
  change, so it never touches the streaming/scroll path (no v1.1.9-class freeze).

## [1.5.0] — 2026-06-14

### Added
- **Copy Claude's replies.** Each of Claude's replies now has a small, always-visible **⧉ Copy**
  button beneath it — the way ChatGPT and Claude show one. Click it and the reply goes to the
  clipboard as **raw Markdown** (the `**bold**`, `#` headings and `| tables |` exactly as written,
  so it pastes with its formatting intact); the button flashes **✓ Copied** for a moment and
  brightens on hover so it reads as clickable. It appears under the finished reply and copies the
  whole turn's answer text (Markdown only — extended thinking and tool chips are excluded). The text
  is snapshotted when the button is drawn, so an older reply still copies the right thing after
  newer turns. Like the v1.4.1 tables, the button forwards the mouse wheel, so hovering it never
  blocks scrolling.

### Fixed
- **Zoom now resizes the *whole* chat.** Ctrl +/− (and Ctrl+mouse-wheel) used to grow only the
  flowing text — your message bubbles, the tool-call chips, and tables stayed frozen at the size
  they were drawn (they're fixed-size canvases that drew with a snapshotted font, so the shared
  zoom didn't reach them; a bigger font would have overflowed their box). They now re-render at the
  new zoom too — recomputing their box each time, so nothing overflows — and the new Copy button
  scales with them, so the entire transcript zooms together. The re-render is debounced and runs
  only on a zoom event (never while streaming), so it doesn't touch the streaming/scroll paths.

## [1.4.2] — 2026-06-13

### Fixed
- **Setup no longer dead-ends when `pip` is missing or off PATH.** `setup.cmd` now bootstraps pip
  with `python -m ensurepip --upgrade` (only if `python -m pip` isn't already available) *before*
  installing the packages — so a Python that shipped without pip, or one whose `Scripts\` folder
  isn't on PATH, no longer stops at "pip install failed". The README's by-hand and "let Claude
  install it" steps now use `python -m pip` instead of a bare `pip` for the same reason.

### Added
- **Heads-up when you have the npm `claude` instead of the native build.** An npm install exposes
  `claude.ps1`, which PowerShell resolves `claude` to — and Windows' default **Restricted**
  ExecutionPolicy blocks `.ps1`, so typing `claude` in PowerShell fails with *"running scripts is
  disabled on this system."* `setup.cmd` now detects this and prints the three fixes (run `claude`
  from CMD, `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or — recommended — install the
  native `.exe` build via `irm https://claude.ai/install.ps1 | iex`). The overlay itself was never
  affected: it launches the CLI via `claude.cmd`, which the policy doesn't gate.

## [1.4.1] — 2026-06-09

### Added
- **A scrollbar.** A thin draggable scrollbar now sits on the right edge of the chat: it shows
  where you are in the transcript, you can drag the thumb (or click the track) to move through a
  long reply, and it auto-hides when everything fits. It's also a wheel-independent way to
  scroll.

### Fixed
- **The mouse wheel / trackpad now scrolls when the cursor is over a table** (or any embedded
  element). Embedded widgets were swallowing the wheel event, so scrolling did nothing while
  hovering a table — which, when a table filled the view, felt like the whole window had frozen
  (the arrow keys still scrolled). Embedded elements now forward the wheel to the chat.

### Changed
- Tables are now drawn on a single lightweight canvas instead of a grid of label widgets — same
  look and column alignment, far less per-table layout work, and a single place to forward the
  wheel from.

## [1.4.0] — 2026-06-08

### Added
- **Markdown is now rendered in Claude's replies.** Previously the chat showed Claude's raw
  Markdown — literal `**asterisks**` around bold text and `| pipe | tables |` that didn't line
  up. The overlay now renders it:
  - **Bold**, *italic*, and `inline code` — formatted live *as the reply streams* (the markers
    turn into styling the instant their closing token arrives, so you never see them linger).
  - **Headings** (`#`/`##`/`###`), **bulleted and numbered lists**, **blockquotes**, and
    horizontal rules.
  - **Fenced code blocks** (` ``` `) render as a monospace block, kept verbatim (so `**` or `|`
    inside code stays literal).
  - **Tables** render as a real grid with thin cell borders. Each cell is laid out by the grid,
    so Chinese and English columns line up exactly — something a monospace text table can't do
    with a non-CJK code font. The raw rows show as they stream, then snap into the grid the
    moment the table block ends.

  Streaming, scroll position, the transcript cap, and live text-zoom (Ctrl +/−) all still work;
  embedded tables are freed with the rest of the transcript when it's pruned.

  Rendering is designed to stay light during streaming: text is appended incrementally (the
  current line is only re-parsed when an emphasis marker actually arrives), and the auto-scroll
  is throttled on pathologically long unbroken lines, so a long reply can't bog down scrolling.
  A pipe inside `inline code` no longer splits a table cell.

### Added
- **Streaming "thinking".** Extended-thinking tokens now stream into the chat as a muted
  `✻ thinking` block *before* the answer, instead of being discarded. The (often 15–30 s)
  wait before the first answer token is now visibly alive — you can watch Claude reason,
  the way the CLI shows it — rather than staring at a frozen "thinking…". The model's
  speed is unchanged; what changes is that the wait no longer *looks* dead.
- **Office COM efficiency guidance.** The system prompt now nudges Claude to drive
  PowerPoint / Excel / Word automation efficiently — batch all inspection into one
  PowerShell script and all edits into another (instead of a call per shape/cell/slide),
  cache COM references, and (Excel) disable `ScreenUpdating`/`Calculation`/`EnableEvents`
  around bulk writes. On a controlled Excel benchmark this cut wall-time ~30% and cost
  ~43% with no loss of correctness.
- **Opt-in activity log.** Set the `CLAUDE_OVERLAY_DEBUG_LOG` environment variable to a
  file path to record a timestamped, one-line-per-event trace of the worker (turn start,
  tool calls, results, errors, reconnects, a throttled streaming heartbeat) — useful for
  diagnosing a slow or stuck turn from outside the (console-less) app. **Off by default**;
  reply/thinking text is never written (only a heartbeat + character count).

### Fixed
- **Closing the window (✕) now always exits the process.** A wedged background thread
  could previously leave a headless `pythonw` process (and its `claude` CLI child) running
  after you closed the overlay. Quit now does its graceful, bounded shutdown and then
  guarantees the process terminates, so nothing lingers in the background.

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

[1.11.0]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.11.0
[1.10.4]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.10.4
[1.10.3]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.10.3
[1.10.2]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.10.2
[1.10.1]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.10.1
[1.10.0]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.10.0
[1.9.0]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.9.0
[1.8.0]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.8.0
[1.7.2]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.7.2
[1.7.1]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.7.1
[1.7.0]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.7.0
[1.6.0]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.6.0
[1.5.3]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.5.3
[1.5.2]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.5.2
[1.5.1]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.5.1
[1.5.0]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.5.0
[1.4.2]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.4.2
[1.4.1]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.4.1
[1.4.0]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.4.0
[1.3.0]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.3.0
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
