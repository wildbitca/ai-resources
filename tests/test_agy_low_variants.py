"""agy `-low` variants: used only for ids that exist (model_pins' agy slugs); a `-low` id is never invented."""
from __future__ import annotations

from ai_resources import model_pins
from ai_resources.setup.cockpits import openclaw


def _chain(engine: str, primary: str) -> list[str]:
    return openclaw._fallbacks_for(primary, engine)


def test_the_routine_fallback_is_the_confirmed_low_variant():
    assert openclaw.AGY_ROUTINE_FALLBACK == "agy-cli/gemini-3.8-flash-low"
    slug = openclaw.AGY_ROUTINE_FALLBACK.split("/", 1)[1]
    assert slug in model_pins.AGY_STATIC


def test_every_agy_slug_in_a_fallback_chain_is_a_known_slug():
    known = set(model_pins.AGY_STATIC)
    for engine, primary in (("antigravity", "gemini-3.8-flash-low"), ("antigravity", "claude-sonnet-5-5"),
                            ("claude-code", "anthropic/claude-sonnet-5-5")):
        for ref in _chain(engine, primary):
            if ref.startswith("agy-cli/gemini-"):
                assert ref.split("/", 1)[1] in known, ref


def test_no_argon_low_id_is_used_because_openclaw_does_not_list_it():
    # `openclaw models list` (2026.10.5) shows agy-cli-lane/gemini-4-argon-high only; the pins list carries -low,
    # which has not been confirmed on this host, so no chain uses it.
    for engine, primary in (("antigravity", "gemini-3.8-flash-low"), ("antigravity", "claude-sonnet-5-5"),
                            ("claude-code", "x")):
        assert not any("gemini-4-argon-low" in ref for ref in _chain(engine, primary))


def test_the_chains_keep_their_order_and_never_list_the_primary():
    chain = _chain("antigravity", "gemini-3.8-flash-low")
    assert chain[0] == "agy-cli/gemini-4-argon-high" and "agy-cli/gemini-3.8-flash-low" in chain
    assert "agy-cli/gemini-3.8-flash-low" not in _chain("antigravity", "agy-cli/gemini-3.8-flash-low")


def test_the_agy_mcp_hint_is_a_hint_only():
    assert "engram" in openclaw.AGY_MCP_HINT and "never writes" in openclaw.AGY_MCP_HINT
