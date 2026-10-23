"""Doctor: vendored skill pins against upstream (ADR-0015 S9). Warn-only, skipped offline, never a network call in tests."""
from __future__ import annotations

import json

import pytest

from ai_resources import vendor_pin

SHA_A, SHA_B = "a" * 40, "b" * 40


def manifest(tmp_path, sha=SHA_A):
    p = tmp_path / "resources.json"
    p.write_text(json.dumps({"version": 1, "skills": {"sources": [
        {"id": "pack", "type": "git", "url": "https://example.test/p.git", "ref": "main", "sha": sha},
        {"id": "local", "type": "path", "url": "x", "sha": "z"}]}}))
    return p


def runner(sha, rc=0, seen=None):
    def run(argv, timeout=10):
        if seen is not None:
            seen.append(list(argv))
        return rc, f"{sha}\trefs/heads/main\n" if sha else ""
    return run


def test_the_conftest_keeps_the_suite_offline(monkeypatch):
    import os
    assert os.environ.get("AI_RESOURCES_OFFLINE") == "1"


def test_a_current_pin_is_current(tmp_path):
    seen = []
    [st] = vendor_pin.check(manifest(tmp_path), runner(SHA_A, seen=seen), environ={})
    assert (st.id, st.status, st.upstream) == ("pack", "current", SHA_A)
    assert seen == [["ls-remote", "https://example.test/p.git", "main"]]


def test_a_pin_behind_upstream_is_reported(tmp_path):
    [st] = vendor_pin.check(manifest(tmp_path), runner(SHA_B), environ={})
    assert st.status == "behind" and SHA_B[:12] in st.detail


@pytest.mark.parametrize("run", [runner("", rc=128), runner("garbage"), runner(SHA_A, rc=1)])
def test_an_unreachable_upstream_is_skipped_not_an_error(tmp_path, run):
    [st] = vendor_pin.check(manifest(tmp_path), run, environ={})
    assert st.status == "skipped"


def test_offline_makes_no_call_at_all(tmp_path):
    seen = []
    [st] = vendor_pin.check(manifest(tmp_path), runner(SHA_A, seen=seen), environ={"AI_RESOURCES_OFFLINE": "1"})
    assert st.status == "skipped" and seen == []


def test_only_git_packs_with_a_sha_are_checked_and_a_bad_manifest_is_empty(tmp_path):
    assert [p["id"] for p in vendor_pin.sources(manifest(tmp_path))] == ["pack"]
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    assert vendor_pin.sources(bad) == [] and vendor_pin.sources(tmp_path / "none.json") == []


def test_the_shipped_manifest_pins_a_full_sha():
    for pack in vendor_pin.sources():
        assert len(pack["sha"]) == 40


def test_the_doctor_check_warns_when_behind_and_never_counts_as_an_issue(monkeypatch, tmp_path):
    from ai_resources import doctor
    from ai_resources.setup import ui
    msgs: list[str] = []
    monkeypatch.setattr(vendor_pin, "check", lambda *a, **k: [
        vendor_pin.PinStatus("pack", SHA_A, SHA_B, "behind", "upstream main is at bbbbbbbbbbbb"),
        vendor_pin.PinStatus("other", SHA_A, "", "skipped", "upstream not reachable")])
    monkeypatch.setattr(ui, "warn", msgs.append)
    monkeypatch.setattr(ui, "detail", msgs.append)
    assert doctor._check_vendor_pins() == 0
    assert any("pinned aaaaaaaaaaaa" in m and "generate --force" in m for m in msgs)
    assert any("not compared" in m for m in msgs)


def test_the_doctor_check_survives_a_crashing_reader(monkeypatch):
    from ai_resources import doctor
    from ai_resources.setup import ui
    monkeypatch.setattr(vendor_pin, "check", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(ui, "detail", lambda m: None)
    assert doctor._check_vendor_pins() == 0
