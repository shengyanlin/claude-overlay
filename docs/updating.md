# Updating

[← Back to the README](../README.md)

This page uses the Windows file names. On a Mac the same things are `update.command`,
`setup.command`, `Diagnose.command` and `Claude Overlay.app`, and they behave the same way.

The overlay shows its version in the bottom status line (e.g. `v1.7.2`) and checks
GitHub for a newer release on startup.

## On its own, when you open it

On a `git clone` install, a newer release found at launch is installed **without a click**:
the chat says so, `update.cmd` runs in a console window (the pull, the packages, the check
that the new code starts), and the overlay restarts into the new version — usually within
a minute of opening it. It only ever does this at launch. If you've already started — sent a
message, resumed a conversation, typed something into the box, or attached a file or a
screenshot — it doesn't restart on you: the new version runs the next time you open the overlay, and a **↻ Restart into
vX.Y.Z** button is there if you want it sooner.

It steps aside, and shows the button below instead, when `update.cmd` would stop rather than
update: a clone that isn't on `main` or has local edits. If an update fails, the console
stays open on the reason, the chat offers a retry, and the next launch tries again. Set
`OVERLAY_AUTO_UPDATE` to `false` in `config.json` (or `CLAUDE_OVERLAY_AUTO_UPDATE=0`) to get
only the notice and its button.

## 🖱️ One click — the button in the chat

When the update isn't automatic, you'll see a 🔔 note and a `⬆` next to the version.

On a `git clone` install, the 🔔 notice comes with an **⬆ Update overlay to vX.Y.Z**
button. Clicking it runs `update.cmd` for you in a console window, so you can watch the
pull, the package refresh and the check that the new code still starts. You don't have to
close the overlay first, and that's the last thing you have to do: when the update lands,
the console closes itself and **the overlay restarts into the new code on its own** — the
fresh window offers to resume the conversation you were in.

If the update *fails*, nothing restarts. The console stays open on the error (that's where
the fix is written), and the button turns into **⚠ Update failed — click to retry**.

(Installed from the **ZIP**? There's no clone to pull into, so the notice gives you the
instructions below instead of a button.)

## 🖱️ One double-click — `update.cmd`

Double-click **`update.cmd`**. It pulls the latest release, refreshes the Python packages,
and — if you already have a Desktop shortcut — refreshes its icon to match the current version.

It pulls `main` from `upstream` if your clone has that remote and from `origin` otherwise, so
a **fork** gets the release rather than its own stale copy. It updates only a clean clone
sitting on `main`: on another branch, or with uncommitted changes, it says so and stops
instead of merging a release into work in progress.

## The Claude and Codex CLIs update themselves

The overlay drives the `claude` CLI (and, in Codex mode, the `codex` CLI), and those update
separately from the overlay — an up-to-date overlay on an old CLI quietly runs older models.
So every launch checks both, and one that's behind is updated on its own when npm installed
it (`npm install -g …@latest`); a native Claude install updates itself, so it's left alone.
The chat says what's happening. A CLI the overlay is using right now is paused for the update
— never in the middle of a reply — and comes back on the same conversation; what you type in
the meantime goes out right after. If an update can't finish (another Claude or Codex
window holding the CLI open, say), the chat says why and offers a one-click retry, and the
next launch tries again. Set `CLI_AUTO_UPDATE` to `false` in `config.json` (or
`CLAUDE_OVERLAY_CLI_AUTO_UPDATE=0`) to get only the notice and its **⬆ Update CLI** button.

## Updating to 1.25 — turning on Codex mode

1.25 adds the titlebar's **Claude | Codex** switch, which drives the Codex CLI. **The update
sets it up for you** (since 1.25.1): `update.cmd` — and the overlay's one-click Update —
asks to install the Codex CLI (Node.js too if there's no npm — into your user folder, no
admin needed, since 1.25.2; winget's admin-approved install is the fallback) and then offers
`codex login`. Say no and it won't ask again on later updates. Claude mode works exactly as
before either way.

Skipped it, or it didn't finish? If the setup was tried and failed, the update's window stays
open on the reason and the overlay says so when it restarts. An npm whose global folder the
overlay doesn't search (an `.npmrc` prefix, a Node.js not from the official installer) gets a
second try into your own `%APPDATA%\npm`. On a network that inspects HTTPS, npm is given the
certificates Windows already trusts, so it isn't stopped by `UNABLE_TO_GET_ISSUER_CERT_LOCALLY`.
Click **Codex** in the titlebar: while the CLI is missing
(or signed out) the overlay offers a one-click **Install Codex CLI & sign in** button — a
console window shows the progress — and Codex is clickable the moment it's done, no
restart. By hand it's `npm install -g @openai/codex` (needs Node.js 18+), then
`codex login`; re-running **`setup.cmd`** does both too. If you already use the Codex app
you're probably signed in — it shares the CLI's login.

## 🛠️ By hand

```
cd claude-overlay
git pull
```

(Installed via **ZIP** instead of `git clone`? Re-download the latest ZIP from the green
**Code** button and unzip **all** of it over the folder, replacing every file. The
overlay is a folder of modules, not a single script — replacing only `claude_overlay.py`
leaves it unable to start. Then double-click **`Diagnose.cmd`** to confirm it loads.)

> **Then restart the overlay.** It's a long-running process and does **not** reload
> while running — close it and re-open **`Start Claude Overlay.cmd`** for the update to
> take effect. Your conversation isn't lost: the relaunch offers a one-click
> **↺ Resume last conversation**, and Claude picks up right where you left off.
> (On a managed/enterprise machine, updating is what fixes the older
> versions that could hang on the first tool call.)
>
> Updated **by hand** (`git pull`) and the Desktop icon still looks old? Re-run
> **`Create Desktop Shortcut.cmd`** once — the shortcut is a machine-specific file that
> `git pull` can't refresh (`update.cmd` does this for you).
