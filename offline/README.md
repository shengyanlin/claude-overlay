# `offline/` — for machines whose proxy refuses the download

Nothing here is needed on a normal machine. This folder exists for one situation, and it is
a common one on managed corporate laptops:

> `setup.cmd` says it could not install Python, and the reason it prints is an **HTTP 403**
> (or any other refusal) rather than "no network".

That is a policy decision made by a proxy or an endpoint-security product, not something a
script can talk its way past. Two things make it go away, and neither needs the blocked
download to start working.

## A. Pre-stage `uv` (best if more than one machine is affected)

On **any** PC that can reach GitHub — a home machine, a phone tethering, a colleague's
laptop — download the `uv` archive for the target architecture:

| Target | File |
|---|---|
| 64-bit Intel/AMD (almost everything) | [`uv-x86_64-pc-windows-msvc.zip`](https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip) |
| Windows on ARM | [`uv-aarch64-pc-windows-msvc.zip`](https://github.com/astral-sh/uv/releases/latest/download/uv-aarch64-pc-windows-msvc.zip) |
| 32-bit Windows (rare) | [`uv-i686-pc-windows-msvc.zip`](https://github.com/astral-sh/uv/releases/latest/download/uv-i686-pc-windows-msvc.zip) |

Match the file to the **target** machine's architecture — a 64-bit `uv.exe` staged for a
32-bit Windows downloads fine and then will not run, which looks like a broken archive.
(Only `.zip` archives are used; the `.tar.gz` uv publishes for other platforms is ignored.)

Drop the `.zip` **in this folder**, then double-click `setup.cmd` again.

`install-python.ps1` looks here **before** it tries the network, so this path is not a
fallback that only gets exercised on the day it is needed — it is the one that runs. Any
file matching `uv*.zip` is accepted (newest wins), so replacing the file is how you upgrade.

`uv` then downloads CPython itself. If *that* is also refused, point it at an internal
mirror of [python-build-standalone](https://github.com/astral-sh/python-build-standalone)
and re-run:

```
setx UV_PYTHON_INSTALL_MIRROR https://your-internal-mirror/example
```

`install-python.ps1` honours that variable and never overwrites it, so it is safe to hand to
a whole fleet.

## B. Bring your own Python (works with no network at all)

Put any **Python 3.10+ that includes tkinter** into:

```
%LOCALAPPDATA%\Programs\Python\
```

That is it. The launcher, `setup.cmd`, `update.cmd` and `Diagnose.cmd` all *scan* that
folder and use whatever actually runs there — they do not care which installer produced it,
where it came from, or who signed it. A zipped copy of a working install from another
machine is enough, and so is anything your IT department provides.

Check it worked by double-clicking `Diagnose.cmd`.

## Why the normal installers fail on some corporate machines

Two independent blocks, both observed on a managed Windows laptop in August 2026:

- **BeyondTrust/Avecto Privilege Management** refuses any Python-Software-Foundation-signed
  `python.exe`. The rule matches the **signature**, not the filename, so renaming changes
  nothing — and it covers the python.org installer, the Microsoft Store build *and* the
  official embeddable zip, i.e. every artifact the normal routes can produce. The CPython
  `uv` installs is Astral's python-build-standalone, which is not PSF-signed and runs fine.
- **Zscaler** answers `403` for `.exe` downloads from python.org while allowing `.zip` and
  GitHub-release downloads, so the normal installer cannot even be fetched.

Neither is universal. Measured on two machines in the same company, the *same* python.org
URL returned 403 on one and 200 on the other — policy is per-user/per-group. So treat the
routes above as the reliable ones and the downloads as a convenience that may or may not be
available to you.
