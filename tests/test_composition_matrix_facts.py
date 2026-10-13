"""Characterization of the compat matrix facts that scored team composition is built on (ADR-0006).

Under the shipped matrix, Anthropic is the only provider with a verified cell that can serve kit roles through
`by_role`. A multi-provider team is therefore unverified by construction. If a cell is flipped on purpose, this
test must be updated, and the flip is the signal that a mixed team can be verified.

The antigravity engine cell being CONFIGURE does NOT make it a composition target: the usage-policy gate
(`scoring/gate.py`, `direct-only`) excludes it because the Antigravity FAQ forbids third-party tools from
accessing it. The cell is pinned here only so a change to it is noticed.
"""
from ai_resources import model_pins
from ai_resources.setup import compat, providers

SERVES = (compat.CONFIGURE, compat.VIA_GATEWAY)
MULTI = (compat.MULTI_LITELLM, compat.MULTI_OPENROUTER)


def _verified_serving(cockpit, provider):
    return [m for m in compat.MODES
            if compat.cell(cockpit, provider, m).action in SERVES and compat.cell(cockpit, provider, m).verified]


def test_anthropic_is_the_only_provider_with_a_verified_serving_cell_under_the_claude_cockpit():
    with_cells = {p for p in compat.PROVIDER_COLUMNS if _verified_serving("claude", p)}
    assert with_cells == {"anthropic"}
    assert _verified_serving("claude", "anthropic") == list(compat.MODES)


def test_google_openai_deepseek_moonshot_are_unverified_under_the_claude_cockpit():
    for provider in ("google", "openai", "deepseek", "moonshot"):
        for mode in MULTI:
            c = compat.cell("claude", provider, mode)
            assert c.verified is False, (provider, mode)
            assert c.action in (compat.VIA_GATEWAY, compat.SKIP)
        assert compat.cell("claude", provider, compat.SINGLE).action == compat.SKIP


def test_agy_is_report_only():
    assert "agy" in compat.REPORT_ONLY
    for provider in compat.PROVIDER_COLUMNS:
        for mode in compat.MODES:
            c = compat.cell("agy", provider, mode)
            assert c.report_only is True and c.action == compat.SKIP


def test_antigravity_engine_cell_names_agy_ids_that_are_not_api_ids():
    cell = compat.ENGINE_CELLS[("antigravity", "google")]
    assert cell.action == compat.CONFIGURE
    assert "agy" in cell.reason and "never API ids" in cell.reason
    api_ids = {m for ids in providers.KNOWN_MODELS.values() for m in ids}
    assert model_pins.AGY_STATIC, "AGY_STATIC is the agy id list"
    assert not api_ids.intersection(model_pins.AGY_STATIC)


def test_all_21_codex_cockpit_cells_are_instructions_only():
    assert len(compat.PROVIDER_COLUMNS) == 7 and len(compat.MODES) == 3
    cells = [compat.cell("codex", p, m) for p in compat.PROVIDER_COLUMNS for m in compat.MODES]
    assert len(cells) == 21
    assert {c.action for c in cells} == {compat.INSTRUCTIONS_ONLY}


def test_openclaw_codex_engine_is_configure_only_for_openai():
    for provider in compat.PROVIDER_COLUMNS:
        c = compat.ENGINE_CELLS[("codex", provider)]
        if provider == "openai":
            assert c.action == compat.CONFIGURE and c.verified is True
        else:
            assert c.action == compat.SKIP, provider


def test_claude_code_engine_runs_claude_refs_only():
    for provider in compat.PROVIDER_COLUMNS:
        action = compat.ENGINE_CELLS[("claude-code", provider)].action
        assert (action == compat.CONFIGURE) == (provider == "anthropic")
