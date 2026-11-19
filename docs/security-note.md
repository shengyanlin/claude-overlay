# ⚠️ Permissions and security

[← Back to the README](../README.md)

The default `PERMISSION_MODE = "bypassPermissions"` makes this a **fully
autonomous agent**: Claude can edit files and run commands in `WORKING_DIR`
**without asking**, and it can see your screen. Combined with screen vision, that
also lets it **act on the app you have open** — e.g. edit the document or slide
deck on your screen via Windows/COM automation (AppleScript on a Mac), and (with autosave on) persist
those edits straight to the original file. That's the magic, but it also means it
can change important documents without a confirmation step — double-check before
you let it loose on anything you can't afford to lose. If you don't want that, open
the **⚙** menu and turn on **Read-only**: it switches the live session into `"plan"`
mode (Claude looks, reads, and answers — but edits and runs nothing), and while it's
on the overlay **denies every permission escalation the agent asks for** (including
`ExitPlanMode`), so read-only can't be talked out of; flip it back for full access.
(One nuance: the CLI refuses to elevate a session to `bypassPermissions` unless it was
*launched* in it — so when the overlay started read-only, flipping the toggle off lands
on `acceptEdits` instead, which the overlay's auto-approval makes effectively full
access; the in-chat notice always names the mode you actually got.)
To *start* locked on first launch, set `PERMISSION_MODE = "plan"` — after that the
toggle's last state is remembered per-machine, and a launch says so in-chat whenever
the remembered choice differs from the configured default. (`"acceptEdits"` / `"default"` are
of limited use here: a GUI with no terminal has nowhere to show a permission prompt,
so the overlay auto-answers them — see `worker._allow_tool`.)

**One thing the overlay reads that it previously didn't:** to show your plan allowance
before you've sent anything, it reads the OAuth access token the `claude` CLI already
stores in `~/.claude/.credentials.json` (on macOS: in your login keychain, read with the
same `security` tool the CLI itself uses) and sends it, once a minute, in a single
`GET https://api.anthropic.com/api/oauth/usage` — the same endpoint the CLI's own
`/usage` screen reads. That token is never stored by the overlay, never written to the
debug log, and never sent anywhere but `api.anthropic.com`; the request is a GET, so it
can't spend, change or send anything, and no token is ever refreshed (that stays the
CLI's job). Accounts authenticating another way — API key, Bedrock, Vertex, a gateway —
are skipped entirely, and every failure just leaves the gauge as it was. It's all in
[`usage.py`](../usage.py), which is short and commented for exactly this reason.
