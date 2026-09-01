# Changelog

All notable changes to Claude Overlay are documented here.
This project follows [Semantic Versioning](https://semver.org/).

## [1.19.0] - 2026-09-01

Two contributed PRs, both by [@Justinkao2](https://github.com/Justinkao2) — thank you
again: [#8](https://github.com/shengyanlin/claude-overlay/pull/8) (seven features) and
[#9](https://github.com/shengyanlin/claude-overlay/pull/9) (the allowance ring).

### Added
- **Plan-allowance ring on the ✻ mark.** The mark is now a gauge: inner arc = the
  5-hour window, outer arc = the weekly one, drawn clockwise from 12 like the clock
  they are. Amber at 75%, red once spent. Hovering the mark opens a panel with both
  windows, their reset times, and context headroom in turns; the first reading is
  introduced by a one-time system line. Rendered through PIL at 4× and downsampled
  (Tk's `create_arc` has no antialiasing on Windows), mark grown 24→36px. The hover
  panel is deliberately a `Label` inside the root window, not a `Toplevel` —
  `WDA_EXCLUDEFROMCAPTURE` is not inherited, so a tooltip window would have been the
  one part of the overlay visible in a screen share.
- **The allowance is read directly, so the gauge is right before anything is sent**
  (new `usage.py`). The overlay polls the same endpoint the CLI's own `/usage` screen
  reads, once a minute, using the CLI's own OAuth token. The exception to "the overlay
  never sees a token" is drawn narrowly and tested: the host is a module constant no
  config or env var can redirect, the request is a GET, the token is re-read from the
  CLI's file each poll and never stored or logged, an expired token is skipped (never
  refreshed), and Bedrock/Vertex/API-key setups are never polled at all.
- **Past conversations.** A sessions list (new `sessions.py`) reads the CLI's own
  transcript store, with thumbnails, in-transcript cards, two-step delete, and
  one-click resume.
- **Copyable messages.** Text in user bubbles can be selected; a click copies the
  whole message as originally sent. Statusline chips mark non-default Read-only /
  Window / Shared modes.
- **Compaction progress bar** with an ETA fitted from the overlay's own past runs
  (seeded from CLI transcripts), capped at 90% and dropping the percentage on overrun.
- **Refused sends survive.** When the allowance runs out mid-conversation the message
  is restored to the box instead of lost, with an **opt-in** one-shot auto-resend armed
  for when the window resets (disarms on any manual edit, send, or Clear).
- **One-click update from the 🔔 notice** for git installs (visible console; ZIP
  installs get instructions instead), and `update.cmd` now names its pull source,
  pulls `--ff-only`, and refuses to run off-main or dirty — so a fork can no longer
  show a correct notice and then no-op the pull.

### Changed
- **Screenshot dedupe is perceptual now.** Measurement showed four grabs of an
  untouched screen produce four different SHA-256es — byte-identity never fired. The
  dedupe now uses a 1024-bit difference hash with a measured threshold (noise ≤2 bits,
  real change ≥4), configurable via `SHOT_DEDUPE_BITS` (0 = off).
- The status row holds still: fixed-width `model · context NN% · version`, no reflow;
  `~N turns` lives in the hover panel. The ring's track color is computed for WCAG
  contrast in both themes, with a test computing the actual ratio.

### Fixed
- **The usage poller could die on its first success**: a debug line formatted a field
  outside any try, above the queue put — one missing key killed the daemon thread and
  the gauge silently never appeared again. The reading now goes on the queue first and
  the log line is guarded, with a test that drives a genuine reading through the queue.
- Shared test fixture now resets allowance state between tests (a leaked reading could
  steal the gauge slot from the next test — the full suite only ever proved one
  ordering).

## [1.18.0] - 2026-08-29

Scroll-follow rework, contributed by [@Justinkao2](https://github.com/Justinkao2)
(the overlay's first external code PR — thank you!).

### Fixed
- **Streaming replies can no longer be silently stranded below the fold.** Whether to
  auto-scroll used to be re-derived on every insert from `yview()[1] > 0.999`, which
  only reads true when the view is pinned to the exact bottom. Any content-driven
  drift — a throttled giant line, an embedded table or user bubble, a window resize —
  left the view a hair short, read as "the user scrolled away", and the rest of the
  reply streamed in off-screen with nothing to say it had arrived. Following is now a
  **mode**: only a real user scroll gesture (wheel, scrollbar drag/click, keyboard
  nav) turns it off, scrolling back to the end resumes it, and sending a new message
  snaps it back on.
- Turn-end catch-up honours the follow mode instead of the old loose `> 0.90`
  heuristic, so a throttled giant line still lands on the end while a reader who
  scrolled up is left alone.

### Added
- **"↓ New output" jump pill.** Deliberately scrolling away during streaming is now a
  state that can persist, so a pill floats over the bottom-right of the transcript
  when you are away from the end — reading "↓ New output" once unseen output has
  landed ("↓ Latest" otherwise) — and one click takes you back. It reads the cached
  scrollbar fraction rather than calling `yview()`, which is O(line length) on a
  pathological newline-free line (the v1.1.9-class freeze).
- 7 tests locking down the follow mode and the pill (`tests/test_ui_scroll_follow.py`).

## [1.17.0] - 2026-08-20

Response-speed work, driven by a benchmark of the overlay against the Claude Code CLI
(same machine, same tasks — trivial replies, a file write, a PowerPoint-COM edit). The
engine verdict: the overlay's SDK transport is **not** slower than the CLI — the
measurable per-message gap was the auto-screenshot payload (+0.7–2.6s time-to-first-token
per message), and the biggest absolute delay on both sides was reasoning effort
(`xhigh` spends 6–11s thinking before even a one-line answer).

### Added
- **Unchanged screens are no longer re-sent.** Auto-screenshot now skips any capture
  that is byte-identical to the last one sent for the same monitor/window and tells
  Claude to keep using the copy it already has (the chat shows `🖼 unchanged`). Quick
  follow-ups — the most latency-sensitive messages there are — get their 1–3 seconds
  back. Manual Snap (and the legacy `"read"` mode) always attach.

  A stale "screen unchanged" note would be worse than a redundant image — it makes the
  model trust a screenshot it no longer has — so the dedupe memory is guarded on two
  fronts (hardened after an external code review of the first cut):
  - a capture only becomes a dedupe baseline once its turn returns a **clean result** —
    a turn that errors out or is stopped may never have delivered the image;
  - the memory is wiped by every event that can cost the context its screenshots:
    Clear and Compact **at click time** (a send can slip in before they complete), the
    CLI's **automatic** mid-conversation compaction (which, unlike explicit `/compact`,
    used to be invisible to the UI), a failed/lost resume, a fresh session standing in
    for one an old SDK couldn't resume, and any error. And in case some path is still
    uncovered, the note itself tells the model: if you no longer have that screenshot,
    say so instead of guessing.
- **`EFFORT` config knob** — the CLI's `--effort` dial, scoped to the overlay:
  `"low"`…`"max"`, or `""` (default) to keep inheriting your `settings.json`
  `effortLevel`. A global effort tuned for deep terminal work quietly taxes every
  overlay reply; now the overlay can run snappier without touching CLI sessions.
- **Fable in the model switcher** (`"fable"` / `"fable[1m]"` aliases, resolved to the
  concrete latest id like the other families) — so the overlay can run exactly what a
  CLI pinned to Fable runs.

### Notes from the benchmark (nothing to fix, worth knowing)
- Session connect has a ~10s floor (CLI/node startup); the overlay's declared MCP
  server and skills add under a second of it.
- `claude -p` (headless) can silently run the **org default model instead of your
  `settings.json` pin** — pin with `--model` when benchmarking anything.

### Fixed
- **`setup.cmd` could install every package into a Python the launcher never runs.**
  Reported from a managed corporate PC: after updating to 1.16.1 the app died at startup
  with `No module named 'PIL'` and BOTH packages "metadata says not installed" — right
  after the scripts had reported success. `setup.cmd` answered "is any Python on this
  PC?" with its own search — `py -3` (the *registry*, PEP 514) first, then a PATH
  `python`, then the folder scan — while the launcher asks `where pythonw` and then
  scans `%LOCALAPPDATA%\Programs\Python`. On a machine holding both a
  registry-registered Python that PATH never mentions (an Anaconda, an old python.org
  install) and the standalone tree setup itself provisions, pip fed the first and the
  launcher ran the second — and setup's closing "[OK] The app loads." was proven against
  the wrong interpreter too, so every screen said success while the next double-click
  crashed. `setup.cmd` now finds Python with the launcher's own search block (kept
  byte-identical across all four `.cmd` files — a test compares them, and two more pin
  the order and the fallback) and installs into what it finds.
- **The startup dialog's fix command could not be followed.** It named the interpreter
  that was running — `pythonw.exe`, which opens no window and prints nothing, so to the
  person pasting it the fix looked broken too. The advice now names the `python.exe`
  beside it (same install, same site-packages, visible output), and the dialog leads
  with the fix that needs no terminal at all: double-click `update.cmd` (or
  `setup.cmd`), either of which repairs the environment and re-proves the app loads.
- **The "two Pythons" warning was silent on exactly the machines it exists for.**
  preflight's launcher-interpreter lookup stopped at PATH, but on the machines
  `setup.cmd` provisions Python lives only under `%LOCALAPPDATA%\Programs\Python` — so
  the one mismatch the warning was written to catch went unreported there. It now scans
  that folder the way the launcher does. Also, a report run under pythonw (which is how
  `Diagnose.cmd` runs it) no longer counts `pythonw.exe` and the `python.exe` beside it
  as two different Pythons.
- **`update.cmd` now bootstraps pip with ensurepip** the way `setup.cmd` always has. On
  a hand-dropped Python without pip (offline `README.md` route B blesses exactly that),
  the refresh failed with "No module named pip" and "run Update again" was a wall.

## [1.16.1] - 2026-08-12

Hardening for the Python-install path that 1.16.0 introduced, from a post-release
review. No new features; if 1.16.0 installed Python for you, nothing changes.

### Fixed
- **The uv `.zip` extraction fallback crashed on every Windows PowerShell 5.1 machine —
  which is exactly where it runs.** When `Expand-Archive` is missing or blocked, the
  fallback called a 3-argument `ExtractToDirectory` overload that exists on .NET Core
  but not on .NET Framework, so under the stock PowerShell of a trimmed or locked-down
  box it always threw, and a successfully downloaded (or pre-staged) uv was never
  extracted. It now extracts entry by entry through an API present on both runtimes.
- **A corrupt pre-staged `offline\uv-*.zip` no longer kills the whole route.** A
  truncated zip (say, an interrupted USB copy) used to abort before the network was
  tried; it now falls through to the download, and the give-up report keeps the fact
  that a staged archive was tried and was bad — instead of overwriting it with the
  download story, which sent people re-running against the same broken file.
- **A broken `uv.exe` can no longer short-circuit the working alternatives.** A found
  uv (on `PATH` or in `Programs\uv`, e.g. left truncated by an earlier failed extract)
  was trusted on sight, skipping both the staged zip and the download; it is now probed
  with `uv --version` first, so the documented recovery path — stage a zip, re-run —
  actually recovers.
- **Download failures now name what actually happened.** The per-route report printed
  things like `HTTP curl exit 6` for a DNS failure, and a transfer that broke mid-body
  was reported as `HTTP 200` — sending users to IT with a proxy ticket for a download
  that merely disconnected. Codes are now self-describing: `HTTP 403`,
  `curl exit 6, no HTTP status`, `curl exit 18 after HTTP 200`.
- **`-DryRun` no longer requires curl.** Its connectivity probe called `curl.exe`
  unguarded — against the file's own "curl preferred, never required" rule — and could
  hang indefinitely on a black-holing proxy; it now falls back to `Invoke-WebRequest`
  and carries a 25-second timeout either way, and also reports whether a staged
  `offline\` zip or a `UV_PYTHON_INSTALL_MIRROR` would be picked up.
- **A Python dropped into `%LOCALAPPDATA%\Programs\Python\` by hand now works with
  pip.** `offline/README.md`'s route B blesses exactly that, but python-build-standalone
  trees ship `Lib\EXTERNALLY-MANAGED`, which makes every pip run refuse the interpreter.
  1.16.0 stripped the marker only when uv itself installed the Python; `setup.cmd` and
  `update.cmd` now sweep it from that folder (and only that folder — a system or conda
  Python elsewhere keeps its marker) before every package install.
- **`setup.cmd`'s post-install re-scan probes what it finds.** The pre-install scan
  verified every candidate with `--version`; the re-scan after an install took the first
  `python.exe` on faith, so a half-installed or policy-blocked interpreter could be
  handed to pip and fail two screens later instead of being reported where it was found.
- **`update.cmd` cannot inherit its way past the setup offer.** The "run setup.cmd
  now?" latch was read before it was ever set, so a `SETUPDONE` variable inherited from
  the calling environment silently skipped the offer — the exact dead end that screen
  was rewritten to remove. It is now cleared on entry.
- **The update-path give-up screen points at the no-download routes.** It still said
  only "python.org" — which, on the blocked machines that reach it, is the download that
  was just refused; it now also names `offline\README.md`, matching every other screen
  this release family touched.
- **`offline/README.md` covers 32-bit Windows.** The pre-stage table listed only
  `x86_64` and `aarch64` zips, while the installer selects `uv-i686-pc-windows-msvc.zip`
  on 32-bit Windows — a reader following the table staged a build that could not run,
  and the failure looked like a broken archive rather than a wrong architecture. Added
  the row and an architecture-match warning.

### Tests
- Still 555, but three of them can now actually fail: the `install-python.ps1` fixture
  strips block-comment bodies (a guarantee stated in the file header could satisfy a
  test about the code), the offline-before-network test anchors on the call site instead
  of a function definition it could never miss, and the bare-`!` tripwire no longer
  skips a whole line because one legitimate `!VAR!` appears on it — and now reads
  `set /p` prompts too, which delayed expansion mangles the same way.
- The launcher tests that spawn a freshly written stub `setup.cmd` now warm it until the
  OS agrees to run it: managed machines' endpoint protection refuses seconds-old `.cmd`
  files, and that refusal landed *inside* the launcher's output where the existing retry
  could not see it, intermittently failing the suite on exactly the machines the
  launcher is built for.

## [1.16.0] - 2026-08-12

### Added
- **Python now installs on locked-down work machines, where both previous routes were
  blocked.** Reported from a managed corporate PC where `setup.cmd` could not install Python
  at all — and where neither existing route could ever have worked: an endpoint-security
  product (BeyondTrust/Avecto) refuses any Python-Software-Foundation-signed `python.exe`,
  and because the rule matches the **signature** rather than the filename it covers the
  python.org installer, the Microsoft Store build *and* the official embeddable zip alike;
  separately, a proxy (Zscaler) answers `403` for `.exe` downloads from python.org, so the
  installer could not even be fetched. There is now a third route:
  [uv](https://github.com/astral-sh/uv), whose own executable arrives inside a
  GitHub-release `.zip` and whose CPython build is not PSF-signed. Verified end to end:
  Python 3.12.13 with tkinter, and `pip` installs into it.
- **`offline/` — two routes that need no working download at all.** A refused download is
  treated as ordinary rather than exceptional, because this policy is per-user/per-group:
  measured on two machines in the same company, the *same* python.org URL returned `403` on
  one and `200` on the other. So any of these downloads can be refused on somebody's
  machine — including uv's own, whose URL redirects to `release-assets.githubusercontent.com`,
  a different host a proxy can block separately from `github.com`. Drop `uv-*.zip` into
  `offline/` (staged from any machine that *can* reach GitHub) and it is used **before** the
  network is tried; or set `UV_PYTHON_INSTALL_MIRROR`, which is honoured and never
  overwritten, so an administrator can hand it to a whole fleet. Failing that, any Python
  3.10+ folder dropped into `%LOCALAPPDATA%\Programs\Python\` is enough — every script here
  *scans* that folder and uses whatever runs, regardless of how it got there. See
  [`offline/README.md`](offline/README.md).
- **The "could not install Python" screen now names a reason per route.** "403 on the `.zip`"
  is something you can hand to IT; "install failed" is not. It also leads with the two
  no-download routes instead of pointing you back at python.org — which, on an affected
  machine, is the exact download that was just refused.
- **`MCP_SERVERS`** — the overlay can be given specific MCP servers (same shape as
  `~/.claude.json`'s `mcpServers`) instead of the previous all-or-nothing choice between
  every server in your config and none. Defaults to `{}`, so nothing changes unless you set
  it. `STRICT_MCP_CONFIG` keeps doing its job around it, which means the servers you declare
  are the *only* ones present — the lean context stays lean.

### Changed
- **`update.cmd` is now the only file you double-click to update; `update-finish.cmd` is
  gone.** It existed for a real reason — `git pull` replaces `update.cmd` while `cmd.exe` is
  still reading it by byte offset, and the post-pull half has to be the code that was just
  *downloaded* — and that guarantee is unchanged: the updater still re-execs from `%TEMP%`
  (where git cannot touch the executing bytes) and still crosses into the freshly pulled
  file for the second half, now via a mode flag rather than a second filename.
- **`update.cmd` fixes a missing Python instead of telling you to go and find `setup.cmd`.**
  It offers to run it (Enter accepts), then re-scans from scratch — the re-scan matters
  because `PATH` inside that window stays stale for its whole life, so an interpreter just
  installed is only reachable through the folder scan. `update.cmd` never installed Python
  itself, so "run Update again" could not clear that wall however many times you tried it,
  and every route out ended at a file that a Desktop-shortcut user has never seen.

### Fixed
- **An update that skipped the package refresh no longer reports success.** The old post-pull
  half printed its warning and then carried on to `[OK] Updated` and exit 0, so an update
  that left the app unable to start looked exactly like one that worked. It now exits
  non-zero and says plainly that the pull itself did succeed.
- **Notices are no longer silently emptied of their `[!]` marker.** These scripts run with
  delayed expansion on, where a lone `!` in an `echo` is deleted — `echo [!] x` printed
  `[] x` — so every `[!]` notice the old post-pull half shipped was read as `[]` by every
  user.
- Python discovery in `install-python.ps1` searched for `Python3*\python.exe`, which no
  longer matches every layout it may find, and did not skip `Lib\venv\scripts\nt` — those
  are venv *template* launchers rather than usable interpreters, and one was measured taking
  17 seconds to answer a `--version` probe.

### Upgrading from 1.15.4
The `update.cmd` shipped in 1.15.4 pulls this release and then looks for the
`update-finish.cmd` that the pull has just deleted, so **that one run stops with "the
packages were NOT refreshed"**. The code is already updated at that point — just
**double-click `update.cmd` again** and it completes normally. Every later update is a single
double-click.

### Tests
- 555 pass. New: the merged updater's mode dispatch is pinned *before* the `%TEMP%` re-exec
  (the wrong order is an infinite `git pull` loop), a skipped package refresh can never claim
  success, no notice in a delayed-expansion script may hide a bare `!`, and `install-python.ps1`
  must keep `curl.exe` optional, avoid curl flags newer than the 7.55 that Windows 10 1803
  ships, judge a download by its status rather than by the output file existing, select the
  right uv build for all three Windows architectures, probe for `--no-bin` before passing it,
  and try `offline/` before the network.

## [1.15.4] - 2026-08-10

### Fixed
- **The "no Python" screen told you the fix but couldn't do it, and everyone who reached
  the screen had reached it precisely by the route that hides the fix.** It named
  `setup.cmd` and stopped — but the screen only ever shows up after double-clicking the
  launcher, very often from a desktop shortcut, from which this folder (and `setup.cmd`
  in it) is never visible. `update.cmd`/`update-finish.cmd` don't install Python either,
  so re-running "Update" could never clear this wall no matter how many times a stuck
  user tried it. `Start Claude Overlay.cmd` now asks **`Run setup.cmd now? [Y/n]`**
  (Enter accepts), runs it, and re-scans for Python from scratch — the re-scan matters
  because the PATH inside this same window stays stale for its whole life, so an install
  `setup.cmd` just performed is only reachable through the folder scan, not PATH.
- **`update.cmd` no longer runs `git pull` on the very file `cmd.exe` is still reading.**
  It now re-execs itself from a temp copy first, and the part that runs after the pull —
  which needs to know where this release looks for Python — lives in a new
  `update-finish.cmd` so it's always the freshly-pulled logic, never logic left over from
  the version being replaced. That driver copy is now resolved by an absolute path handed
  down from the original launch location, not `%~dp0`, since by the time it runs it's
  executing out of `%TEMP%`.
- **`requirements.txt` pinned nothing.** It said `claude-agent-sdk>=0.2.87` while
  `setup.cmd`/`update.cmd` both installed by spelling the package names out, so the file
  itself constrained nobody and a `pip install --upgrade` could land 45 releases ahead of
  what's actually been tested. The floor is now enforced from the one file, every install
  site reads it, and CI installs the newest available SDK on every run instead of a pin
  that quietly goes stale — so the CI badge is the live "does the newest SDK still work"
  signal instead of a number nobody revisits.
- **`Diagnose.cmd` always overwrote the clipboard with its report**, clobbering whatever
  a user had copied right before asking for help. `Diagnose.cmd --no-clip` keeps the
  report on screen without touching the clipboard.

### Added
- **`"auto"` permission mode.** Routes every action through Claude's own classifier
  model before it runs, instead of either blocking nothing (`bypassPermissions`) or
  asking about everything (`default`) — set it via `config.json`'s `PERMISSION_MODE`.
  Needs a recent model and an account with auto mode enabled.

### Tests
- New behavioural cases for the setup.cmd offer: it's presented **at most once** per
  launch, a missing `setup.cmd` is never offered (the manual fallback instructions still
  print), and answering `n` declines while still leaving those instructions on screen.
- Fixed a pre-existing flake: the test harness's fabricated "app started" marker was read
  the instant the file *existed*, before the stub app had actually written to it, so a
  timing-sensitive run could report the wrong interpreter started. Now waits for the file
  to be non-empty.
- 521 tests pass.

## [1.15.3] - 2026-08-07

### Fixed
- **The launcher walled machines that had Python, because it only ever looked at PATH.**
  `setup.cmd` installs Python into `%LOCALAPPDATA%\Programs\Python\Python3xx\` and then
  locates it by *scanning that folder* — deliberately, because the PATH inside its own
  window is stale after an install. The launcher never looked there. So "setup.cmd printed
  `[OK] The app loads.`" and "PATH has a Python" were two different claims, and a machine
  could satisfy the first while failing the second forever: setup succeeded, the launcher
  put up `[X] no Python on PATH ran`, and nothing on screen connected the two.
  `Start Claude Overlay.cmd`, `Diagnose.cmd` and `update.cmd` now search the folders
  `setup.cmd` installs into — plus `%ProgramFiles%\Python3*` and `%SystemDrive%\Python3*`
  — after PATH and before giving up, still verifying each candidate by running it.
- **`update.cmd` refreshed packages into the wrong interpreter for the same reason.** On a
  machine whose only Python is off PATH it reported that it could not find one and skipped
  the upgrade, which is how an install stays un-updatable.
- **`setup.cmd` no longer offers to install Python over one it already installed.** It now
  checks its own install folder before prompting.

### Changed
- **The "no Python" screen now says which of two different problems this is.** It reports
  the off-PATH install folders as well as `where pythonw` / `python` / `py`, states plainly
  that paths under `\WindowsApps\` are Windows placeholders rather than an install, and
  leads with `setup.cmd` — which installs Python itself, per-user, no admin — instead of
  sending people to python.org first.

### Tests
- The three `.cmd` scripts' interpreter-discovery block is now compared for byte equality,
  and asserted to search the folder `setup.cmd` installs into. All three shipped the same
  v1.15.1 defect independently; nothing was comparing them.
- `test_every_interpreter_invocation_goes_through_call` now strips `if` / `for` / `(`
  prefixes and inspects the command underneath, instead of skipping such lines. The old
  version would not have examined a single probe in the new directory-scanning block.
- New end-to-end case: PATH holding nothing but dead App-execution-alias stubs, with a
  working Python where `setup.cmd` puts it, must launch — and must launch *that*
  interpreter.
- **`test_copy_btn_puts_text_on_clipboard` was never testing the Copy button.** It
  synthesised a `<Button-1>`, which Tk discards for a withdrawn widget, so the handler
  never ran and the assertion compared the expected string against whatever was on the
  developer's clipboard. It passed only because an earlier run of the same test had left
  that string there. It now invokes the handler and records the clipboard calls, so it
  neither depends on nor destroys the machine's clipboard.

## [1.15.2] - 2026-08-07

### Fixed
- **A regression in v1.15.1: the launcher refused to start on machines where it always
  had.** v1.15.1 checked that `pythonw` was real before using it, but it checked the
  wrong file — the `python.exe` sitting *next to* `pythonw.exe`. That is a different
  binary, so the answer could be wrong in both directions. If `python.exe` was missing,
  blocked, renamed or non-zero for any reason at all, a perfectly good `pythonw` was
  discarded and the launcher printed `[X] No working Python was found` — a working
  install turned into a wall. (And in the other direction, a healthy `python.exe` could
  vouch for a `pythonw` that was itself a dead Store alias.)

  The check now **runs the exact binary it is about to launch**. `pythonw` cannot print
  its own version — it has no console — but its exit code still comes back, and that is
  all a check ever needed.

  Verified against five machine profiles. v1.15.2 starts everywhere v1.14.0 did, plus
  one profile neither earlier version could start:

  | Machine | v1.14.0 | v1.15.1 | v1.15.2 |
  | --- | --- | --- | --- |
  | `pythonw` works, `python.exe` check fails | starts | **wall** | starts |
  | Only the App-execution-alias stub | silent nothing | explains | explains |
  | Normal python.org install | starts | starts | starts |
  | `python.exe` works, `pythonw` broken | silent nothing | silent nothing | **starts** |
  | No Python at all | message | message | message |

- **The launcher can no longer dead-end while a usable `pythonw` is sitting on PATH.**
  After the verified candidates it tries a plain `python.exe` (which leaves a console
  window behind the overlay — ugly, but everything that goes wrong is now visible), and
  finally launches the first `pythonw` on PATH unverified, exactly as every release
  before v1.15.1 did. A wrong "no Python" wall is worse than the silent failure the
  check was added to prevent. The one candidate never tried blind is the alias stub
  itself, which is the only case that check really existed for.
- **A `pythonw` that is a `.bat`/`.cmd` shim killed the launcher outright.** `where
  pythonw` legitimately returns a shim on machines managed by pyenv-win and by several
  conda wrappers. Running a batch file from a batch file *without* `call` transfers
  control and never returns, so the launcher ended silently in the middle of its own
  check — no window, no message, exactly the symptom it exists to prevent. Every
  interpreter invocation in every shipped `.cmd` now goes through `call`, and a test
  asserts it structurally, because this failure is invisible on any machine whose Python
  is a plain `.exe`.
- **And such a shim could not be started even once found.** `start "" "some.bat" "arg"`
  becomes `cmd /K "some.bat" "arg"`, and cmd strips the outer quotes off a command that
  both begins and ends with one — so a shim whose path contains a space never ran, and
  left an idle console window sitting where the overlay should have been. Shims are now
  started as `cmd /c call ...`, which keeps the quoting intact and closes the window with
  the app. Plain `.exe` interpreters are still launched directly, so the common path is
  byte-for-byte unchanged.
- **The "no Python" message now prints what the machine actually has** — the raw
  `where pythonw` / `where python` / `where py` output — instead of only asserting that
  nothing was found. A report that doesn't say what it looked at can't be acted on.
- **`update.cmd` could not find the launcher's interpreter at all** on those same
  machines, so it refreshed packages into a different Python or refused outright. It now
  resolves `pythonw` by running it, and falls back to `pythonw` itself rather than to
  whichever Python happens to be first on PATH.
- **`Diagnose.cmd` gave up on exactly the machines it exists for.** It shared the same
  proxy check, so it answered `[X] No working Python was found` on installs that had
  one. It now reports under the launcher's own interpreter — `pythonw`'s output does
  reach a redirected file even with no console — prints which interpreter that is, and
  falls back to the raw `where` output instead of just giving up.

## [1.15.1] — 2026-08-07

### Fixed
- **The last silent way to fail: `Start Claude Overlay.cmd` launching a Python that
  isn't one.** Windows 11 ships an "App execution alias" stub at
  `%LOCALAPPDATA%\Microsoft\WindowsApps\pythonw.exe` that `where pythonw` finds *even
  when Python isn't installed*. Starting it opens the Microsoft Store, or does nothing
  at all — and the overlay never appears. This was the one case v1.15.0's crash
  reporting could not help with, because no Python ever ran to report anything. The
  launcher now verifies the interpreter (via the `python.exe` next to it — `pythonw`
  can't print its own version) before using it, falls back to the `py` launcher's
  windowed twin, and if nothing works says so and points at `Diagnose.cmd` instead of
  closing in silence. It **verifies rather than reorders**: a setup that works today
  keeps using exactly the interpreter it always did.

## [1.15.0] — 2026-08-07

### Added
- **A failed launch now says why, instead of doing nothing.** The overlay runs under
  `pythonw` so that it has no console window — which also meant that any error before
  the window appeared killed it with no window, no message and no log. From the outside
  that is "I updated it and now double-clicking does nothing", and it is unfixable at a
  distance: there is nothing to send anyone. Startup failures are now caught and
  reported **in a dialog** that names the cause *and the single command that fixes it*,
  and written to **`%LOCALAPPDATA%\claude-overlay\crash.log`** together with the facts
  these bugs actually turn on — which interpreter is running, which package versions it
  has, where the app is installed. Crashes *after* startup are covered too, including a
  worker thread dying: that used to leave a window that simply never answered again.
  A console run prints instead of showing a modal (so an automated run can't hang);
  `CLAUDE_OVERLAY_DIALOG=1`/`0` forces either way.
- **`Diagnose.cmd`** — double-click it and get one readable report: which Python the
  launcher uses, what's installed in it, whether the app actually loads (it tries, in a
  real subprocess), and the tail of the crash log. The report is copied to your
  clipboard, so "it won't open" can be a paste instead of a conversation. Every problem
  it finds comes with the exact command that fixes it, spelled with the full path to the
  interpreter that needs it — because "I already installed it" is nearly always "into
  the other Python", and a bare `pip install` in the instructions can land in the wrong
  one all over again.
- **`setup.cmd` and `update.cmd` now verify the result** instead of announcing success.
  Both load the app the way the launcher will and refuse to finish quietly if it can't
  start.

### Fixed
- **An update could leave the install broken and still report `[OK] Updated`.**
  `update.cmd` refreshed the Python packages with `pip --quiet` and never checked
  whether pip succeeded. Since pip uninstalls the old version before installing the
  new one, an interrupted or proxy-blocked upgrade can leave a package *gone* — and the
  next launch then did nothing at all, with the update having declared itself fine. pip
  failures are now fatal and explained.
- **`update.cmd` refreshed the wrong Python on a two-Python machine.** It used the `py`
  launcher while `Start Claude Overlay.cmd` runs whatever `pythonw` resolves to; where
  those differ (python.org plus the Microsoft Store build is the common pair) the
  upgrade landed in an interpreter the app never uses. It now resolves the launcher's
  interpreter first, and the diagnostics call out a mismatch explicitly.
- **The documented way to update from a ZIP produced a guaranteed crash.** Both
  `update.cmd` and the README told anyone without git to unzip over the folder and "at
  minimum replace `claude_overlay.py`". That was true when the overlay was a single
  script and has been wrong since it was split into modules — the new file imports
  siblings that a partial copy doesn't have. The instructions now say to replace every
  file, and doing it the old way produces an explanatory dialog rather than silence.

## [1.14.1] — 2026-08-04

### Fixed
- **"OAuth session expired and could not be refreshed" is no longer a dead end.** When
  the `claude` CLI's token refresh is rejected, the CLI blanks its own stored credentials
  — after which *every* message fails, in every process, until you sign in again;
  restarting the overlay cannot help. The overlay used to report the CLI's error and then
  add "Your next message is unaffected", which is true for an overload but flatly wrong
  here, so you'd keep writing messages that could never be delivered. Now it recognises
  the failure, says what actually fixes it (`claude auth login` in a terminal), and
  **holds the send back** rather than swallowing your typed prompt and its attachments
  into a turn that cannot succeed — nothing is consumed, so your message is still there
  afterwards. The login is polled in the background too, so a death is announced when it
  happens (or when you open the overlay onto one) and the **recovery** is announced as
  well: sign in from any terminal and the overlay picks it up on its own, no restart.
  Detection is deliberately one-sided — only the CLI's own on-disk "dead" marker counts,
  and it stands down entirely for API-key / Bedrock / Vertex / `apiKeyHelper` setups — so
  it can't refuse a message that would have worked. `CLAUDE_OVERLAY_AUTH_GATE=0` keeps the
  notice but stops it blocking.
- **A login refreshed elsewhere no longer poisons the running session.** The CLI caches
  the tokens it read at startup, so the long-lived subprocess the overlay holds open can
  end up using a superseded copy — and a copy it already failed to refresh is marked dead
  *inside that process only*. The overlay now fingerprints the credential file and, when
  it changes (any process's refresh, or your own re-login), recycles the CLI subprocess
  before the next turn — with `--resume`, so the conversation is kept. Same recycle is
  tried once after an auth-rejected turn, which fully recovers the case where the tokens
  on disk were fine all along.

## [1.14.0] — 2026-07-23

### Added
- **Conversations survive restarts.** The overlay remembers the CLI session id of your
  current conversation (per completed turn, in the same per-machine `state.json` as the
  toggles), and the next launch offers a one-click **↺ Resume last conversation** button
  in the chat — the transcript isn't replayed, but Claude remembers everything, so you
  just keep going. Closing the overlay to update it no longer costs you your context.
  Offered only for a session from the same working directory, younger than
  `RESUME_OFFER_MAX_AGE` (7 days); **Clear wipes the record**, so a deliberately
  discarded conversation is never offered back. If the session file is gone, the click
  falls back to a fresh session with a notice. Set `RESUME_OFFER = False` (or
  `CLAUDE_OVERLAY_RESUME_OFFER=0`) to never offer. Thanks @l8w8!

### Fixed
- **A transport drop mid-session no longer loses the conversation.** The
  "↻ Connection hiccup" recovery path used to reconnect with a *fresh* session —
  silently discarding all context. It now reconnects with `--resume` into the same
  conversation; only a failed resume falls back to a fresh session (and says so).

## [1.13.0] — 2026-07-23

### Added
- **Personal settings now live in a per-machine `config.json` — no more editing
  `config.py`.** Drop the settings you want to change (e.g. `{"PERMISSION_MODE":
  "plan", "THEME": "dark"}`) into `%LOCALAPPDATA%\claude-overlay\config.json` (or any
  path via the `CLAUDE_OVERLAY_CONFIG` env var) and they override the committed
  defaults at startup — so a customized setup survives every `git pull` / `update.cmd`
  with no conflicts. Precedence: constants < `config.json` < an explicitly set
  `CLAUDE_OVERLAY_*` env var, and the remembered ⚙-toggle state still wins over all
  three, exactly as before. Values are validated against a whitelist; anything typo'd,
  wrong-typed, or unknown is skipped and called out in-chat at startup — a mistake in
  the file can never silently launch a misconfigured (say, full-access) session, and a
  broken file degrades to the defaults instead of preventing launch. A misspelled key
  suggests the setting you probably meant, and an invalid value is still flagged even
  when an env var happens to shadow it. See the README's **Configuration** section for
  the full list of overridable settings. Thanks @l8w8!

## [1.12.2] — 2026-07-16

### Fixed
- **Turning the Read-only lock back off no longer errors on machines where
  `bypassPermissions` is disabled by policy.** If your Claude configuration disables bypass
  mode (common on managed/enterprise setups), the overlay silently launches in a degraded
  mode but still believed it could return to `bypassPermissions` at run time — so flipping
  Read-only off failed with *"Cannot set permission mode to bypassPermissions because it is
  disabled by settings or configuration"*. It now falls back to `acceptEdits` (the same
  effective full access here, since the overlay auto-approves prompts) instead of erroring,
  and remembers bypass is unreachable so it won't try again.

### Changed
- **The ⚙ settings-menu icon is now the crisp native Windows settings glyph** (Segoe Fluent
  Icons / MDL2 Assets) instead of the thin, awkward `⚙` character, so it reads cleanly in
  the status bar. Falls back to the plain glyph if those fonts aren't present.

## [1.12.1] — 2026-07-16

### Changed
- **The Window-only, Shareable, and Read-only toggles are now tucked behind a single ⚙
  settings menu, so the status bar isn't crowded.** Auto-shot, Compact, and Clear stay
  inline; clicking **⚙** opens a menu where each setting shows a ✓ when it's on. The gear
  turns the accent color while **Read-only** is on, so that safety state stays visible at
  a glance without opening the menu. Nothing about the toggles themselves changed — only
  where they live.

## [1.12.0] — 2026-07-16

### Added
- **A Read-only status-bar toggle (◉ / ○ Read-only).** Switches the live session between
  `"plan"` — Claude can see your screen, read files, and answer, but not edit anything or
  run commands — and the configured `PERMISSION_MODE`, with no restart. The toggle only
  flips once the CLI confirms the switch, and while read-only is on the overlay denies
  every permission escalation the agent requests (including `ExitPlanMode`, which the
  overlay's blanket auto-approval would otherwise grant — silently lifting read-only).
  Set `PERMISSION_MODE = "plan"` to start locked on first launch — the toggle remembers
  your last choice across launches (same per-machine state store as Window-only, and a
  remembered unlock launches the session directly in the full-access mode so it stays
  bypass-capable); because that memory is a safety state, startup announces it in-chat
  whenever it differs from the configured default. The mode also survives the overlay's
  automatic reconnects. When the session wasn't *launched* in `bypassPermissions` the
  CLI forbids elevating to it at run time, so unlocking lands on `acceptEdits` instead —
  effectively full access here, and the in-chat notice names the mode you actually got.
- **Screenshots can now capture just the active window instead of every monitor.** A new
  **◉ / ○ Window-only** status-bar toggle (startup default via `SHOT_SCOPE` /
  `CLAUDE_OVERLAY_SHOT_SCOPE`) scopes each capture to the window you're working in —
  more private (Claude sees nothing outside that window) and much cheaper in vision
  tokens on multi-monitor setups. Because the overlay itself has focus while you type,
  it remembers the window you were in before summoning it and captures that one; the
  window's title is passed to Claude so it knows what it's looking at, and the visible
  frame is captured without the drop shadow. When no usable window exists (fresh
  launch, desktop focused, window minimized) it falls back to the normal full-screen
  capture rather than sending nothing. The toggle remembers your choice across launches
  (a tiny per-machine `state.json` under `%LOCALAPPDATA%\claude-overlay`); an explicitly
  set `CLAUDE_OVERLAY_SHOT_SCOPE` env var overrides the memory for that launch.

Both features were contributed by [@l8w8](https://github.com/l8w8) in
[#3](https://github.com/shengyanlin/claude-overlay/pull/3).

## [1.11.4] — 2026-07-15

### Fixed
- **The model menu now picks up a newer model the day it becomes available, instead of
  staying stuck on the previous one.** The overlay ships model *families* ("Opus",
  "Sonnet", "Haiku") and asks the Claude CLI which concrete version each family currently
  means, caching the answer so most launches are instant. That cache was only refreshed
  when the CLI itself was upgraded — but which version a family points to can also change
  with no CLI upgrade at all (for example when your account or organization is granted a
  newer model mid-week). When that happened, the overlay kept resolving the family to the
  older version indefinitely, so choosing "Sonnet" still ran the previous Sonnet even after
  a newer one went live. The cache now also expires on a timer (re-checking at least every
  few hours) and a cache written by an older overlay is refreshed on first launch, so a
  newly available model shows up on its own without reinstalling or clearing anything.

## [1.11.3] — 2026-07-13

### Fixed
- **The model shown in the status line is now correct for the 1M-context option.** When you
  picked "Opus (1M)", the status line displayed the *previous* Opus version (e.g. "4.7")
  even though your messages were actually being answered by the latest Opus at a 1M-token
  window. The overlay was reading the model name from a Claude CLI field that reports the
  wrong version for 1M sessions; it now shows the model it actually runs, so the label
  matches reality. (A genuine cross-model override — e.g. one forced by managed settings —
  still shows through.) No change to which model runs; this is a display fix only.

## [1.11.2] — 2026-07-10

### Fixed
- **The taskbar button now shows the Clawd icon in more of the cases where it used to fall
  back to the generic Python icon — including on locked-down machines, and after the overlay
  has been closed.** The earlier taskbar-icon fixes relied on a Start Menu shortcut the
  overlay creates for itself; on a managed machine where security policy (AppLocker /
  PowerShell ExecutionPolicy) blocks that shortcut from being created, the icon still fell
  back to pythonw's — and the closed/pinned icon depended on that shortcut too. The overlay
  now *also* stamps its identity, a relaunch command, and its icon directly onto the window
  itself, with no shortcut involved at all, so: a pin made from the running overlay keeps the
  Clawd icon **and** relaunches the overlay when you click it after closing — even with no
  Start Menu shortcut; and the running window's button is given the Clawd icon directly. This
  is all done in-process (no extra files, no PowerShell), so it works on machines where the
  shortcut approach can't run.

## [1.11.1] — 2026-07-10

### Fixed
- **The taskbar button now shows the Clawd icon even when the overlay runs under Microsoft
  Store Python.** If your Python came from the Microsoft Store (an MSIX-packaged build),
  Windows forces every window that process opens to use the *package's* taskbar identity —
  pythonw's generic Python icon — and silently ignores the app identity the overlay sets for
  itself, so the earlier Start Menu shortcut fix couldn't take effect for those users. The
  overlay now stamps its identity directly onto the window (not just the process); a
  window-level identity outranks the package one, pulling the button onto the matching Start
  Menu shortcut that already carries the Clawd icon. Regular (non-Store) Python was never
  affected. Thanks to @krystallinyuheng-cpu for the fix.

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

[1.14.1]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.14.1
[1.14.0]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.14.0
[1.13.0]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.13.0
[1.12.2]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.12.2
[1.12.1]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.12.1
[1.12.0]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.12.0
[1.11.4]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.11.4
[1.11.3]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.11.3
[1.11.2]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.11.2
[1.11.1]: https://github.com/shengyanlin/claude-overlay/releases/tag/v1.11.1
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
