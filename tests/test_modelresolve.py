"""Tests for modelresolve — mapping a family alias ("opus") to the concrete latest id.

Everything is monkeypatched so no real `claude` CLI is ever spawned; these assert the
pure decision/parsing/caching logic (the streaming-alias-lag workaround, see the module
docstring). Cross-platform: the module is stdlib-only and the Windows console flag is 0
off-Windows."""
import json

import modelresolve as mr


# ── pass-through: only bare family aliases get resolved; everything else is untouched ──

def test_concrete_id_passes_through_without_touching_cli(monkeypatch):
    # A concrete id must be returned verbatim AND must not trigger any CLI work.
    called = []
    monkeypatch.setattr(mr, "_find_cli", lambda: called.append("find") or "claude")
    monkeypatch.setattr(mr, "_probe_concrete", lambda *a: called.append("probe") or "X")
    assert mr.resolve_model("claude-opus-4-8") == "claude-opus-4-8"
    assert called == []          # neither _find_cli nor _probe_concrete ran


def test_inherit_and_unknown_and_blank_pass_through():
    assert mr.resolve_model("inherit") == "inherit"
    assert mr.resolve_model("some-future-provider/model-x") == "some-future-provider/model-x"
    assert mr.resolve_model("") == ""
    assert mr.resolve_model("   ") == "   "
    assert mr.resolve_model(None) is None
    assert mr.resolve_model(123) == 123


# ── happy path: alias -> concrete latest id ─────────────────────────────────────────

def _patch_ok(monkeypatch, concrete="claude-opus-4-8", sig="claude|100|111"):
    monkeypatch.setattr(mr, "_find_cli", lambda: "claude")
    monkeypatch.setattr(mr, "cli_signature", lambda cli: sig)
    monkeypatch.setattr(mr, "_probe_concrete", lambda cli, base: concrete)


def test_alias_resolves_to_concrete(monkeypatch, tmp_path):
    monkeypatch.setattr(mr, "_CACHE_PATH", tmp_path / "c.json")
    _patch_ok(monkeypatch)
    assert mr.resolve_model("opus") == "claude-opus-4-8"


def test_alias_1m_suffix_reattached(monkeypatch, tmp_path):
    # The "[1m]" is a context-window modifier, not part of the model id — strip it for the
    # probe, then re-attach it so the streaming session still gets the 1M context.
    monkeypatch.setattr(mr, "_CACHE_PATH", tmp_path / "c.json")
    _patch_ok(monkeypatch)
    assert mr.resolve_model("opus[1m]") == "claude-opus-4-8[1m]"


def test_alias_is_case_and_whitespace_tolerant(monkeypatch, tmp_path):
    monkeypatch.setattr(mr, "_CACHE_PATH", tmp_path / "c.json")
    _patch_ok(monkeypatch)
    assert mr.resolve_model("  OPUS  ") == "claude-opus-4-8"


# ── graceful degradation: any failure returns the ORIGINAL spec (never breaks startup) ──

def test_probe_failure_returns_original_alias(monkeypatch, tmp_path):
    monkeypatch.setattr(mr, "_CACHE_PATH", tmp_path / "c.json")
    monkeypatch.setattr(mr, "_find_cli", lambda: "claude")
    monkeypatch.setattr(mr, "cli_signature", lambda cli: "claude|100|111")
    monkeypatch.setattr(mr, "_probe_concrete", lambda cli, base: None)
    assert mr.resolve_model("opus") == "opus"
    assert mr.resolve_model("opus[1m]") == "opus[1m]"


def test_no_cli_returns_original_alias(monkeypatch):
    monkeypatch.setattr(mr, "_find_cli", lambda: None)
    assert mr.resolve_model("opus") == "opus"


# ── cache: keyed by the CLI launcher's file signature; a hit skips the probe (and any
#    subprocess), a changed CLI (new signature) re-probes ──

def test_cache_hit_skips_probe(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"signature": "claude|100|111",
                                 "aliases": {"opus": "claude-opus-4-8"}}), "utf-8")
    monkeypatch.setattr(mr, "_CACHE_PATH", cache)
    monkeypatch.setattr(mr, "_find_cli", lambda: "claude")
    monkeypatch.setattr(mr, "cli_signature", lambda cli: "claude|100|111")
    probed = []
    monkeypatch.setattr(mr, "_probe_concrete", lambda cli, base: probed.append(base) or "SHOULD-NOT-HAPPEN")
    assert mr.resolve_model("opus") == "claude-opus-4-8"
    assert mr.resolve_model("opus[1m]") == "claude-opus-4-8[1m]"   # suffix reattached from cache
    assert probed == []                                            # cache served both, no probe


def test_cache_signature_change_reprobes_and_rewrites(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"signature": "claude|90|100",     # old CLI
                                 "aliases": {"opus": "claude-opus-4-6"}}), "utf-8")
    monkeypatch.setattr(mr, "_CACHE_PATH", cache)
    monkeypatch.setattr(mr, "_find_cli", lambda: "claude")
    monkeypatch.setattr(mr, "cli_signature", lambda cli: "claude|100|222")   # CLI upgraded
    monkeypatch.setattr(mr, "_probe_concrete", lambda cli, base: "claude-opus-4-8")
    assert mr.resolve_model("opus") == "claude-opus-4-8"
    # the stale-signature cache was replaced with the fresh probe under the new signature
    saved = json.loads(cache.read_text("utf-8"))
    assert saved["signature"] == "claude|100|222"
    assert saved["aliases"]["opus"] == "claude-opus-4-8"


def test_cache_miss_writes_result(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    monkeypatch.setattr(mr, "_CACHE_PATH", cache)
    monkeypatch.setattr(mr, "_find_cli", lambda: "claude")
    monkeypatch.setattr(mr, "cli_signature", lambda cli: "claude|100|111")
    monkeypatch.setattr(mr, "_probe_concrete", lambda cli, base: "claude-sonnet-4-6")
    assert mr.resolve_model("sonnet") == "claude-sonnet-4-6"
    saved = json.loads(cache.read_text("utf-8"))
    assert saved == {"signature": "claude|100|111", "aliases": {"sonnet": "claude-sonnet-4-6"}}


def test_use_cache_false_always_probes_and_never_writes(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    monkeypatch.setattr(mr, "_CACHE_PATH", cache)
    monkeypatch.setattr(mr, "_find_cli", lambda: "claude")
    monkeypatch.setattr(mr, "cli_signature", lambda cli: "claude|100|111")
    probed = []
    monkeypatch.setattr(mr, "_probe_concrete", lambda cli, base: probed.append(base) or "claude-opus-4-8")
    assert mr.resolve_model("opus", use_cache=False) == "claude-opus-4-8"
    assert probed == ["opus"]              # probed despite a would-be cache
    assert not cache.exists()              # and wrote nothing


def test_cli_signature_stats_the_file(monkeypatch, tmp_path):
    f = tmp_path / "claude.CMD"
    f.write_text("shim", "utf-8")
    sig = mr.cli_signature(str(f))
    assert sig is not None and str(f) in sig
    assert mr.cli_signature(str(tmp_path / "does-not-exist")) is None


# ── _probe_concrete: parse the CLI's -p JSON, prefer the family we asked for ──

def test_probe_concrete_single_model_key(monkeypatch):
    monkeypatch.setattr(mr, "_run_cli",
                        lambda cli, args, timeout: json.dumps({"modelUsage": {"claude-opus-4-8": {}}}))
    assert mr._probe_concrete("claude", "opus") == "claude-opus-4-8"


def test_probe_concrete_prefers_matching_family_over_helper(monkeypatch):
    # A turn can bill a small helper model alongside the main one; pick the family we asked for.
    monkeypatch.setattr(mr, "_run_cli", lambda cli, args, timeout: json.dumps(
        {"modelUsage": {"claude-haiku-4-5": {}, "claude-opus-4-8": {}}}))
    assert mr._probe_concrete("claude", "opus") == "claude-opus-4-8"


def test_probe_concrete_falls_back_to_top_level_model(monkeypatch):
    monkeypatch.setattr(mr, "_run_cli",
                        lambda cli, args, timeout: json.dumps({"modelUsage": {}, "model": "claude-opus-4-8"}))
    assert mr._probe_concrete("claude", "opus") == "claude-opus-4-8"


def test_probe_concrete_none_on_bad_json_or_no_output(monkeypatch):
    monkeypatch.setattr(mr, "_run_cli", lambda cli, args, timeout: "not json {{{")
    assert mr._probe_concrete("claude", "opus") is None
    monkeypatch.setattr(mr, "_run_cli", lambda cli, args, timeout: None)
    assert mr._probe_concrete("claude", "opus") is None


# ── cli_version parsing ─────────────────────────────────────────────────────────────

def test_cli_version_parses_semver(monkeypatch):
    monkeypatch.setattr(mr, "_find_cli", lambda: "claude")
    monkeypatch.setattr(mr, "_run_cli", lambda cli, args, timeout: "2.1.156 (Claude Code)\n")
    assert mr.cli_version() == "2.1.156"


def test_cli_version_none_when_unavailable(monkeypatch):
    monkeypatch.setattr(mr, "_find_cli", lambda: "claude")
    monkeypatch.setattr(mr, "_run_cli", lambda cli, args, timeout: None)
    assert mr.cli_version() is None
