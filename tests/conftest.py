"""Shared pytest configuration."""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    """Skip integration tests unless VIDEO_STORY_INTEGRATION=1 is set."""
    if os.environ.get("VIDEO_STORY_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(
        reason="set VIDEO_STORY_INTEGRATION=1 to run tests that use real screens/audio/model"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
