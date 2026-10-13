"""TS-BC55-030: every shipped routing profile is in the profile table of docs/multi-model.md, and nothing else is.

`profiles/openclaw-host.json5` is the host configuration, not a routing profile: only `profiles/*.yaml` count.
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "multi-model.md"


def table_rows() -> dict[str, str]:
    """profile name -> its row, from the table under `## Profiles`."""
    text = DOC.read_text(encoding="utf-8")
    section = text.split("\n## Profiles\n", 1)[1].split("\n## ", 1)[0]
    rows = {}
    for line in section.splitlines():
        m = re.match(r"^\| `([a-z0-9-]+)` \|", line)
        if m:
            rows[m.group(1)] = line
    return rows


def test_every_shipped_profile_is_in_the_docs_table():
    shipped = {p.stem for p in (REPO / "profiles").glob("*.yaml")}
    rows = table_rows()
    assert shipped - set(rows) == set(), f"profiles missing from docs/multi-model.md: {sorted(shipped - set(rows))}"


def test_the_docs_table_names_no_profile_that_does_not_exist():
    shipped = {p.stem for p in (REPO / "profiles").glob("*.yaml")}
    assert set(table_rows()) - shipped == set()


def test_the_recommended_profile_is_marked_recommended_and_only_it():
    from ai_resources.setup import wizard
    rows = table_rows()
    marked = [name for name, line in rows.items() if "recommended" in line.lower()
              and "not recommended" not in line.lower()]
    assert marked == [wizard.RECOMMENDED_PROFILE]
