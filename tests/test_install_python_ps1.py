"""Tests for install-python.ps1 -- the helper setup.cmd calls when this PC has no Python.

It grew a third strategy (uv) for a machine where the first two are both blocked by device
policy: an endpoint-security product refuses PSF-signed python.exe *by signature*, so the
python.org installer, the Microsoft Store build and the official embeddable zip are all
refused alike, and a proxy answers 403 for .exe downloads from python.org so the installer
cannot even be fetched. uv's own exe arrives in a GitHub-release .zip and the CPython it
installs is not PSF-signed, so that route survives.

The invariants below are mostly about NOT assuming an environment, because this file runs on
whatever Windows a stranger has, not on the author's:

  * a refused download is treated as ordinary, not exceptional, so the routes that need no
    download come first and the give-up screen leads with them;
  * curl.exe is preferred but never required (Windows 10 before 1803 has none, and
    Invoke-WebRequest is what used to do this job there);
  * no curl flag newer than what Windows 10 1803's curl 7.55 understands;
  * all three Windows architectures get the right uv build;
  * a uv that predates a flag we would like to pass must not be broken by it.

These are read from the text rather than executed: driving the real script would install
Python on the machine running the tests, and CI has no way to simulate a corporate proxy.
The behavioural verification is a separate scripted rig (see PROGRESS.md).
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "install-python.ps1")
OFFLINE_README = os.path.join(ROOT, "offline", "README.md")


@pytest.fixture(scope="module")
def text():
    return open(SCRIPT, encoding="ascii").read()


@pytest.fixture(scope="module")
def code(text):
    """Just the executable lines -- comments explain intent, they do not implement it, and a
    rule that a comment can satisfy is not a rule. That includes the BODY of the <# ... #>
    block comment at the top of the file: its lines start with '*' or plain words, not '#',
    and leaving them in let a header sentence satisfy assertions meant for code."""
    out = []
    in_block = False
    for line in text.splitlines():
        s = line.strip()
        if in_block:
            if "#>" in s:
                in_block = False
            continue
        if s.startswith("<#"):
            if "#>" not in s[2:]:
                in_block = True
            continue
        if s.startswith("#") or s.startswith(".") or not s:
            continue
        out.append(line)
    return "\n".join(out)


def test_the_script_is_ascii_and_bom_free():
    """PS 5.1 reads a file with no BOM in the machine's ANSI codepage, so a non-ASCII byte can
    decode differently than it did for the author. This file declares itself ASCII-only; that
    is only true while something checks."""
    raw = open(SCRIPT, "rb").read()
    assert not raw.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM"
    bad = [i for i, b in enumerate(raw) if b > 127]
    assert not bad, f"non-ASCII byte at offset {bad[0]}"


def test_curl_is_preferred_but_not_required(code):
    """The regression this test exists for was mine: swapping Invoke-WebRequest out for
    curl.exe fixed a managed machine (curl uses the Windows certificate store, so a
    TLS-intercepting proxy just works) and would have broken every Windows 10 older than
    1803, which ships no curl.exe at all. Prefer the better client; keep the old one as the
    floor. Same shape as the launcher's never-worse rule."""
    assert "curl.exe" in code, "the download no longer prefers curl.exe"
    assert re.search(r"Get-Command\s+curl\.exe", code), (
        "curl.exe is invoked without first checking it exists, so a pre-1803 Windows 10 "
        "loses a download path that used to work")
    assert "Invoke-WebRequest" in code, (
        "the Invoke-WebRequest fallback is gone; on a Windows without curl.exe there is now "
        "no way to download anything")


def test_no_curl_flag_newer_than_windows_10_1803(code):
    """Windows 10 1803 shipped curl 7.55. An unknown option does not degrade -- curl exits
    non-zero, so EVERY download would be reported as refused, on exactly the older machines
    least able to diagnose it. --fail-with-body arrived in 7.76 and was the first casualty."""
    modern = ("--fail-with-body", "--retry-all-errors", "--json", "--parallel")
    for flag in modern:
        assert flag not in code, (
            f"{flag} needs a curl newer than the one Windows 10 1803 ships; read the status "
            f"code instead")


def test_a_download_is_judged_by_status_not_by_the_file_existing(code):
    """Without -f, curl writes the error PAGE to the output file and still exits 0 -- so
    "the file is there" is true for a 403 too. Something has to look at the status."""
    assert re.search(r"http_code", code), "the HTTP status is never captured"
    assert re.search(r"\^2\\d\\d\$|\^2\[0-9\]", code), (
        "nothing checks the status is a 2xx, so an error page counts as a successful download")


def test_all_three_windows_architectures_get_the_right_uv(code):
    """32-bit x86 is rare but real, and $arch is '' there -- so an `arm64 ? aarch64 : x86_64`
    split hands it a 64-bit binary that downloads perfectly and then will not run, which looks
    like a corrupt archive rather than a wrong build."""
    for asset in ("uv-aarch64-pc-windows-msvc.zip",
                  "uv-x86_64-pc-windows-msvc.zip",
                  "uv-i686-pc-windows-msvc.zip"):
        assert asset in code, f"no uv build selected for {asset.split('-')[1]}"


def test_the_offline_archive_is_tried_before_the_network(code):
    """The point of the offline route is that it works on a machine whose download is refused.
    Ordering is what makes that true: checked first, it is also the path that gets exercised
    routinely instead of only on the day it is needed.

    Anchored on the CALL SITE, not the function definition: `function Get-OfflineUvZip {`
    sits near the top of the file and precedes the download whatever order strategy 3 runs
    in, so matching it would make this test unable to fail."""
    offline = code.find("$offlineZip = Get-OfflineUvZip")
    download = code.find("Invoke-Download $uvUrl")
    assert offline > 0, "install-python.ps1 no longer looks for a pre-staged uv archive"
    assert download > 0, "install-python.ps1 no longer downloads uv"
    assert offline < download, (
        "the uv download is attempted before the pre-staged archive, so a machine whose "
        "proxy refuses it never reaches the copy someone put there for it")


def test_the_mirror_env_var_is_honoured_and_never_overwritten(code):
    """UV_PYTHON_INSTALL_MIRROR is the one setting an IT department can push to a whole fleet
    to make CPython come from somewhere reachable. Assigning it here would silently undo that,
    and the failure would look like "uv cannot download" rather than "we clobbered your
    config"."""
    assert "UV_PYTHON_INSTALL_MIRROR" in code, (
        "the internal-mirror escape hatch is not mentioned to the user or honoured")
    assert not re.search(r"\$env:UV_PYTHON_INSTALL_MIRROR\s*=", code), (
        "install-python.ps1 ASSIGNS UV_PYTHON_INSTALL_MIRROR, overwriting the value an "
        "administrator set for this machine")


def test_uv_flags_are_probed_before_use(code):
    """Whoever already has uv on PATH may have any version, and an unrecognised flag is a hard
    error -- it would lose the entire strategy to gain a nicety. So a flag we would merely
    LIKE has to be checked for first."""
    assert "--no-bin" in code, "the uv bin-shim suppression was dropped"
    assert re.search(r"python\s+install\s+--help", code), (
        "--no-bin is passed without probing whether this uv understands it, so an older uv "
        "fails the whole install")


def test_the_install_lands_where_every_script_actually_looks(code, text):
    """The v1.15.3 defect, from the other side: an install that succeeds somewhere nothing
    scans is indistinguishable from no install at all. setup.cmd, the launcher, update.cmd and
    Diagnose.cmd all scan %LOCALAPPDATA%\\Programs\\Python by FILENAME, so uv is pointed there
    rather than installing elsewhere and copying."""
    assert re.search(r"UV_PYTHON_INSTALL_DIR\s*=\s*\$PyRoot", code), (
        "uv no longer installs into the folder the other scripts scan")
    setup = open(os.path.join(ROOT, "setup.cmd"), encoding="ascii").read()
    assert r"%LOCALAPPDATA%\Programs\Python" in setup, (
        "setup.cmd no longer scans that folder -- these two must agree or a successful "
        "install becomes invisible")


def test_externally_managed_is_removed(code):
    """python-build-standalone ships Lib\\EXTERNALLY-MANAGED on Windows (measured -- it is not
    a Linux-only thing), and while it is there pip refuses this interpreter with
    "externally-managed-environment". Without this, setup.cmd installs Python successfully and
    then fails on the very next step."""
    assert "EXTERNALLY-MANAGED" in code, (
        "nothing removes EXTERNALLY-MANAGED, so pip will refuse the interpreter uv just "
        "installed")


def test_the_venv_template_launchers_are_not_treated_as_interpreters(code):
    """Every CPython ships venv TEMPLATE launchers under Lib\\venv\\scripts\\nt. They are not
    usable interpreters, and one was measured taking 17 seconds to answer a --version probe --
    so a recursive filename search must skip them rather than wait on them."""
    assert re.search(r"Lib\\\\+venv", code), (
        "the recursive python.exe search does not exclude Lib\\venv\\scripts\\nt")


def test_the_give_up_screen_leads_with_what_works_offline(text):
    """This screen IS the deliverable for a blocked user, so its content is pinned. It has to
    name a per-route reason (an IT ticket needs "403 on the .zip", not "install failed") and
    both no-download routes -- naming only python.org sends them back at the wall they just
    hit."""
    tail = text[text.find("all three failed"):]
    assert tail, "the combined failure report is gone"
    for needle in ("Pre-stage uv", "Bring your own Python", "UV_PYTHON_INSTALL_MIRROR",
                   "$OfflineDir", "$PyRoot"):
        assert needle in tail, f"the give-up screen no longer mentions {needle}"
    assert "python.org/downloads" not in tail, (
        "the give-up screen sends a blocked user back to python.org, which is the download "
        "their machine just refused")


def test_the_offline_folder_ships_with_instructions_and_ignores_binaries():
    """The folder is only useful if the person who needs it can tell what to put in it, and
    the binaries must not end up in git: they are tens of MB, they go stale, and the right
    build depends on the reader's architecture."""
    assert os.path.exists(OFFLINE_README), (
        "offline/README.md is missing, so the folder is an unexplained empty directory")
    readme = open(OFFLINE_README, encoding="utf-8").read()
    for asset in ("uv-x86_64-pc-windows-msvc.zip", "uv-aarch64-pc-windows-msvc.zip"):
        assert asset in readme, f"README does not tell the reader to fetch {asset}"
    assert "UV_PYTHON_INSTALL_MIRROR" in readme
    ignore = open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read()
    assert "offline/*.zip" in ignore, (
        "a pre-staged uv archive would be committed to the repo")


def test_no_client_or_employer_name_leaks_into_the_public_repo():
    """This repo is public. The device-policy findings came from a corporate machine, and the
    products involved (BeyondTrust/Avecto, Zscaler) are worth naming because other people hit
    the same wall -- but the company does not belong in a public file, and it went in twice
    while this was being written."""
    tracked = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "__pycache__", ".pytest_cache", "docs",
                                    # local-only build/env clutter: reading it wastes I/O
                                    # and third-party text could trip the scan spuriously
                                    ".venv", "venv", ".mypy_cache", ".ruff_cache",
                                    "node_modules")]
        for name in filenames:
            if name.lower().endswith((".ps1", ".cmd", ".py", ".md", ".txt", ".yml", ".json")):
                tracked.append(os.path.join(dirpath, name))
    # These are deliberately local-only notes; .gitignore keeps them out of the repo.
    local_only = {"PROGRESS.md", "LAUNCH_KIT.md", "CODEX_CRASH_AUDIT.md"}
    # Assembled rather than written out, so the needles do not appear literally in this file.
    # Spelling them here would make the scan match its own source -- and the usual escape,
    # skipping this file, would leave the one file containing the rule as the only file the
    # rule cannot see.
    needles = ("B" + "CG", "Boston" + " Consulting")
    pattern = re.compile(r"\b%s\b|%s" % (needles[0], needles[1]))
    offenders = []
    for path in tracked:
        if os.path.basename(path) in local_only:
            continue
        body = open(path, encoding="utf-8", errors="replace").read()
        for lineno, line in enumerate(body.splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{os.path.relpath(path, ROOT)}:{lineno}")
    assert not offenders, "employer name in a public file:\n" + "\n".join(offenders)
