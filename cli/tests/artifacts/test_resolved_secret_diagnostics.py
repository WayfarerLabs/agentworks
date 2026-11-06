"""Diagnostic redaction protects resolved secrets without destroying plain paths."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest

from agentworks.artifacts.native.probe import probe_native
from agentworks.errors import ExternalError
from agentworks.harness_setup.runner import SetupRunner
from agentworks.native_files import NativeFiles
from agentworks.ssh import SSHError, SSHLogger, SSHResult
from agentworks.transports import Transport
from tests.test_native_harness_setup import transport as transport


@pytest.mark.parametrize("raised", [False, True])
def test_plain_environment_values_do_not_redact_the_offending_native_path(raised: bool) -> None:
    path = "/home/agt-tester/.grok/agents/operator.md"
    runner = Mock(spec=Transport)
    runner.logger = None
    failure = SSHError(path)
    runner.run.side_effect = [
        SSHResult(0, "\nAGW_PYTHON3=available\n", ""),
        failure if raised else SSHResult(7, "", path),
    ]
    with pytest.raises((SSHError, ExternalError)) as caught:
        probe_native(runner, tool="grok", environment={"USER": "tester", "LETTER": "e", "TYPE": "agent"})
    assert path in str(caught.value)
    if raised:
        assert caught.value is failure


@pytest.mark.parametrize("through_setup_runner", [False, True])
def test_logger_only_secret_is_removed_before_terminal_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, through_setup_runner: bool
) -> None:
    marker = uuid4().hex.upper()
    secret = "x" + marker + "\nsecond-part"
    path = "/home/agt-tester/.grok/agents/operator.md"
    failure = SSHError("\x1b[" + secret + " " + path, hint=secret)
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path / "logs")
    runner = Mock(spec=Transport)
    runner.logger = SSHLogger("resolved-values", "probe", redactions=(secret,))
    runner.default_timeout = 30
    runner.run.side_effect = failure
    with pytest.raises(SSHError) as caught:
        if through_setup_runner:
            SetupRunner(runner, {"USER": "tester", "LETTER": "e"}).run("fixture")
        else:
            probe_native(runner, tool="grok", environment={"USER": "tester", "LETTER": "e"})
    assert caught.value is failure
    assert marker not in str(failure) and marker not in (failure.hint or "")
    assert path in str(failure)


@pytest.mark.skipif(sys.platform != "linux", reason="executes Linux guest native-file helpers")
def test_native_staging_ignores_a_startup_banner_and_cleans_its_directory(transport, monkeypatch):
    original = transport.run

    def with_banner(command, **kwargs):
        result = original(command, **kwargs)
        return SSHResult(result.returncode, "fixture welcome" + result.stdout, result.stderr)

    monkeypatch.setattr(transport, "run", with_banner)
    with NativeFiles(transport) as files:
        destination = str(transport.home / "settings.json")
        files.publish(destination, b"{}", expected=None)
        assert files.read(destination) == b"{}"
    assert not list((transport.root / "tmp").iterdir())
