"""The public prompt and output names of setup.ui keep their signatures (new names only may be added)."""
from __future__ import annotations

import inspect

import pytest

from ai_resources.setup import ui

_SIGNATURES = {
    "select": "(message: 'str', choices: 'list[Any]', default: 'Any | None' = None, instruction: 'str' = '', "
              "recommended: 'Any | None' = None) -> 'Any'",
    "checkbox": "(message: 'str', choices: 'list[Any]', default: 'list[Any] | None' = None, "
                "instruction: 'str' = '') -> 'list[Any]'",
    "text": "(message: 'str', default: 'str' = '', validate: 'Callable[[str], bool | str] | None' = None) -> 'str'",
    "password": "(message: 'str', validate: 'Callable[[str], bool | str] | None' = None) -> 'str'",
    "confirm": "(message: 'str', default: 'bool' = True, recommended: 'bool | None' = None) -> 'bool'",
    "section": "(num: 'int', total: 'int', title: 'str') -> 'None'",
    "info": "(msg: 'str') -> 'None'",
    "ok": "(msg: 'str') -> 'None'",
    "warn": "(msg: 'str') -> 'None'",
    "error": "(msg: 'str') -> 'None'",
    "detail": "(msg: 'str') -> 'None'",
    "banner": "(title: 'str', subtitle: 'str' = '', version: 'str' = '') -> 'None'",
    "spinner": "(label: 'str')",
}


@pytest.mark.parametrize("name", sorted(_SIGNATURES))
def test_wrapper_signature_pinned(name):
    assert str(inspect.signature(getattr(ui, name))) == _SIGNATURES[name]
