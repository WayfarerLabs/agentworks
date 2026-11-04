"""Normalization cannot reconstruct a credential after setup redaction."""

from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from agentworks.capabilities.harness_integration import harness_integration_for
from agentworks.errors import ExternalError
from agentworks.harness_setup.dispatch import run_setup
from agentworks.harness_setup.outcomes import SetupFailure, describe_setup_failures
from agentworks.harness_setup.state import read_native_setup
from agentworks.ssh import SSHLogger
from tests.capabilities.test_native_setup_dispatch import setup_case as setup_case


def _secret(label: str, representation: str) -> tuple[str, str, str]:
    value = label + "-" + uuid4().hex
    if representation == "whitespace":
        value += "\nline two\twords"
        rendered = value.replace("\n", " \r\n\t").replace("\twords", "  \nwords")
    else:
        if representation == "long":
            value += "x" * 3072 + uuid4().hex
        middle = len(value) // 2
        sequence = "\x1b]0;fixture-title\x07" if representation == "osc" else "\x1b[31m"
        rendered = value[:middle] + sequence + value[middle:]
    return value, rendered, " ".join(value.split())


@pytest.mark.parametrize("representation", ["ansi", "osc", "whitespace", "long"])
@pytest.mark.parametrize("with_logger", [False, True])
@pytest.mark.parametrize("report_incomplete", [False, True])
def test_dispatch_redacts_canonicalized_invocation_and_logger_patterns_before_bounding(
    setup_case, monkeypatch, tmp_path, representation, with_logger, report_incomplete
):
    db, inputs, invocation, events, _ = setup_case
    token, rendered, canonical = _secret("invocation", representation)
    log_token, log_rendered, log_canonical = _secret("logger", representation)
    logger = None
    if with_logger:
        monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path / "logs")
        logger = SSHLogger("private-diagnostics", "setup", redactions=(log_token,))
    invocation.runner.logger = logger
    invocation = replace(invocation, secrets={"credential": token})
    # The logger-only value precedes a long invocation value crossing the storage
    # limit. Redacting a truncated or pre-normalized copy cannot protect both.
    payload = "failure " + (log_rendered + " " if with_logger else "")
    if representation == "long":
        payload += "f" * (2048 - 64)
    payload += rendered
    original = harness_integration_for("first").user_init

    def initialize(self, call):
        result = original(self, call)
        if self.name == "first":
            if report_incomplete:
                call.report_incomplete(payload)
            raise ExternalError(payload, hint=payload)
        return result

    monkeypatch.setattr(harness_integration_for("first"), "user_init", initialize)
    failures: list[SetupFailure] = []
    state = run_setup(db, Mock(), inputs, invocation, failures=failures, operation="agent-reinit")
    assert events == [("first", False), ("second", False)]
    assert not state.records[0].complete and state.records[1].complete
    (failure,) = failures
    assert failure.hint is not None
    scalar_values = [failure.reason, failure.hint, *state.records[0].incomplete_reasons]
    assert all(len(value.encode()) <= 2048 for value in scalar_values)
    persisted = read_native_setup(db, "agent", "agent")
    assert persisted == state
    surfaces = [persisted.model_dump_json(), describe_setup_failures(failures)]
    if logger is not None:
        surfaces += [logger.path.read_text(), str(logger.warnings)]
    patterns = [canonical, canonical[:16]]
    if with_logger:
        patterns += [log_canonical, log_canonical[:16]]
    assert all(pattern not in surface for surface in surfaces for pattern in patterns)
    assert all("\x1b" not in surface for surface in surfaces)
