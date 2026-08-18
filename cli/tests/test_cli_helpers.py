"""Tests for the CLI-layer helpers in agentworks.cli._helpers."""

from __future__ import annotations

import sys

import pytest

from agentworks import output
from agentworks.cli._helpers import ordinary_interaction_policy, parse_csv_filter
from agentworks.secrets.policy import InteractionPolicy


def test_parse_csv_filter_none_passes_through() -> None:
    assert parse_csv_filter(None) is None


def test_parse_csv_filter_single_value_stays_a_string() -> None:
    # Single value stays a bare string so single-value callers read naturally.
    assert parse_csv_filter("ws1") == "ws1"


def test_parse_csv_filter_strips_surrounding_whitespace_on_single() -> None:
    assert parse_csv_filter("  ws1  ") == "ws1"


def test_parse_csv_filter_multiple_values_returns_list() -> None:
    assert parse_csv_filter("ws1,ws2,ws3") == ["ws1", "ws2", "ws3"]


def test_parse_csv_filter_strips_whitespace_around_each_value() -> None:
    assert parse_csv_filter("ws1, ws2 , ws3") == ["ws1", "ws2", "ws3"]


def test_parse_csv_filter_drops_empty_segments() -> None:
    # Trailing commas, doubled commas, leading commas all collapse cleanly.
    assert parse_csv_filter("ws1,,ws2,") == ["ws1", "ws2"]
    assert parse_csv_filter(",ws1,ws2") == ["ws1", "ws2"]


def test_parse_csv_filter_all_empty_returns_none() -> None:
    # The flag was present but contained only whitespace / separators -- treat
    # as no filter rather than a "match nothing" filter.
    assert parse_csv_filter("") is None
    assert parse_csv_filter(",") is None
    assert parse_csv_filter("  ,  ,  ") is None


def test_parse_csv_filter_single_value_after_dedup_stays_a_string() -> None:
    # Trailing comma with one real value still collapses to the bare-string form.
    assert parse_csv_filter("ws1,") == "ws1"


def test_parse_csv_filter_preserves_duplicate_values() -> None:
    # Duplicates are NOT collapsed; the helper preserves the operator's input
    # verbatim. SQL IN with duplicate parameters is a harmless no-op, and
    # collapsing here would hide a typo from a user trying to spot one.
    assert parse_csv_filter("ws1,ws1") == ["ws1", "ws1"]
    assert parse_csv_filter("ws1,ws2,ws1") == ["ws1", "ws2", "ws1"]


# -- ordinary_interaction_policy: consent is the flag alone ------------------
#
# The field bug this fix closes: a configured, verified 1Password source was
# unusable from a non-TTY agent shell because consent derivation read TTY-ness
# (docs/sdd/2026-08-04-next-steps/message-2026-08-18-agentic-onboarding-run.md).
# These pin the replacement invariant directly, at the one function that
# derives it, rather than only through a command built on top of it.


def test_ordinary_interaction_policy_allows_by_default_without_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output.set_non_interactive(False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(
        output,
        "is_interactive",
        lambda: pytest.fail("consent must not consult TTY-ness"),
    )
    assert ordinary_interaction_policy() is InteractionPolicy.ALLOW


def test_ordinary_interaction_policy_allows_by_default_with_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    output.set_non_interactive(False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert ordinary_interaction_policy() is InteractionPolicy.ALLOW


def test_ordinary_interaction_policy_refuses_under_non_interactive_regardless_of_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output.set_non_interactive(True)
    try:
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        assert ordinary_interaction_policy() is InteractionPolicy.REFUSE
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        assert ordinary_interaction_policy() is InteractionPolicy.REFUSE
    finally:
        output.set_non_interactive(False)
