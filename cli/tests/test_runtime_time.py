"""Start-time validation, uptime derivation, and duration formatting."""

from datetime import UTC, datetime

import pytest

from agentworks.errors import StateError
from agentworks.runtime_time import derive_uptime_seconds, format_duration


def test_uptime_requires_running_status_and_known_start_time() -> None:
    now = datetime(2026, 9, 6, 1, 2, 3, tzinfo=UTC)

    assert (
        derive_uptime_seconds(
            "2026-09-06T00:00:00Z",
            running=True,
            entity_kind="session",
            entity_name="work",
            now=now,
        )
        == 3_723
    )
    assert (
        derive_uptime_seconds(
            "2026-09-06T00:00:00Z",
            running=False,
            entity_kind="session",
            entity_name="work",
            now=now,
        )
        is None
    )
    assert (
        derive_uptime_seconds(
            None,
            running=True,
            entity_kind="session",
            entity_name="work",
            now=now,
        )
        is None
    )


def test_uptime_clamps_future_clock_skew() -> None:
    assert (
        derive_uptime_seconds(
            "2026-09-06T01:02:04Z",
            running=True,
            entity_kind="vm",
            entity_name="box",
            now=datetime(2026, 9, 6, 1, 2, 3, tzinfo=UTC),
        )
        == 0
    )


def test_malformed_start_time_is_typed_state_corruption_even_when_stopped() -> None:
    with pytest.raises(StateError) as raised:
        derive_uptime_seconds(
            "not-a-timestamp",
            running=False,
            entity_kind="console",
            entity_name="work",
        )

    assert raised.value.entity_kind == "console"
    assert raised.value.entity_name == "work"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, "unknown"),
        (0, "0s"),
        (59, "59s"),
        (60, "1m"),
        (3_661, "1h 1m 1s"),
        (90_061, "1d 1h 1m 1s"),
    ],
)
def test_format_duration(seconds: int | None, expected: str) -> None:
    assert format_duration(seconds) == expected


def test_format_duration_rejects_negative_values() -> None:
    with pytest.raises(ValueError):
        format_duration(-1)
