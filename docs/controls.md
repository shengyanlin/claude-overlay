# Controls

[← Back to the README](../README.md)

On a Mac, `⌘` works wherever this page says `Ctrl` for paste, copy and zoom, and the
show / hide hotkey is `⌥⇧Space`. See [what's different on a Mac](macos.md#whats-different-on-a-mac).

| Action | How |
|---|---|
| Send message | `Enter` (or click the **↑** button) |
| Type ahead while Claude answers | just keep going — `Enter` mid-reply **queues** the message (like the Claude Code CLI) instead of interrupting; queued messages show above the box and go out one per finished reply, in order. Each row's **✕** hands its text back to the box |
| New line | `Shift+Enter` |
| Stop a running reply | click **Stop** (the ↑ becomes ■ while busy) or press `Esc` — either also drops the queued line-up, listing the texts in the transcript and putting the first back in the box |
| Paste an image | **Ctrl+V** (or `Shift+Insert`) |
| Attach a file | **⚙ → Attach files…** — images, PDF, `.docx`, `.pptx`, multi-select allowed |
| See what's attached | a strip above the input box, one row per file with its name and size. Files that couldn't be attached sit there too, in red, each saying why |
| Remove one attachment | that row's **✕** |
| Clear them all | **Clear all** at the right of the strip |
| Toggle auto-screenshot | **◉ / ○ Auto-shot** (orange = on) |
| Settings menu | click **⚙** — Window-only, Shareable, Read-only, Animated icon (✓ = on), then **Attach files…** and **Past conversations…**; the gear turns orange while Read-only is on |
| &nbsp;&nbsp;• Capture only the active window | **⚙ → Window-only** (window only; off = every monitor) |
| &nbsp;&nbsp;• Show / hide in screen shares | **⚙ → Shareable** (visible to Teams/Zoom/OBS; off = private, the default — on a Mac that is best effort, see the [macOS guide](macos.md#whats-different-on-a-mac)) |
| &nbsp;&nbsp;• Lock Claude read-only | **⚙ → Read-only** ("plan" mode: looks and answers, changes nothing; off = the configured `PERMISSION_MODE`) |
| &nbsp;&nbsp;• Animate the collapsed icon | **⚙ → Animated icon** (on by default, remembered). Collapsed, Clawd blinks, looks towards your pointer, and now and then nods, dances, bounces or plays peekaboo; he waves when the pointer arrives, shuffles while a reply is running, cheers when it lands and naps after three quiet minutes. Off = the still icon. Nothing runs while the overlay is expanded |
| Switch model | click the **statusline** (`model ▾`) — the list shows the model families **your** login can actually pick, read from the CLI's own record of them, so it can't offer you a model that would silently fall back to another one |
| Change how hard it thinks | click the statusline's **`⚡ auto ▾`** — Low / Medium / High / Extra high / Max, **Auto** (no fixed level: the model decides per request), or Default (your CLI's own `effortLevel`). Ships at **Auto**: the overlay is for quick turns, and at an inherited `xhigh` a one-line rewrite took 14–24s against ~2s at Auto with the same answer — while a hard question can still get the thinking it needs. Pin a level when you want one. Switching is instant and keeps the conversation (it is the CLI's own `/effort` command); only **Default** relaunches Claude, resuming the conversation (~10s). Your choice is remembered across launches |
| Glance the allowance | the ring on the titlebar **✻** — inner arc is the 5-hour window, outer is weekly, amber near the limit, red past it. **Hover** it for the numbers: both windows, reset times, and roughly how many turns of context are left |
| See how much allowance is left | the statusline's `5h 61%` — the window is **named**, so you always know whether you're looking at the 5-hour allowance or a weekly one, and whichever you'll hit first is the one shown. Filled in from your account the moment the overlay opens, so it's there **before** you send anything, and refreshed every minute while it sits idle; amber as you approach a limit, red once it's gone, and the reset time joins it only once that's worth planning around. It speaks up once per transition, and a message refused for allowance is put back in the box rather than lost |
| Retry when the allowance returns | a refused message offers **⏱ Send it automatically at &lt;time&gt;** — opt-in, one click, and it stands down the moment you type something else, send by hand, or Clear |
| See how much context is left | the statusline's `context 72%` — its own segment, beside the allowance rather than competing with it: the allowance is your plan, context is the size of *this* conversation, and Clear or Compact hands context back while the spend stays spent. A note at 70% and again at 85% says when compacting is worth it |
| Zoom text in / out | **Ctrl +** / **Ctrl −** (or **Ctrl + mouse-wheel**); **Ctrl 0** resets |
| New conversation | **Clear** |
| Compact the conversation (free up context) | **Compact** — summarizes older turns, keeps going |
| Copy a reply | click **⧉ Copy** under the message |
| Switch between Claude and Codex | the **Claude \| Codex** pill in the titlebar — swaps the agent *and* the look in place (Codex's blue-violet on cool whites — or slate in the dark theme — its `>_` mark, an indigo send button), no restart. It only switches an **empty** conversation — once something has been asked, Clear first (one agent can't continue the other's); what you've typed but not sent comes along, and the choice is remembered for the next launch. If only one of the two CLIs is installed, the other side of the pill is struck through and clicking it says how to install it. Everything else works the same in Codex mode, with Codex's own pieces behind it: the model menu lists **your** Codex models, ⚡ offers the levels the current model supports (Auto = that model's own default), the ring and the allowance notices/auto-retry run on Codex's allowance (re-read every minute), Past conversations lists your Codex threads (✕ archives one), a signed-out Codex is announced and holds your message, PDFs go along as their text, the overlay's MCP settings apply, and the one-click CLI update updates Codex. When Codex wants to step outside its sandbox on a PC whose Codex policy requires approvals, Codex's own auto-review decides, so you aren't asked (a refusal shows in the chat with its reason); in Read-only, or with `CODEX_APPROVALS_REVIEWER` set to `user`, an **Allow / Allow for this session / Deny** card asks you instead. Claude skills are Claude-only. The collapsed orb stays Clawd either way |
| Name this overlay | click the title (**Claude**) — handy with several open |
| Collapse to a Claude orb | **–**, or double-click the title bar |
| Expand from the orb | click the orb (drag it to move) |
| Quit | **✕** |
| Move | drag the title bar |
| Resize | drag **any edge or corner** (or the **◢** grip) |
