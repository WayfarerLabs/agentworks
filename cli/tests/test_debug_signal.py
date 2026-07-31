"""The debug signal is mirrored into AGW_DEBUG (issue #309).

``--debug`` and ``AGW_DEBUG=1`` are equivalent inputs to ``debug_enabled``.
For layers below the CLI to read one process-wide signal without importing
``agentworks.cli`` (the azure plugin quiets azure-identity's logging only when
debug is off), ``--debug`` must also set ``AGW_DEBUG``. These tests pin that
mirror on the single ``_set_debug`` chokepoint both entry points route through.
"""

from __future__ import annotations

import os

import pytest

from agentworks.cli._app import _set_debug, debug_enabled


@pytest.fixture(autouse=True)
def _restore_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the module-level debug flag from test order: reset it after
    each case (monkeypatch already restores the AGW_DEBUG env var)."""
    monkeypatch.setattr("agentworks.cli._app._debug", False)


def test_set_debug_true_mirrors_agw_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGW_DEBUG", raising=False)

    _set_debug(True)

    assert debug_enabled() is True
    assert os.environ.get("AGW_DEBUG") == "1"


def test_set_debug_false_leaves_env_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    # When debug is off, AGW_DEBUG was not "1" to begin with, so the helper
    # leaves it untouched rather than clearing an unrelated value.
    monkeypatch.delenv("AGW_DEBUG", raising=False)

    _set_debug(False)

    assert debug_enabled() is False
    assert "AGW_DEBUG" not in os.environ
