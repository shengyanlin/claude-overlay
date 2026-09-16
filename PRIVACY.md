# Privacy

Claude Overlay can report anonymous usage counts — how many people use it and which
version they run. This page says exactly what that means, in the order you'd ask.

**Status in this release: nothing is sent.** No endpoint is configured (`TELEMETRY_URL`
in `config.py` is empty), so every build published so far reports nothing at all. The
rest of this page describes what happens once a release configures one.

**This is opt-out, not opt-in.** There is no first-run dialog asking permission — once an
endpoint exists, a launch reports unless you've told it not to. That is a deliberate
choice, made in the open: this page, and the "How to turn it off" section below, are the
disclosure in place of a dialog. If that trade isn't one you want, turn it off before
upgrading — the settings below work whether or not you've ever launched the version that
added this.

## The short version

- It is **on by default once an endpoint is configured** (still nowhere, today). Turn it
  off with `DO_NOT_TRACK=1` or `CLAUDE_OVERLAY_TELEMETRY=0` — see below.
- It is **at most one HTTP request per launch**, carrying four short values.
- That request contains **nothing you typed, nothing the model said, and no
  screenshots**.
- You can read everything that runs **on your machine** in five minutes, and it is three
  places, not one: [`telemetry.py`](telemetry.py) (the payload, the checks, the request),
  `Overlay._ping_telemetry` and `Overlay._install_id` in
  [`claude_overlay.py`](claude_overlay.py) (when it is attempted, the id), and
  `TELEMETRY` / `TELEMETRY_URL` in [`config.py`](config.py) (whether there is an endpoint
  at all). The server that receives it is **not** published — see "Where it goes".

## What is sent

At most one `GET` per launch. The values travel in the query string rather than a request
body specifically so that you can paste the URL into a browser and see what leaves:

| Field | Meaning | Example |
|---|---|---|
| `v` | the overlay version | `1.22.1` |
| `id` | a random install id | `3f2a…` (a uuid4) |
| `os` | the OS build | `10.0.26100` |
| `py` | the Python version, major.minor | `3.14` |

Those are the four **telemetry parameters**, and a test pins the key set so a fifth
cannot arrive quietly. Two other things ride along, listed here so the accounting is
complete rather than flattering:

- a fixed **`User-Agent: claude-overlay`** header — chosen deliberately over urllib's
  default, which would announce your Python version to every proxy between you and the
  endpoint;
- whatever **query a self-hosted `TELEMETRY_URL` carries of its own** (`?src=…`, say),
  which belongs to whoever configured it, not to the app.

Everything else about a URL that could redirect the data or quietly break it is refused:
no `http`, no credentials, no `#fragment`, no invalid port, and a redirect is never
followed.

**The install id** is a `uuid4` — random — generated on first use and stored on your
machine in `%LOCALAPPDATA%\claude-overlay\state.json`. It is **not** derived from your
MAC address, hostname, username, or anything else about your machine: it identifies a
copy of the app and cannot be worked backwards to a person. A value in that file that
isn't a version-4 uuid is replaced rather than sent, which is also what stops a `uuid1`
(that kind *can* embed a MAC address) from being reported as if it were random. Delete
the id, or the file, and the next launch mints a new one, which simply reads as a new
install.

## What is never sent

Not in the request: anything you type. Anything the model replies. Screenshots, or any
part of your screen. File names, file contents, or paths. Your username, hostname, or
working directory. Which model you use, what you asked it, or how long you used it.
Nothing the agent read.

**The wording matters here.** Obviously the *app* handles your prompts and your screen —
that is what it is for, it screenshots your desktop on every message. What this page can
tell you is that none of that is reachable from the reporting path: it is built by one
function that takes a version string and an id, and no code in it touches anything else.
The payload is four short scalars rather than a nested object for exactly this reason —
the claim stays checkable at a glance, by you, in a file you can open.

What the app does with your prompts and screenshots *otherwise* is a different question
with a different answer, and it is the README's, not this page's: they go to Anthropic as
part of the conversation, exactly as they do in the Claude Code CLI this drives.

## What is not promised

Everything above this line is a property of code you can read in this repository.
Everything in this section is not, and the distinction is the point of the section.

**Your IP address reaches the server the moment the connection opens.** That is how TCP
works, and no code here changes it. The *intended* policy for the author's endpoint is
not to store or log it — but that is a statement of intent about a server, not a
guarantee this repository can enforce, and you cannot verify it from here. A hosting
provider or CDN also sits in the path and necessarily processes the connection under its
own terms.

**There is no deployed endpoint yet.** No published build has sent anything, so at the
time of writing there is nothing to store and nothing to disclose. When an endpoint
exists, this page will be updated to name it and its retention, and this paragraph will
say so plainly instead.

**If `TELEMETRY_URL` points somewhere else, none of this applies.** A self-hosted or
organisation-run collector is operated by whoever set it, under their logging and
retention policies, not the author's. The client-side guarantees above — four fields,
https only, no redirects — still hold, because they are enforced here. Nothing about
what happens after the request arrives does.

Three other honest limits:

- These numbers are a **floor, never a true count**. A ping that a corporate proxy blocks
  is simply lost — it is never retried, and there is no way to tell a blocked user from
  an absent one. A launch shorter than about two seconds never reaches the point where
  one is attempted, and quitting mid-request kills it too.
- Launch counts include crash loops and quick relaunches. Only the distinct install ids
  approximate "people".
- **Forks and development clones report too.** `TELEMETRY_URL` is a committed constant
  and this project is installed by `git clone`, so there is no way to tell an ordinary
  install from somebody hacking on the code or running a fork — they are the same act.
  A fork that wants its launches somewhere else, or nowhere, changes one line in
  `config.py`.

## Where it goes

To an endpoint run by the author (`shengyanlin`), named by `TELEMETRY_URL` in
`config.py` — currently empty, so currently nowhere.

**The server's source is not published**, and you should read the rest of this section
knowing that. What it does, described rather than shown: about seventy lines that accept
one request shape and increment one counter, writing to a table that holds one row per
install per **day** rather than one row per ping — so there is no record of *when* you
launched the app, only that you did at some point that day. It has no read path, so the
endpoint itself cannot be used to count or enumerate anything.

That paragraph is a description you cannot verify, which is a real step down from the
rest of this page — everything above it is a claim about code in this repository that
you can go and read. The honest summary is: **what leaves your machine is auditable;
what happens to it afterwards is not.** If that is not a trade you want, the settings
below turn it off and they are enforced on your side, not the server's.

The undertaking, for when one exists: it is not sold, not shared with anyone else, and
not fed to any third-party analytics product; the aggregate numbers are not published.
The unavoidable exception is the hosting provider that serves the endpoint, which
processes the request in order to receive it at all.

If you would rather have your own numbers than send anyone else's anywhere — a company
deploying this internally, say — `TELEMETRY_URL` is overridable per machine (below), so
you can point it at your own collector. See the note above about what that changes.

## How to turn it off

Any one of these is enough, and each one is checked fresh on every launch — so switching
it off takes effect on the next start, not the next reinstall:

1. **`DO_NOT_TRACK=1`** in your environment. The standard variable is honoured, and it
   wins over everything else.
2. **`CLAUDE_OVERLAY_TELEMETRY=0`** in your environment, or `{"TELEMETRY": false}` in
   `%LOCALAPPDATA%\claude-overlay\config.json`.
3. Setting `"telemetry_consent": false` by hand in
   `%LOCALAPPDATA%\claude-overlay\state.json`. There is no settings toggle that writes
   this today — the field exists for whichever comes first, a future toggle or your own
   edit — but a `false` there is honoured exactly like the other two.

There is also a hard floor you don't have to configure: the endpoint must be `https`, or
the ping is refused rather than sent in clear text, and a redirect is never followed —
the install id goes to the host written in `config.py` or nowhere.

## Questions

Open an issue at <https://github.com/shengyanlin/claude-overlay/issues>.
