"""ADR-0016 S3: the pinned shapes the live readers parse (docs and a scrubbed copy; no live host call)."""
from __future__ import annotations

import json
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"


def test_live_openclaw_fixture_pins_the_agent_model_and_runtime_shape():
    doc = json.loads((FIX / "openclaw_live" / "all_anthropic.json").read_text(encoding="utf-8"))
    entries = doc["agents"]["entries"]
    assert entries["main"]["model"]["primary"].startswith("anthropic/")
    assert doc["agents"]["defaults"]["model"]["primary"].startswith("anthropic/")
    assert doc["agents"]["defaults"]["models"]["anthropic/*"]["agentRuntime"]["id"] == "openclaw"
    assert "operator@" not in json.dumps(doc)


def test_claude_auth_fixtures_load():
    sub = json.loads((FIX / "claude_auth" / "status_subscription.json").read_text(encoding="utf-8"))
    assert sub["authMethod"] == "claude.ai" and sub["apiProvider"] == "firstParty"
    assert json.loads((FIX / "claude_auth" / "status_logged_out.json").read_text(encoding="utf-8")) == {"loggedIn": False}
