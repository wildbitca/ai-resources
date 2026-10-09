"""Drift guard: the hand-written Claude ids must follow model_pins.DEFAULTS.

Profiles, litellm, providers and aider keep their own spelling of a model id (the profile file format
does not change). This test fails when one of them names a Claude id that is not a rendering of the
kit default for its class, unless the id is listed in LEGACY with a reason.
"""
from __future__ import annotations

import pathlib
import re

from ai_resources import model_pins as mp

REPO = pathlib.Path(__file__).resolve().parents[1]
SCANNED = sorted(str(p.relative_to(REPO)) for p in (REPO / "profiles").glob("*.yaml")) + [
    "profiles/openclaw-host.json5",
    "scripts/ai_resources/setup/litellm.py",
    "scripts/ai_resources/setup/providers.py",
    "scripts/ai_resources/setup/cockpits/aider.py",
]
_ID = re.compile(r"claude-(?:opus|sonnet|haiku|fable)-[0-9][0-9a-z.@]*(?:-[0-9a-z.@]+)*")

# (file, id) -> reason. Keep each entry short and justified; remove it when the id goes away.
LEGACY = {
    ("scripts/ai_resources/setup/providers.py", "claude-haiku-4-5@20251001"):
        "Vertex AI spells the dated haiku release with @date",
}


def allowed_ids() -> set[str]:
    out: set[str] = set()
    for model_id in mp.DEFAULTS.values():
        out |= {model_id, mp.openclaw_ref(model_id), mp.openrouter_id(model_id)}
    return out


def drift(name: str, text: str, allowed: set[str], legacy=LEGACY) -> list[str]:
    found = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in _ID.finditer(line):
            token = m.group(0).rstrip(".-")
            if token in allowed or "anthropic/" + token in allowed or (name, token) in legacy:
                continue
            found.append(f"{name}:{lineno}: {token}")
    return found


def test_hand_written_ids_follow_the_declaration():
    allowed = allowed_ids()
    problems = []
    for name in SCANNED:
        problems += drift(name, (REPO / name).read_text(encoding="utf-8"), allowed)
    assert not problems, "Claude ids that are not model_pins.DEFAULTS renderings (fix them or add a LEGACY entry):\n" + "\n".join(problems)


def test_drift_is_detected_and_names_the_file():
    text = "classes:\n  opus: anthropic/claude-opus-4-7\n"
    assert drift("profiles/x.yaml", text, allowed_ids()) == ["profiles/x.yaml:2: claude-opus-4-7"]


def test_legacy_entries_still_exist():
    for (name, token), _reason in LEGACY.items():
        assert token in (REPO / name).read_text(encoding="utf-8"), f"stale LEGACY entry {name} {token}"


# --- S13: every vendor, not only Claude ----------------------------------------------------------

from ai_resources import model_providers as mpv  # noqa: E402
from ai_resources.setup import providers as kit_providers  # noqa: E402

_VENDOR_TOKEN = re.compile(r"(?:gemini|gpt|deepseek|kimi)-[A-Za-z0-9][A-Za-z0-9.\-]*")
VENDOR_LEGACY: dict = {}      # (file, id) -> reason; empty today


def vendor_allowed() -> set[str]:
    """Kit defaults and the KNOWN_MODELS entries, in every spelling the adapters accept."""
    out: set[str] = set()
    for adapter in mpv.REGISTRY.values():
        out |= set(adapter.defaults.values())
    for pid, ids in kit_providers.KNOWN_MODELS.items():
        for model_id in ids:
            out.add(model_id)
            out.add(model_id.split("/", 1)[-1])
    return out


def vendor_drift(name: str, text: str, allowed: set[str], legacy=VENDOR_LEGACY) -> list[str]:
    found = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in _VENDOR_TOKEN.finditer(line):
            token = m.group(0).rstrip(".-")
            ref = mpv.identify(token)
            if ref is None or ref.provider == "anthropic":
                continue                 # not a model id of a known family (a prose word, a gemma id) or Claude
            if token in allowed or (name, token) in legacy:
                continue
            found.append(f"{name}:{lineno}: {token}")
    return found


def test_hand_written_vendor_ids_are_kit_defaults_or_known_models():
    allowed = vendor_allowed()
    problems = []
    for name in SCANNED:
        problems += vendor_drift(name, (REPO / name).read_text(encoding="utf-8"), allowed)
    assert not problems, "vendor ids that are neither a kit default nor a KNOWN_MODELS entry:\n" + "\n".join(problems)


def test_a_planted_stale_gemini_id_is_reported_with_its_file():
    text = "by_role:\n  explore:\n    model: google/gemini-2.9-flash\n"
    assert vendor_drift("profiles/x.yaml", text, vendor_allowed()) == ["profiles/x.yaml:3: gemini-2.9-flash"]


def test_a_planted_stale_id_in_a_copied_profile_fails_the_guard(tmp_path):
    src = (REPO / "profiles" / "all-gemini.yaml").read_text(encoding="utf-8")
    planted = src.replace("gemini-3.7-flash", "gemini-2.0-flash", 1)
    assert vendor_drift("profiles/all-gemini.yaml", planted, vendor_allowed()) == [
        f"profiles/all-gemini.yaml:{planted[:planted.index('gemini-2.0-flash')].count(chr(10)) + 1}: gemini-2.0-flash"]


def test_kit_defaults_are_known_models_so_a_default_bump_cannot_orphan_a_profile():
    for adapter in mpv.REGISTRY.values():
        for model_id in adapter.defaults.values():
            if adapter.id in ("anthropic", "vertex"):
                continue
            listed = set(kit_providers.KNOWN_MODELS.get(adapter.id, []))
            assert model_id in listed, f"{adapter.id} default {model_id} is not in KNOWN_MODELS"


# --- AC-S13c: generated Claude files carry no id the matrix does not allow ---------------------------

def _generated_claude_text(tmp_path, monkeypatch, *, mode, backend, route_selection, allow_unverified=False):
    from types import SimpleNamespace
    from ai_resources import selection as sel
    from ai_resources.setup import model_selection as ms, state
    from ai_resources.setup.cockpits import _shared, claude
    root = tmp_path / "claude"
    for attr, value in (("CONFIG_ROOT", root), ("SETTINGS_PATH", root / "settings.json"), ("CLAUDE_MD_PATH", root / "CLAUDE.md"),
                        ("AGENTS_DIR", root / "agents"), ("WORKFLOWS_DIR", root / "workflows")):
        monkeypatch.setattr(claude, attr, value)
    monkeypatch.setattr(claude, "_install_engram_plugin", lambda: False)
    monkeypatch.setattr(_shared, "sync_skill_links", lambda *a, **k: {"removed": [], "skipped": [], "added": []})
    monkeypatch.setattr(claude, "_install_workflow_scripts", lambda *a, **k: ([], []))
    s = state.SetupState()
    s.mode, s.backend = mode, backend or "litellm"
    s.set_selection(route_selection)
    route = next(a for a in ms.plan_table(route_selection, mode, backend, ["claude"], allow_unverified=allow_unverified))
    executors = {"by_role": {"implementer": {"model": "google/gemini-3.8-flash"}, "planner": {"model": "claude-opus-5"}},
                 "classes": {}}
    claude.configure({"state": s, "executors": executors, "master_key": "k", "gateway_url": "http://gw.invalid", "route": route})
    return "\n".join(p.read_text() for p in (root.rglob("*")) if p.is_file())


def _google_only(**kw):
    from ai_resources import selection as sel
    return sel.Selection(providers={"google": sel.ProviderSel()},
                         slots={"google:gemini-flash": sel.SlotSel("google/gemini-3.8-flash")}, **kw)


def test_no_gemini_id_reaches_a_claude_file_when_the_cell_is_not_via_gateway(tmp_path, monkeypatch):
    # multi-model with a skipped route: the announced downgrade to the Claude-native roles
    text = _generated_claude_text(tmp_path, monkeypatch, mode="multi-model", backend="litellm",
                                  route_selection=_google_only())
    assert "gemini" not in text


def test_a_non_claude_id_that_reaches_single_model_is_a_typed_skip_not_a_file(tmp_path, monkeypatch):
    import pytest
    from ai_resources.setup.cockpits import _shared
    with pytest.raises(_shared.SkipCockpit):
        _generated_claude_text(tmp_path, monkeypatch, mode="single-model", backend=None, route_selection=_google_only())


def test_a_gemini_id_is_allowed_in_a_claude_file_only_with_the_opt_in_and_a_gateway(tmp_path, monkeypatch):
    text = _generated_claude_text(tmp_path, monkeypatch, mode="multi-model", backend="litellm",
                                  route_selection=_google_only(allow_unverified=True), allow_unverified=True)
    assert "gemini-3.8-flash" in text


def test_agy_ids_exist_only_in_the_antigravity_engine():
    from ai_resources.setup.cockpits import openclaw
    for engine_id, engine in openclaw.ENGINES.items():
        has_agy = any(m in engine.models for m in mp.AGY_STATIC)
        assert has_agy == (engine_id == "antigravity"), engine_id
