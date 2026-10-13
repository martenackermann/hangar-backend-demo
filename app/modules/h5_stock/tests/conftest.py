"""H5 test fixtures."""

from __future__ import annotations

import pytest

from app.core.actions import actions
from app.core.slices import SliceRegistry


@pytest.fixture(autouse=True)
def h5_asset_actions() -> None:
    """Core ``tests/test_actions.py`` registers and then *unregisters* test handlers for the
    asset actions ``mark_missing``/``correct`` (same registry keys as H5). Re-register the
    slice's handlers when that happened (see the shared-file request in the README)."""
    from app.modules.h5_stock import register_asset_actions

    names = ("mark_missing", "mark_found", "correct")
    if any((d := actions.get("assets", n)) is None or d.owner != "h5" for n in names):
        register_asset_actions(SliceRegistry(owner="h5"))
