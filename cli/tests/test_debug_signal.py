"""Debug state and its AGW_DEBUG mirror (issue #309).

``--debug`` and ``AGW_DEBUG=1`` are equivalent inputs to ``debug_enabled``. For
layers below the CLI to read one process-wide signal without importing
``agentworks.cli`` (the azure plugin quiets azure-identity's logging only when
debug is off), the debug state is mirrored into ``AGW_DEBUG``, but ONLY by the
real Typer callback, after Click's authoritative parse. The pre-callback sets
``_debug`` from an argv heuristic and must NOT mirror, or a false-positive
``--debug`` token (e.g. a positional after ``--``) could not self-correct
against Click's parse. These tests pin that split.
"""

from __future__ import annotations

import os

import pytest

from agentworks.cli._app import (
    _global_options,
    _seed_debug_from_pre_callback,
    _set_debug,
    debug_enabled,
)


@pytest.fixture(autouse=True)
def _reset_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate the module-level ``_debug`` flag from test order (the conftest
    autouse fixture already snapshots and restores ``AGW_DEBUG``)."""
    monkeypatch.setattr("agentworks.cli._app._debug", False)


def test_set_debug_records_state_without_mirroring(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_set_debug`` sets ``_debug`` only: mirroring AGW_DEBUG is the real
    callback's job, so the heuristic pre-callback cannot poison the env the
    real callback reads back."""
    monkeypatch.delenv("AGW_DEBUG", raising=False)

    _set_debug(True)

    assert debug_enabled() is True
    assert os.environ.get("AGW_DEBUG") != "1"


def test_real_callback_mirrors_genuine_debug_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """``agw --debug cmd``: Click binds the flag, so the real callback turns
    debug on AND mirrors it to AGW_DEBUG for the layers below the CLI."""
    monkeypatch.delenv("AGW_DEBUG", raising=False)

    _global_options(debug=True)

    assert debug_enabled() is True
    assert os.environ.get("AGW_DEBUG") == "1"


def test_real_callback_honors_ambient_agw_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AGW_DEBUG=1 agw cmd``: the flag is unset but the ambient env turns
    debug on, and the mirror leaves it set."""
    monkeypatch.setenv("AGW_DEBUG", "1")

    _global_options(debug=False)

    assert debug_enabled() is True
    assert os.environ.get("AGW_DEBUG") == "1"


def test_real_callback_leaves_env_untouched_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Debug off: AGW_DEBUG was not "1" to begin with, so the mirror leaves it
    untouched rather than clearing an unrelated value."""
    monkeypatch.delenv("AGW_DEBUG", raising=False)

    _global_options(debug=False)

    assert debug_enabled() is False
    assert "AGW_DEBUG" not in os.environ


def test_false_positive_debug_token_does_not_stick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (the AGW_DEBUG feedback loop): a literal ``--debug`` token
    Click does NOT bind (e.g. a positional after ``--``) trips the
    pre-callback's argv heuristic, but must not leave debug on.

    The pre-callback sets ``_debug`` transiently WITHOUT mirroring; the real
    callback then sees ``debug=False`` and no ambient AGW_DEBUG, recomputes
    debug off, and does not mirror. Before the split (when the pre-callback
    mirrored) the heuristic write became the env the real callback read back,
    so the false positive stuck: this test fails against that code.
    """
    monkeypatch.delenv("AGW_DEBUG", raising=False)
    monkeypatch.setattr("sys.argv", ["agw", "some-cmd", "--", "--debug"])

    _seed_debug_from_pre_callback()
    # The heuristic pre-pass flips _debug on transiently (argv contains the
    # token), for the parse-error traceback path.
    assert debug_enabled() is True

    # Click did not bind --debug (positional after `--`), so the real callback
    # runs with debug=False and no ambient AGW_DEBUG.
    _global_options(debug=False)

    assert debug_enabled() is False
    assert os.environ.get("AGW_DEBUG") != "1"
