"""Guest failures remain bounded and redacted at session boundaries."""

import subprocess
import traceback
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest

from agentworks.artifacts.native.common import ancestor_diagnostics
from agentworks.artifacts.native.probe import probe_native
from agentworks.errors import ExternalError
from agentworks.harness_setup.dispatch import destination_id
from agentworks.harness_setup.outcomes import sanitize_setup_diagnostic
from agentworks.harness_setup.runner import SetupRunner
from agentworks.ssh import SSHError, SSHResult
from agentworks.transports import Transport
from tests.artifacts.test_native_probe import probe
from tests.native_setup_fixtures import LocalFixtureTransport


def hostile_text() -> tuple[str, str]:
    secret = uuid4().hex
    middle = len(secret) // 2
    rendered = secret[:middle] + "\x1b[31m\ufffd" + secret[middle:]
    return secret, "\x1b]0;guest-title\x07\n" + rendered + "\n" + "x" * (1024 * 1024)


def assert_safe(text: str, secret: str) -> None:
    assert secret not in text
    assert "\x1b" not in text and "\x07" not in text and "\n" not in text and "\r" not in text
    assert len(text.encode()) <= 2048


def test_raw_redaction_precedes_context_sensitive_terminal_normalization():
    marker = uuid4().hex
    secret = "x" + marker + "\nsecond-part"
    rendered = sanitize_setup_diagnostic("\x1b[" + secret, redactions=(secret,))
    assert marker not in rendered
    assert_safe(rendered, secret)


@pytest.mark.parametrize("stage", ["python-exception", "probe-exception", "probe-status", "probe-protocol"])
def test_session_probe_sanitizes_raised_and_returned_guest_failures(stage):
    secret, payload = hostile_text()
    runner = Mock(spec=Transport)
    runner.logger = None
    failure = SSHError(payload, hint=payload)
    python = SSHResult(0, "AGW_PYTHON3=available\n", "")
    if stage == "python-exception":
        runner.run.side_effect = [failure]
    elif stage == "probe-exception":
        runner.run.side_effect = [python, failure]
    else:
        runner.run.side_effect = [python, SSHResult(19 if stage == "probe-status" else 0, "invalid", payload)]
    with pytest.raises((SSHError, ExternalError)) as caught:
        probe_native(runner, tool="grok", environment={"TOKEN": secret})
    assert_safe(str(caught.value), secret)
    if caught.value.hint:
        assert_safe(caught.value.hint, secret)
    if stage.endswith("exception"):
        assert caught.value is failure


def test_advisory_ancestor_warning_redacts_normalized_runtime_values():
    secret, payload = hostile_text()
    with patch("agentworks.output.warn") as warn, ancestor_diagnostics("grok-build", environment={"TOKEN": secret}):
        raise ExternalError(payload, hint=payload)
    message = warn.call_args.args[0]
    assert secret not in message and "\x1b" not in message and "\x07" not in message
    assert len(message.encode()) < 2300


def test_setup_runner_preserves_error_identity_and_sanitizes_environment():
    secret, payload = hostile_text()
    target = Mock(spec=Transport)
    target.logger = None
    target.default_timeout = 30
    failure = SSHError(payload, hint=payload)
    target.run.side_effect = failure
    with pytest.raises(SSHError) as caught:
        SetupRunner(target, {"TOKEN": secret}).run("fixture")
    assert caught.value is failure
    assert_safe(str(failure), secret)
    assert failure.hint is not None
    assert_safe(failure.hint, secret)


def test_native_discovery_accepts_a_login_banner_without_a_newline(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    target = LocalFixtureTransport(root)
    binary = root / "bin/grok"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    (root / "login-shell").write_text(
        '#!/bin/bash\ncase "$1" in -lc|-lic) ;; *) exit 9 ;; esac\n'
        '. "$HOME/.profile"\nexec /bin/bash --noprofile --norc -c "$2"\n'
    )
    (target.home / ".profile").write_text('printf "fixture-banner"\n')
    assert probe_native(target, tool="grok", environment={}, home=str(target.home)) == str(target.home / ".grok")


@pytest.mark.parametrize("banner", ["", "Welcome\n", "Welcome"])
def test_destination_identity_ignores_noninteractive_startup_output(db, banner):
    vm = db.insert_vm("identity", "site", "vm")
    runner = Mock(spec=Transport)
    runner.logger = None
    runner.run.side_effect = [
        SSHResult(0, banner + "\nAGW_NATIVE_ID=machine-token\n", ""),
        SSHResult(0, banner + "\nAGW_NATIVE_ID=1:2:1000\n", ""),
    ]
    observed = destination_id(vm, runner, location="/home/identity", username="identity")
    runner.run.side_effect = [
        SSHResult(0, "\nAGW_NATIVE_ID=machine-token\n", ""),
        SSHResult(0, "\nAGW_NATIVE_ID=1:2:1000\n", ""),
    ]
    assert observed == destination_id(vm, runner, location="/home/identity", username="identity")


def test_machine_inventory_failure_identifies_the_actual_machine_path(tmp_path):
    path = tmp_path / "etc/codex/skills"
    path.parent.mkdir(parents=True)
    path.write_text("not a directory")
    observed = probe(tmp_path, tool="codex", vm_only=True)
    assert str(path) in observed["problem_paths"]


def test_malformed_native_persona_metadata_keeps_its_path():
    from agentworks.artifacts.native.probe import _inventory_problems

    path = "/fixture/.grok/agents/operator.md"
    problems = _inventory_problems([{"kind": "agent", "path": path, "frontmatter": ""}], (), {})
    assert len(problems) == 1 and problems[0].path == path


def test_firstparty_ssh_timeout_cannot_expose_setenv_through_its_traceback(monkeypatch):
    from agentworks.transports.ssh import SSHTransport

    secret = uuid4().hex
    calls: list[list[str]] = []

    def run(arguments, **kwargs):
        calls.append(arguments)
        if len(calls) == 1:
            return subprocess.CompletedProcess(arguments, 0, "\nAGW_PYTHON3=available\n", "")
        raise subprocess.TimeoutExpired(arguments, 30)

    monkeypatch.setattr("agentworks.transports.ssh.subprocess.run", run)
    with pytest.raises(SSHError) as caught:
        probe_native(SSHTransport("fixture.invalid", retries=1), tool="grok", environment={"TOKEN": secret})
    assert len(calls) == 2 and any(secret in argument for argument in calls[1])
    assert secret not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_safe_domain_causes_survive_without_unsafe_implicit_context():
    from agentworks.errors import StateError
    from agentworks.harness_setup.outcomes import sanitize_setup_error

    secret, payload = hostile_text()
    cause = ExternalError(payload, hint=payload)
    error = StateError(payload)
    error.__cause__ = cause
    error.__context__ = subprocess.TimeoutExpired(["fixture", secret], 30)
    sanitize_setup_error(error, redactions=(secret,))
    assert error.__cause__ is cause and error.__context__ is None
    assert_safe(str(cause), secret)
    assert secret not in "".join(traceback.format_exception(error))
