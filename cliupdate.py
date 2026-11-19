# -*- coding: utf-8 -*-
"""Detect an out-of-date `claude` CLI and run a one-click, in-overlay update.

WHY THIS EXISTS
The overlay is a thin GUI over the `claude` CLI (driven through the Agent SDK). Which
models exist — and what a family alias like `opus` resolves to — is decided by the CLI
(and the backend), NOT the overlay: see modelresolve.py's alias-lag note. So an overlay
that's fully up to date but a CLI that's months behind silently runs an OLDER model, or
can't reach a brand-new one at all. And the CLI's own auto-updater does NOT run for a
global-npm install (the overlay's assumed setup — it needs %APPDATA%\\npm on PATH to find
`claude`), so the CLI can sit many versions behind indefinitely with no prompting.

WHAT
`check_update()` compares the installed CLI version against the latest published on the
npm registry and returns whether it's behind. When it is, the overlay shows a one-line
notice with an "Update" button; clicking it calls `run_update()`, which runs
`npm install -g @anthropic-ai/claude-code@latest`. Nothing is ever done silently or
without a click.

SAFETY / COST (mirrors modelresolve.py's doctrine)
Leaf module: stdlib only (modelresolve is imported lazily, inside a function, purely to
reuse its tested `claude -v` probe). Every function degrades to None / (False, reason) on
ANY failure (no npm, offline, corporate proxy block, odd output) so a check can neither
block nor break startup. The npm-registry lookup and `claude -v` are node cold starts of a
few seconds, so callers run this off the UI thread AND it's throttled to once/day via a
cache in ~/.claude-overlay — most launches read one small file and spawn nothing.

WINDOWS
npm ships as `npm.cmd`, a batch shim that CreateProcess can't exec directly, so every npm
invocation goes through `cmd /c` (bare `npm`, resolved off PATH — avoids quoting the space
in a path like "C:\\Users\\Lin Jason\\..."). CREATE_NO_WINDOW keeps a console from flashing
under pythonw. `npm view` prints the version to STDOUT and its "new npm available" nags to
STDERR, so the version is parsed from stdout ONLY (a stderr nag can contain a stray x.y.z)."""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

_PKG = "@anthropic-ai/claude-code"
_CACHE_PATH = Path.home() / ".claude-overlay" / "cli_update_cache.json"
# The Codex side of the titlebar switch: same check, same one-click update, its own package
# and cache. Every public function takes cli="claude" | "codex" and defaults to Claude, so
# existing callers (and their tests, which patch the module globals) are unchanged.
_CODEX_PKG = "@openai/codex"
_CODEX_CACHE_PATH = Path.home() / ".claude-overlay" / "codex_update_cache.json"
_CHECK_INTERVAL_S = 24 * 60 * 60          # re-query the registry at most once/day
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_NPM_VIEW_TIMEOUT = 25                     # registry lookup (cache miss only)
_INSTALL_TIMEOUT = 300                     # `npm install -g` (download + extract)
_REASON_MAX = 200                          # cap a surfaced failure reason


def npm_prefix(cli="claude"):
    """The npm global folder the `claude` / `codex` on PATH runs from — the folder holding the
    shim npm made, with the package beside it in node_modules — or None when it isn't an npm
    global install: a native install (Claude's ~/.local/bin/claude.exe updates itself) or any
    other installer. run_update installs into exactly this folder (--prefix): npm's own
    configured prefix may have moved since (an .npmrc), and an update landing there would add
    a copy that never runs — the CLI would look behind, and be updated, at every launch."""
    pkg = _CODEX_PKG if cli == "codex" else _PKG
    exe = shutil.which("codex" if cli == "codex" else "claude")
    if not exe or os.path.splitext(exe)[1].lower() not in (".cmd", ".bat", ".ps1", ""):
        return None
    folder = os.path.dirname(exe)
    if os.path.isdir(os.path.join(folder, "node_modules", *pkg.split("/"))):
        return folder                    # Windows layout: the shim sits beside node_modules
    if sys.platform == "win32":
        return None                      # ...and it is the only one npm has there
    # Unix layout: <prefix>/bin/<cli> is a symlink into <prefix>/lib/node_modules/<pkg>.
    # Anything that doesn't resolve into such a folder (Claude's native install is a
    # symlink into ~/.local/share/claude) isn't npm's to update.
    marker = os.sep + os.path.join("lib", "node_modules", *pkg.split("/")) + os.sep
    real = os.path.realpath(exe)
    return real[:real.index(marker)] if marker in real else None


def npm_managed(cli="claude"):
    """True when npm's global install is the CLI that runs (see npm_prefix)."""
    return npm_prefix(cli) is not None


def _find_npm():
    """Path to the npm launcher if npm is on PATH, else None — a cheap gate so we never even
    try to check/update when npm isn't installed (a native-installer setup, say). shutil.which
    honours PATHEXT, so it finds npm.cmd on Windows."""
    return shutil.which("npm")


_ca_pem = None                             # the trusted-roots file, once written this run


def _windows_ca_pem():
    """Path of the PEM file of the roots Windows trusts for HTTPS (windowsca.py — the same
    file install-codex.cmd writes), or None. A network that inspects HTTPS re-signs every
    site with the organisation's own root, which Windows trusts and Node.js's built-in list
    doesn't, so npm fails with UNABLE_TO_GET_ISSUER_CERT_LOCALLY (a field report on 1.25.2).
    Written once a run (rewritten if it went missing); imported lazily so a broken windowsca
    can't cost us the module. On a Mac it is _mac_ca_pem's file, under the same once-a-run
    rule."""
    global _ca_pem
    if _ca_pem == "":                    # couldn't be written this run: don't keep trying
        return None
    if _ca_pem and os.path.isfile(_ca_pem):
        return _ca_pem
    _ca_pem = ""
    if sys.platform == "darwin":
        _ca_pem = _mac_ca_pem() or ""
        return _ca_pem or None
    if sys.platform != "win32":
        return None
    try:
        import windowsca
        _ca_pem = windowsca.write() or ""
    except Exception:
        pass
    return _ca_pem or None


_SECURITY = "/usr/bin/security"            # by path: a trust decision shouldn't ride on PATH
_MAC_CA_BUDGET = 10                        # seconds, for one run's `security` calls together
_PEM_CERT = re.compile(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", re.S)


def _is_root_ca(der):
    """True for a self-issued CA certificate that OpenSSL — Node.js's reader — can load: the
    only kind worth handing over as an extra CA. macOS also verifies an ordinary certificate
    issued under a trusted root (this Mac's own identity in the System keychain, say), and
    that is not a root to trust anything by."""
    import ssl
    try:
        # truststore (config.py) swaps ssl.SSLContext for a subclass that refuses to list
        # what it loaded; the real class is the one right under it.
        real = next(c for c in ssl.SSLContext.__mro__ if c.__module__ == "ssl")
        ctx = real(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cadata=der)
        cas = ctx.get_ca_certs()         # lists CA certificates only
        return len(cas) == 1 and cas[0]["subject"] == cas[0]["issuer"]
    except Exception:
        return False


def _mac_trusts_for_ssl(pem_file, timeout=_MAC_CA_BUDGET):
    """Whether macOS itself accepts the certificate in this PEM file for an HTTPS server.
    A keychain only STORES certificates: one that sits there with no trust granted, or set to
    "Never Trust", is not trusted, and `find-certificate` returns it all the same. So each
    one goes through the system's own evaluation, which applies the admin's and the user's
    trust settings for the SSL policy. -l: the certificate is a CA, not a site's; -L: nothing
    is fetched from the network. Raises when `security` can't run or doesn't answer in time."""
    r = subprocess.run([_SECURITY, "verify-cert", "-c", str(pem_file), "-p", "ssl",
                        "-l", "-L", "-q"],
                       capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    return r.returncode == 0


def _mac_trusted_roots():
    """DER of each root CA in the user's keychains that macOS trusts for HTTPS, in keychain
    order. The default search list — login and System, where an organisation's or the user's
    own root is installed — and not Apple's SystemRootCertificates: Node.js brings the public
    roots itself, and NODE_EXTRA_CA_CERTS only adds to them. Raises on any failure, a timeout
    included, so the caller writes every trusted root or no file — never a partial one."""
    import ssl
    import tempfile
    deadline = time.monotonic() + _MAC_CA_BUDGET

    def left():
        t = deadline - time.monotonic()
        if t <= 0:
            raise TimeoutError("security")
        return t

    r = subprocess.run([_SECURITY, "find-certificate", "-a", "-p"], capture_output=True,
                       timeout=left(), stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise OSError("security find-certificate")
    roots, seen = [], set()
    with tempfile.TemporaryDirectory(prefix="claude-overlay-ca-") as tmp:
        one = os.path.join(tmp, "cert.pem")
        for block in _PEM_CERT.findall(r.stdout.decode("ascii", "replace")):
            try:
                der = ssl.PEM_cert_to_DER_cert(block)
            except Exception:
                continue
            if der in seen or not _is_root_ca(der):
                continue
            seen.add(der)
            with open(one, "w", encoding="ascii", newline="\n") as f:
                f.write(ssl.DER_cert_to_PEM_cert(der))
            if _mac_trusts_for_ssl(one, left()):
                roots.append(der)
    return roots


def _mac_ca_pem():
    """The macOS counterpart of windowsca: one PEM file of the roots the organisation or the
    user installed AND this Mac trusts for HTTPS (see _mac_trusts_for_ssl) — an
    HTTPS-inspecting proxy's root is one of those. A current Node.js reads the keychain itself
    (NODE_USE_SYSTEM_CA) and takes these same roots from it, so the file adds nothing there;
    it is for a Node.js too old for that, and is written either way rather than guess from a
    version number which Node npm will run under. Read-only `security` calls. None — and no
    file left behind, an earlier run's included — when nothing is trusted or anything fails."""
    try:
        import config
        path = config._data_home() / "claude-overlay" / "mac-ca.pem"
    except Exception:
        return None
    try:
        import ssl
        import tempfile
        pem = "".join(ssl.DER_cert_to_PEM_cert(der) for der in _mac_trusted_roots())
        if pem:
            path.parent.mkdir(parents=True, exist_ok=True)
            # A file of its own, then os.replace (as windowsca): an npm reading this moment
            # sees the old file or the new one, never half of one.
            fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".partial",
                                       dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="ascii", newline="\n") as f:
                    f.write(pem)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            return str(path)
    except Exception:
        pass
    try:
        path.unlink()                    # what it held may no longer be trusted
    except OSError:
        pass
    return None


def _npm_env():
    """The environment npm runs in: this one, plus the system's certificates for Node.js —
    NODE_USE_SYSTEM_CA for a Node.js that reads the store itself, NODE_EXTRA_CA_CERTS (see
    _windows_ca_pem; on a Mac, _mac_ca_pem) for one that doesn't. The file holds only roots
    the system trusts for HTTPS, so it grants a current Node.js nothing its own reading of the
    store doesn't. Nothing is switched off: npm still checks every certificate. Either
    variable already set is kept."""
    env = dict(os.environ)
    env.setdefault("NODE_USE_SYSTEM_CA", "1")
    if not env.get("NODE_EXTRA_CA_CERTS"):
        pem = _windows_ca_pem()
        if pem:
            env["NODE_EXTRA_CA_CERTS"] = pem
    return env


_CMD_SPECIAL = re.compile(r"[\s&|<>^()\[\]{}=;!'+,`~\"]")


def _cmdline(args):
    """The arguments as cmd.exe reads them. A list would go through Windows' own quoting,
    which quotes only for spaces, so an argument with an & in it would reach cmd bare and end
    the command there. Any argument with a character cmd treats specially is double-quoted;
    the command itself (npm, codex) comes first unquoted, so cmd /c keeps every quote as
    written. (A %NAME% still expands inside quotes — so a folder goes in env_extra, not here.)"""
    return " ".join(f'"{a}"' if (not a or _CMD_SPECIAL.search(a)) else a for a in args)


def _run(args, timeout, env_extra=None):
    """Run `cmd /c <args>` capturing stdout+stderr as decoded UTF-8. Going through cmd.exe is
    required on Windows to launch npm (a .cmd shim); CREATE_NO_WINDOW suppresses the console
    under pythonw. `env_extra` is added to the environment as is — cmd never parses it.
    Returns (returncode, stdout, stderr) or None if it couldn't even spawn. Never raises."""
    try:
        env = _npm_env()
        env.update(env_extra or {})
        if sys.platform == "win32":
            r = subprocess.run("cmd /c " + _cmdline(args), capture_output=True,
                               timeout=timeout, env=env, creationflags=_CREATE_NO_WINDOW)
        else:
            # No shell needed (or wanted) off Windows: npm and codex are executables there,
            # and an argument list reaches them exactly as written.
            exe = shutil.which(args[0], path=env.get("PATH"))
            if not exe:
                return None
            r = subprocess.run([exe, *args[1:]], capture_output=True, timeout=timeout,
                               env=env, stdin=subprocess.DEVNULL)
    except Exception:
        return None
    dec = lambda b: (b or b"").decode("utf-8", "replace")
    return (r.returncode, dec(r.stdout), dec(r.stderr))


def _parse_ver(s):
    """'2.1.198' -> (2, 1, 198) for a correct numeric compare ((2,1,198) < (2,1,204)). Caps each
    field's digits so a hostile 'version' can't hit Python's int-from-string limit; missing ->
    (0,) so an unreadable version sorts oldest (never spuriously 'ahead')."""
    nums = re.findall(r"\d+", str(s or ""))
    return tuple(int(n[:9]) for n in nums[:3]) if nums else (0,)


def installed_version():
    """The installed CLI's version string (e.g. '2.1.198') or None. Reuses modelresolve's tested
    `claude -v` probe (imported lazily) so there's ONE source of truth for what the CLI reports."""
    try:
        import modelresolve
        return modelresolve.cli_version()
    except Exception:
        return None


def codex_version():
    """The installed `codex` CLI's version ('0.160.1') or None. `codex --version` prints
    "codex-cli 0.160.1"; through cmd /c because the npm install is a .cmd shim."""
    res = _run(["codex", "--version"], _NPM_VIEW_TIMEOUT)
    if not res or res[0] != 0:
        return None
    m = re.search(r"\b(\d+\.\d+\.\d+)\b", res[1])
    return m.group(1) if m else None


def _npm_latest(pkg=None):
    """The latest published version of the package on the npm registry (the exact version
    `npm install -g <pkg>@latest` would install), or None. Parses STDOUT only — npm's upgrade
    nags go to stderr and could otherwise be mis-read as the package version."""
    res = _run(["npm", "view", pkg or _PKG, "version"], _NPM_VIEW_TIMEOUT)
    if not res:
        return None
    rc, out, _err = res
    if rc != 0:
        return None
    m = re.search(r"\b(\d+\.\d+\.\d+)\b", out)
    return m.group(1) if m else None


def _load_cache(path=None):
    try:
        c = json.loads((path or _CACHE_PATH).read_text("utf-8"))
        return c if isinstance(c, dict) else None
    except Exception:
        return None


def _save_cache(result, path=None):
    """Persist a check result with a timestamp so the (slow) registry lookup is skipped for
    _CHECK_INTERVAL_S. Best-effort — a write failure just means we re-check next launch."""
    try:
        path = path or _CACHE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ts": time.time(), "result": result}), "utf-8")
    except Exception:
        pass


def _fresh_cache(path=None):
    """The cached check result if it's younger than _CHECK_INTERVAL_S and well-formed, else None."""
    c = _load_cache(path)
    if not c:
        return None
    ts = c.get("ts")
    if not isinstance(ts, (int, float)) or (time.time() - ts) > _CHECK_INTERVAL_S:
        return None
    r = c.get("result")
    if isinstance(r, dict) and {"installed", "latest", "behind"} <= set(r):
        return r
    return None


def _spec(cli):
    """(package, cache path, installed-version fn, latest-version fn) for a CLI. Looked up
    at call time, so a test that patches the module's Claude globals still sees them."""
    if cli == "codex":
        return _CODEX_PKG, _CODEX_CACHE_PATH, codex_version, lambda: _npm_latest(_CODEX_PKG)
    return _PKG, _CACHE_PATH, installed_version, _npm_latest


def check_update(force=False, cli="claude"):
    """Decide whether the installed CLI is behind the latest npm release.

    Returns {"installed": "2.1.198", "latest": "2.1.204", "behind": True} — or None on ANY
    failure (no npm, offline, proxy block, unparseable output) so the caller simply shows no
    notice. Throttled: a cache newer than _CHECK_INTERVAL_S short-circuits the slow node/npm
    calls (pass force=True to bypass, e.g. right after an update)."""
    if _find_npm() is None:
        return None
    _pkg, cache, installed, latest_of = _spec(cli)
    if not force:
        cached = _fresh_cache(cache)
        if cached is not None:
            return cached
    inst = installed()
    if not inst:                 # not installed (no Codex CLI, say): no registry call either
        return None
    latest = latest_of()
    if not latest:
        return None
    result = {"installed": inst, "latest": latest,
              "behind": bool(_parse_ver(inst) < _parse_ver(latest))}
    _save_cache(result, cache)
    return result


def _failure_reason(rc, out, err):
    """A short, USER-FACING reason for a failed `npm install -g`. Two Windows realities drive this:

    (1) The COMMON, structural failure is EBUSY / EPERM: the `claude.exe` npm is trying to replace
        is LOCKED because a Claude process is running — most often the overlay's OWN worker (the
        window hosting this button is itself running `claude`), but also any other Claude Code
        window. Windows can't overwrite a running exe. So detect it and say so ACTIONABLY, rather
        than leaking npm's raw error.
    (2) Otherwise, npm's LAST stderr line is the useless "A complete log of this run can be found
        in: …" pointer — skip it and surface the real error line, trimming the "npm error " prefix."""
    blob = (err or "") + "\n" + (out or "")
    low = blob.lower()
    if any(k in low for k in ("ebusy", "resource busy or locked", "eperm", "text file busy")):
        return ("a running Claude process is using the CLI, so it can't be replaced — close other "
                "Claude windows (including this overlay) and try again")
    if "eacces" in low or "permission denied" in low:
        return "permission denied writing the global npm folder"
    for line in reversed(blob.splitlines()):
        s = line.strip().rstrip(".")
        if not s or "a complete log of this run" in s.lower():
            continue
        s = re.sub(r"^npm (error|err!)\s*", "", s, flags=re.IGNORECASE).strip()
        if s:
            return s[:_REASON_MAX]
    return f"npm exited with code {rc}"


def run_update(cli="claude"):
    """Run `npm install -g @anthropic-ai/claude-code@latest`.

    Returns (True, new_version) on success — after which the cache is refreshed to not-behind
    so the notice won't re-fire — or (False, short_reason) on failure. Never raises; the caller
    can always fall back to running the npm command by hand."""
    if _find_npm() is None:
        return (False, "npm not found on PATH")
    pkg, cache, installed, _latest = _spec(cli)
    args = ["npm", "install", "-g", pkg + "@latest"]
    prefix = npm_prefix(cli)
    # Replace the copy that runs (see npm_prefix). As npm_config_prefix — npm's own name for
    # the same setting, above any .npmrc — not --prefix: cmd.exe never parses an environment
    # value, so a folder with &, ( or a literal %NAME% in it reaches npm exactly as it is.
    res = _run(args, _INSTALL_TIMEOUT,
               **({"env_extra": {"npm_config_prefix": prefix}} if prefix else {}))
    if not res:
        return (False, "couldn't launch npm")
    rc, out, err = res
    if rc != 0:
        reason = _failure_reason(rc, out, err)
        if cli == "codex":
            reason = reason.replace("a running Claude process", "a running Codex process "
                                    "(the Codex app, or a terminal running codex)")
        return (False, reason)
    new = installed() or "latest"
    _save_cache({"installed": new, "latest": new, "behind": False}, cache)
    return (True, new)
