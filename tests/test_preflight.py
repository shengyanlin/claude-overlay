"""Tests for the environment preflight, and the tripwire that keeps the startup guard
from quietly coming undone.

Two things are being protected here:

  1. preflight names the actual cause. A check that reports "environment fine" for a
     machine that can't launch is worse than none, so the negative cases (missing app
     files, an SDK too old for the code) are tested as hard as the happy path.

  2. The import guard in claude_overlay.py keeps covering every risky import. That is a
     structural property, not a value — so it's asserted against the AST, which means it
     keeps holding for imports nobody has written yet. Pinning the current import list
     instead would just be a copy of today's code that has to be edited whenever it's
     wrong, which is the same as not checking.
"""
import ast
import os
import sys

import pytest

import preflight

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Modules that are safe to import before the crash reporter is installed: they ship with
# Python, so they cannot be the thing a broken install is missing.
_STDLIB_OK = {
    "asyncio", "base64", "ctypes", "ctypes.wintypes", "hashlib", "json", "math", "os",
    "re", "statistics", "sys", "threading", "time", "queue", "pathlib", "traceback",
    "tempfile",
}


def _module_level_imports(tree):
    """(node, inside_try) for every import statement at module level."""
    out = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            out.append((node, False))
        elif isinstance(node, ast.Try):
            for sub in node.body:
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    out.append((sub, True))
    return out


def _names(node):
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    return [node.module or ""]


# ── the structural tripwire ─────────────────────────────────────────────────
def test_every_risky_import_stays_inside_the_startup_guard():
    """A new unguarded `import something` at the top of claude_overlay.py would restore
    the original bug exactly: under pythonw it dies with no window and no message. This
    test fails the moment one is added."""
    src = open(os.path.join(ROOT, "claude_overlay.py"), encoding="utf-8").read()
    tree = ast.parse(src)

    unguarded = []
    for node, in_try in _module_level_imports(tree):
        if in_try:
            continue
        for name in _names(node):
            root = name.split(".")[0]
            if name in _STDLIB_OK or root in _STDLIB_OK or root == "crashreport":
                continue
            unguarded.append(f"line {node.lineno}: {name}")

    assert not unguarded, (
        "these imports run before/outside the crash guard, so a failure in them is "
        "invisible under pythonw:\n  " + "\n  ".join(unguarded))


def test_crashreport_is_imported_before_anything_that_can_fail():
    """Order matters: the reporter has to be installed before the imports it protects."""
    src = open(os.path.join(ROOT, "claude_overlay.py"), encoding="utf-8").read()
    tree = ast.parse(src)

    crash_line = next(n.lineno for n, _ in _module_level_imports(tree)
                      if "crashreport" in _names(n))
    # crashreport's own import is in a try too (see the fallback test below), so it is
    # excluded here - what's being asserted is that it comes first, not that it's bare.
    guarded = [n.lineno for n, in_try in _module_level_imports(tree)
               if in_try and "crashreport" not in _names(n)]
    assert guarded, "the risky imports should be inside a try block"
    assert crash_line < min(guarded)


def test_crashreport_import_has_its_own_fallback():
    """crashreport.py is itself one of the files a partial copy leaves behind, so its
    import must not be the one unprotected line in the file."""
    src = open(os.path.join(ROOT, "claude_overlay.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Try):
            if any("crashreport" in _names(s) for s in node.body
                   if isinstance(s, (ast.Import, ast.ImportFrom))):
                assert node.handlers, "needs an except: branch"
                return
    pytest.fail("`import crashreport` is not wrapped in a try/except")


# ── required symbols are derived, not copied ────────────────────────────────
def test_required_symbols_match_the_unguarded_sdk_import_in_worker():
    """The list is read out of worker.py so it cannot drift. Verify it reflects the real
    import - and, importantly, that it EXCLUDES the try/except-guarded ones, which are
    designed to be absent on an older SDK and must not be reported as breakage."""
    syms = preflight.required_sdk_symbols()
    assert "StreamEvent" in syms          # the one that dates the SDK floor (0.1.49)
    assert "ClaudeSDKClient" in syms
    # These are imported defensively in worker.py; an SDK without them still works.
    for optional in ("PermissionResultDeny", "ClaudeSDKError", "CLIConnectionError",
                     "CLIJSONDecodeError", "ProcessError"):
        assert optional not in syms, f"{optional} is a guarded import, not a requirement"


def test_required_symbols_are_read_from_worker_not_hardcoded(tmp_path, monkeypatch):
    """Point repo_dir at a fake worker.py and the requirement list must follow it."""
    (tmp_path / "worker.py").write_text(
        "from claude_agent_sdk import AlphaThing, BetaThing\n"
        "try:\n    from claude_agent_sdk import GuardedThing\nexcept Exception:\n    pass\n",
        encoding="utf-8")
    monkeypatch.setattr(preflight, "repo_dir", lambda: str(tmp_path))

    syms = preflight.required_sdk_symbols()
    assert set(syms) == {"AlphaThing", "BetaThing"}


def test_required_symbols_fall_back_when_worker_is_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "repo_dir", lambda: str(tmp_path))   # no worker.py
    assert preflight.required_sdk_symbols() == preflight._FALLBACK_SDK_SYMBOLS


# ── the checks themselves ───────────────────────────────────────────────────
def test_healthy_install_reports_no_blocking_problems():
    fails = [p for p in preflight.check() if p.level == preflight.FAIL]
    assert not fails, "\n".join(p.text() for p in fails)


def test_missing_app_files_are_named(tmp_path, monkeypatch):
    """The 'I only replaced claude_overlay.py' update. Every missing module gets named,
    because "something is missing" is not a fix and "config.py, worker.py" is."""
    (tmp_path / "claude_overlay.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(preflight, "repo_dir", lambda: str(tmp_path))

    problem = next(p for p in preflight.check() if p.key == "files")
    assert problem.level == preflight.FAIL
    for expected in ("config.py", "worker.py", "authstate.py"):
        assert expected in problem.what
    assert "whole folder" in problem.fix


def test_sdk_missing_a_required_symbol_is_reported_as_too_old(monkeypatch):
    """Simulates the pre-0.1.49 SDK: importable, but without StreamEvent."""
    monkeypatch.setattr(preflight, "required_sdk_symbols",
                        lambda: ("ClaudeSDKClient", "NoSuchSymbolInAnySDK"))

    problem = next(p for p in preflight.check() if p.key == "sdk-old")
    assert problem.level == preflight.FAIL
    assert "NoSuchSymbolInAnySDK" in problem.what
    assert "ClaudeSDKClient" not in problem.what      # only the ABSENT ones are listed
    assert "-m pip install" in problem.fix


def test_two_pythons_are_called_out(monkeypatch, tmp_path):
    """The packages ARE installed, pip list proves it, and the app still won't start -
    because they went into a Python the launcher doesn't use."""
    other = tmp_path / "otherpython" / "python.exe"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"")
    monkeypatch.setattr(preflight, "launcher_python", lambda: str(other))

    problem = next(p for p in preflight.check() if p.key == "two-pythons")
    assert problem.level == preflight.WARN      # never blocking: the report may legitimately
    assert str(other) in problem.what           # be run by a different interpreter
    assert sys.executable in problem.what
    assert str(other) in problem.fix            # the fix must target the LAUNCHER's python


def test_no_two_pythons_warning_when_they_match(monkeypatch):
    monkeypatch.setattr(preflight, "launcher_python", lambda: sys.executable)
    assert not [p for p in preflight.check() if p.key == "two-pythons"]


def test_no_two_pythons_warning_when_pythonw_is_absent(monkeypatch):
    monkeypatch.setattr(preflight, "launcher_python", lambda: None)
    assert not [p for p in preflight.check() if p.key == "two-pythons"]


def test_fix_commands_name_this_interpreter():
    """"But I already installed it" is nearly always "into the other Python". A bare
    `pip install` in the instructions can land in the wrong one; this cannot."""
    assert sys.executable in preflight.pip_command()


def test_fix_commands_swap_pythonw_for_its_console_twin(tmp_path):
    """The startup dialog is produced UNDER pythonw, so sys.executable there IS pythonw —
    and a `pythonw -m pip ...` fix opens no window and prints nothing, which to the person
    pasting it reads as "the fix is broken too" (reported from the field, screenshot and
    all). The advice must name the python.exe beside it: same install, same
    site-packages, visible output."""
    pyw = tmp_path / "pythonw.exe"
    twin = tmp_path / "python.exe"
    pyw.write_bytes(b"")
    twin.write_bytes(b"")
    cmd = preflight.pip_command(str(pyw))
    assert str(twin) in cmd
    assert "pythonw.exe" not in cmd


def test_fix_commands_keep_pythonw_when_it_is_all_there_is(tmp_path):
    """A SILENT install into the right Python still beats a visible one into the wrong
    Python, so a pythonw with no python.exe sibling stays the target."""
    pyw = tmp_path / "pythonw.exe"
    pyw.write_bytes(b"")
    assert str(pyw) in preflight.pip_command(str(pyw))


def test_launcher_python_scans_where_setup_installs(monkeypatch, tmp_path):
    """PATH is not the whole world: on the machines setup.cmd provisions, the interpreter
    lives ONLY under %LOCALAPPDATA%\\Programs\\Python, and the launcher finds it by
    scanning. A launcher_python that stopped at PATH kept the two-pythons warning silent
    on exactly those machines — the one mismatch it exists to catch."""
    import shutil
    tree = tmp_path / "Programs" / "Python" / "cpython-3.12-windows-x86_64-none"
    tree.mkdir(parents=True)
    (tree / "pythonw.exe").write_bytes(b"")
    (tree / "python.exe").write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda name: None)

    got = preflight.launcher_python()
    assert got is not None
    assert os.path.normcase(got) == os.path.normcase(str(tree / "python.exe"))


def test_no_two_pythons_warning_when_the_report_runs_under_pythonw(monkeypatch, tmp_path):
    """Diagnose.cmd runs this report under the launcher's own pythonw. pythonw.exe and
    the python.exe beside it are two file names for ONE environment; calling that "two
    Pythons" on every healthy machine would teach people to ignore the warning."""
    pyw = tmp_path / "pythonw.exe"
    twin = tmp_path / "python.exe"
    pyw.write_bytes(b"")
    twin.write_bytes(b"")
    monkeypatch.setattr(preflight, "launcher_python", lambda: str(twin))
    monkeypatch.setattr(sys, "executable", str(pyw))
    assert not [p for p in preflight.check() if p.key == "two-pythons"]


def test_summary_reports_only_blocking_problems():
    probs = [preflight.Problem(preflight.WARN, "w", "just a warning"),
             preflight.Problem(preflight.FAIL, "f", "the blocker", "do this")]
    text = preflight.summary(probs)
    assert "the blocker" in text and "do this" in text
    assert "just a warning" not in text


def test_summary_says_so_when_nothing_is_wrong():
    assert "No blocking" in preflight.summary([])


def test_verdict_follows_the_report(monkeypatch):
    """A green exit code over a red report is worse than no check at all."""
    monkeypatch.setattr(preflight, "check",
                        lambda: [preflight.Problem(preflight.FAIL, "x", "broken")])
    text, ok = preflight.run(deep=False)
    assert "broken" in text and ok is False

    monkeypatch.setattr(preflight, "check", lambda: [])
    text, ok = preflight.run(deep=False)
    assert ok is True


def test_deep_run_actually_loads_the_app():
    """The end-to-end check: a real subprocess importing the app the way the launcher
    will. This is the only check that can catch a break nobody thought to look for."""
    ok, out = preflight._import_smoke()
    assert ok, out
    assert "import OK" in out


def test_a_broken_app_fails_the_deep_run(monkeypatch):
    monkeypatch.setattr(preflight, "_import_smoke",
                        lambda: (False, "Traceback ... ImportError: boom"))
    text, ok = preflight.run(deep=True)
    assert ok is False
    assert "cannot load" in text


# ── the model-menu line: name the offer AND where it came from ──────────────────────

def _entitle(tmp_path, monkeypatch, *families):
    import json
    import modelresolve as mr
    mr._ENT_MEMO["key"] = mr._ENT_MEMO["families"] = None
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps({"modelAccessCache": [
        {"apiName": f"claude-{f}-9", "entitled": True} for f in families]}), "utf-8")
    monkeypatch.setattr(mr, "_CLAUDE_JSON", p)
    return p


def test_model_menu_line_names_the_offer_and_the_source(tmp_path, monkeypatch):
    # "Fable is missing from my menu" has three different causes; the report has to
    # distinguish them, so it prints what is offered, what is entitled, and from where.
    p = _entitle(tmp_path, monkeypatch, "opus", "sonnet", "haiku")
    line = preflight.model_menu_line()
    assert "Fable" not in line.split("(")[0]
    assert "entitled families: haiku, opus, sonnet" in line
    assert str(p) in line


def test_model_menu_line_says_when_entitlement_is_unknown(tmp_path, monkeypatch):
    import modelresolve as mr
    mr._ENT_MEMO["key"] = mr._ENT_MEMO["families"] = None
    monkeypatch.setattr(mr, "_CLAUDE_JSON", tmp_path / "absent.json")
    line = preflight.model_menu_line()
    assert "entitlement unknown" in line and "showing all" in line
    assert "Fable" in line          # unknown means show everything, not hide everything


def test_model_menu_line_says_when_the_filter_is_off(tmp_path, monkeypatch):
    import config
    _entitle(tmp_path, monkeypatch, "opus")
    monkeypatch.setattr(config, "MODEL_MENU_FILTER", False)
    line = preflight.model_menu_line()
    assert "filter OFF" in line and "Fable" in line


def test_model_menu_line_never_raises(monkeypatch):
    import modelresolve as mr
    def boom(*a, **k):
        raise RuntimeError("nope")
    monkeypatch.setattr(mr, "entitled_families", boom)
    assert "could not be determined" in preflight.model_menu_line()
