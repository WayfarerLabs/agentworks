"""The operation log record: a request's shape and outcome, never its secrets."""

from __future__ import annotations

import pytest

from agentworks.execution.oplog import format_record
from agentworks.execution.requests import Command, Script, Shell
from agentworks.execution.results import Completed, NotDelivered, NotDeliveredReason, Result, Unknown, UnknownReason

ENV_VALUE = "env-value-7f3a"
SOURCE = "echo script-source-9c1d"


def test_a_script_record_names_the_environment_but_holds_no_value_and_no_source() -> None:
    request = Script(SOURCE, Shell.BASH, env={"API_TOKEN": ENV_VALUE, "MODE": "x"}, deadline=30)

    record = format_record(request, Result(Completed(0)), label="Checking")

    assert ENV_VALUE not in record
    assert "script-source" not in record
    assert "API_TOKEN" in record
    assert "MODE" in record


def test_a_command_record_shows_its_argv() -> None:
    record = format_record(Command(("systemctl", "is-active", "ssh")), Result(Completed(3)), label="Checking")

    assert "systemctl is-active ssh" in record


@pytest.mark.parametrize(
    "completion",
    [
        Completed(255),
        NotDelivered(NotDeliveredReason.REFUSED, "cwd"),
        NotDelivered(NotDeliveredReason.PYTHON_MISSING),
        Unknown(UnknownReason.DEADLINE),
    ],
)
def test_every_completion_and_both_streams_and_the_diagnostics_reach_the_record(completion: object) -> None:
    result = Result(completion, b"out-bytes\n", b"err-bytes\xff", "ssh: diagnostics line")  # type: ignore[arg-type]

    record = format_record(Command(("true",)), result, label="Step")

    assert "out-bytes" in record
    assert "err-bytes" in record
    assert "ssh: diagnostics line" in record
