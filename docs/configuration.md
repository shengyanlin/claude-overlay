# Configuration

[← Back to the README](../README.md)

All settings live as constants at the top of `config.py` — but **you don't have to edit
the file**. Put personal values in a small per-machine **`config.json`** instead, so your
setup survives every update with no `git pull` conflicts:

| Windows | macOS |
|---|---|
| `%LOCALAPPDATA%\claude-overlay\config.json` | `~/Library/Application Support/claude-overlay/config.json` |

(the same folder that already remembers your toggles). List only the settings you want
to change, using the constant names below — for example:

```json
{
  "PERMISSION_MODE": "plan",
  "THEME": "dark",
  "WORKING_DIR": "C:\\Users\\you\\Documents"
}
```

Overridable: `WORKING_DIR`, `MODEL`, `MODEL_MENU_FILTER`, `QUEUE_MESSAGES`, `EFFORT`,
`PERMISSION_MODE`,
`SKILLS`, `STRICT_MCP_CONFIG`, `CLI_UPDATE_CHECK`, `CLI_AUTO_UPDATE`, `OVERLAY_AUTO_UPDATE`,
`TELEMETRY`,
`TELEMETRY_URL`,
`AUTO_SCREENSHOT_DEFAULT`, `SHOT_SCOPE`,
`SHOT_FORMAT`, `SHOT_JPEG_QUALITY`, `SHOT_DEDUPE_BITS`, `HIDE_SCREENSHOT_TOOL`, `THEME`,
`SHOW_IN_SCREEN_SHARE_DEFAULT`, `TASKBAR_BUTTON`, `HOTKEY`, `WINDOW_ALPHA`,
`CORNER_RADIUS`, `ORB_SIZE`, `FONT_SANS` / `FONT_SERIF` / `FONT_MONO`, and for Codex mode
`CODEX_MODEL`, `CODEX_EFFORT`, `CODEX_SANDBOX`, `CODEX_APPROVALS_REVIEWER`.

Precedence, weakest to strongest: the constants in `config.py` < `config.json` < an
explicitly set `CLAUDE_OVERLAY_*` env var — and the remembered ⚙-toggle state
(Window-only / Read-only) still wins over all three, exactly as it does over the
constants: `SHOT_SCOPE` and `PERMISSION_MODE` from the file only seed the first launch.
A typo'd key or wrong-typed value is skipped (never fatal) and called out in-chat at
startup, so a mistake can't silently launch a misconfigured session. To keep the file
somewhere else, point the `CLAUDE_OVERLAY_CONFIG` env var at it.

The settings themselves:

- `MODEL` — defaults to `"opus"`, a **family alias for the latest Opus**, so a future
  Opus release is adopted automatically. Use `"opus[1m]"` for the 1M-context variant, or
  `"fable"` / `"sonnet"` / `"haiku"` — every alias tracks the newest model of its family,
  and the in-app switcher lists the ones your login can pick (the statusline shows the
  concrete version each alias resolved to, e.g. `claude-opus-4-8`). Don't use `None`: the
  Agent SDK resolves `None` to an older model, not the CLI's interactive default.
- `MODEL_MENU_FILTER` — `true` by default: the model switcher hides families your account
  isn't entitled to, which it reads from the same record the CLI builds its own `/model`
  picker from (`modelAccessCache` in `~/.claude.json`). Without this the menu offered
  every family to everyone, and choosing one you don't have looks like nothing happened —
  the CLI does not error on an unentitled `--model`, it quietly runs your default model
  instead. Filtering only removes what that record positively contradicts: if it's
  missing or unreadable, or filtering would empty the menu, you get the full list. Set it
  to `false` (or `CLAUDE_OVERLAY_MODEL_FILTER=0`) if you've just been granted a model the
  record hasn't caught up with — any `claude -p` run refreshes it too.
- `QUEUE_MESSAGES` — `true` by default: `Enter` while a reply is streaming queues the
  message (shown above the input box, sent in order as each reply finishes — the Claude
  Code CLI's type-ahead behaviour). Set it to `false` to restore the old meaning of
  `Enter` mid-reply: interrupt. Stopping (the ■ button or `Esc`) always drops the
  line-up, with the texts listed in the transcript so nothing is silently lost.
- `TELEMETRY` / `TELEMETRY_URL` — anonymous usage reporting: one request per launch
  carrying the version, a random install id, the OS build and the Python version, and
  nothing else — no prompts, no replies, no screenshots, no paths. It is **on** in this
  release and it is **opt-out**: `DO_NOT_TRACK=1` or `CLAUDE_OVERLAY_TELEMETRY=0` turns
  it off, checked fresh on every launch. `TELEMETRY_URL` names the endpoint, and
  `"TELEMETRY_URL": ""` is a second way to switch it off — or point it at your own
  collector if you'd rather have your own numbers. Full details:
  [PRIVACY.md](../PRIVACY.md).
- `EFFORT` — the reasoning effort the overlay **starts** at: `"auto"` (default — no
  fixed level, the model decides per request), `"low"`, `"medium"`, `"high"`, `"xhigh"`,
  `"max"`, or `""` to inherit your CLI's setting
  (`effortLevel` in `~/.claude/settings.json`, or the CLI default). The same dial as the
  CLI's `--effort` flag, scoped to the overlay; your terminal sessions keep their own
  setting. This only seeds the first launch — the statusline's **⚡ menu** changes it live
  and remembers the choice, so you rarely need it here. Why Auto: a global `effortLevel`
  tuned for deep terminal work makes the model **think for extra seconds before every
  overlay reply** — at `xhigh` we measured 6–11s of thinking on one-line questions, and
  14–24s on a one-line rewrite once the model decided the answer also deserved a tool
  call; Auto answered the same in about 2s, and unlike a pinned Low it can still think
  hard when a question calls for it.
- `PERMISSION_MODE` — `"bypassPermissions"` by default (see the [security note](security-note.md)); this
  is just the **first-launch** mode — the **⚙ → Read-only** menu item switches
  the live session between `"plan"` (read-only) and this mode at any time, and the
  toggle **remembers your last choice across launches** (announced in-chat at startup
  whenever the remembered state differs from this default). Set `"plan"` to start
  locked read-only.
- `WORKING_DIR` — folder Claude operates in (default: your home directory).
- `THEME` — `"light"` (warm paper) or `"dark"`.
- `TASKBAR_BUTTON` — `True` (default) gives the frameless window a real, clickable
  Windows taskbar button (on a Mac: the Clawd icon in the menu bar); `False` for the pure
  no-taskbar floating overlay.
- `SKILLS` — which Agent SDK skills to expose: `"all"`, a list of names, or `None`.
- `STRICT_MCP_CONFIG` — `True` (default) keeps the overlay lean by **not** inheriting
  the MCP servers from your `~/.claude` config; set `False` to expose them here
  (handier, but their tool schemas cost a lot of context).
- `SHOT_FORMAT` / `SHOT_JPEG_QUALITY` (or the `CLAUDE_OVERLAY_SHOT_FORMAT` /
  `CLAUDE_OVERLAY_SHOT_JPEG_QUALITY` env vars) — screenshot payload: `"auto"` keeps
  the smaller of PNG/JPEG per capture; `"png"`/`"jpeg"` force one; JPEG quality 50–95.
- `SHOT_DEDUPE_BITS` (or `CLAUDE_OVERLAY_SHOT_DEDUPE_BITS`) — how different two
  auto-captures may look and still count as the same screen, in bits out of a 1024-bit
  perceptual hash. Auto-shot attaches a capture to **every** message and each one keeps
  costing vision tokens for the rest of the conversation, so a screen you haven't touched
  is skipped and Claude is pointed at the copy already in context. `2` (default) ignores
  a blinking caret, a ticking clock and re-compression noise while still re-sending on a
  single new line of terminal output; raising it past `3` starts missing small real
  changes, and `0` falls back to exact-bytes only.
- `SHOT_SCOPE` (or the `CLAUDE_OVERLAY_SHOT_SCOPE` env var) — what a screenshot covers:
  `"screens"` (default) captures every monitor, one image each; `"window"` captures
  **only the active window** — more private and cheaper in vision tokens, but Claude
  can't see anything outside it. This is just the startup default; flip it live with
  the **⚙ → Window-only** menu item — and the choice **is remembered
  across launches** (stored per-machine in `%LOCALAPPDATA%\claude-overlay\state.json`;
  setting the env var explicitly overrides it for that launch). While you're typing *in* the overlay,
  "active" means the window you were working in before it (tracked automatically), and
  when no usable window exists (fresh launch, desktop focused, window minimized) it
  falls back to full-screen capture rather than sending nothing.
- `RESUME_OFFER` (or the `CLAUDE_OVERLAY_RESUME_OFFER` env var) — on launch, offer a
  one-click **↺ Resume last conversation** when the previous run left one behind (the
  session id is remembered per completed turn; **Clear** wipes it, so a discarded
  conversation is never offered back). `RESUME_OFFER_MAX_AGE` bounds how old a
  conversation may be to qualify (default 7 days). The transcript isn't replayed —
  Claude just remembers the context and you keep going. Note the overlay resumes the
  session **it** recorded, not "the latest conversation in this directory" — if you
  continue that session elsewhere (the CLI or Desktop) afterward, resume still loads its
  newest state, but the "from … ago" label is measured from the overlay's last turn.
- `CLAUDE_OVERLAY_AUTH_GATE` (env var) — when the `claude` CLI's login dies in the one
  way nothing local can repair (a token refresh rejected with `invalid_grant` makes the
  CLI blank its own stored credentials), the overlay says so and **holds your message
  back** instead of feeding it — and its attachments — into a turn that cannot succeed.
  Set it to `0` to keep the notice but never block a send. Signing in again from a
  terminal (`claude auth login`) is picked up on its own: no restart, and the
  conversation is kept.
- `AUTO_SCREENSHOT_DEFAULT`, `FONT_SANS/SERIF/MONO`, `CORNER_RADIUS`, `ORB_SIZE`,
  `HIDE_SCREENSHOT_TOOL`, `WINDOW_ALPHA` — see inline comments.
