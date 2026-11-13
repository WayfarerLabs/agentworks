"""Guest failures remain bounded and redacted at session boundaries."""

import shlex
import subprocess
import sys
import traceback
from unittest.mock import patch
from uuid import uuid4

import pytest

from agentworks.artifacts.native.common import ancestor_diagnostics
from agentworks.artifacts.native.probe import probe_native
from agentworks.errors import ExternalError
from agentworks.execution.results import Completed, Result
from agentworks.harness_setup.outcomes import sanitize_setup_diagnostic
from tests.artifacts.test_native_probe import probe
from tests.vm_access_support import Admin, RecordingVMAccess, account_shell, local_vm_access


def hostile_text(representation: str = "raw") -> tuple[str, str]:
    secret = uuid4().hex + "_'both\"_end"
    encoded = {"raw": secret, "shell": shlex.quote(secret), "repr": repr(secret)}[representation]
    middle = len(encoded) // 2
    rendered = encoded[:middle] + "\x1b[31m\ufffd" + encoded[middle:]
    return secret, "\x1b]0;guest-title\x07\n" + rendered + "\n" + "x" * (1024 * 1024)


def assert_safe(text: str, secret: str) -> None:
    assert all(fragment not in text for fragment in (secret, secret[: len(secret) // 2], secret[len(secret) // 2 :]))
    assert "\x1b" not in text and "\x07" not in text and "\n" not in text and "\r" not in text
    assert len(text.encode()) <= 2048


def test_raw_redaction_precedes_context_sensitive_terminal_normalization():
    marker = uuid4().hex
    secret = "x" + marker + "\nsecond-part"
    rendered = sanitize_setup_diagnostic("\x1b[" + secret, redactions=(secret,))
    assert marker not in rendered
    assert_safe(rendered, secret)


@pytest.mark.windows
@pytest.mark.parametrize("representation", ["raw", "shell", "repr"])
@pytest.mark.parametrize("stage", ["probe-exception", "probe-status", "probe-protocol"])
def test_session_probe_sanitizes_raised_and_returned_guest_failures(stage, representation):
    secret, payload = hostile_text(representation)
    access = RecordingVMAccess([], Admin())
    failure = ExternalError(payload, hint=payload)
    if stage == "probe-exception":
        access.run_outcomes = [failure]
    else:
        access.run_outcomes = [Result(Completed(19 if stage == "probe-status" else 0), b"invalid", payload.encode())]
    with pytest.raises(ExternalError) as caught:
        probe_native(access, tool="grok", environment={"TOKEN": secret}, redactions=(secret,))
    assert_safe(str(caught.value), secret)
    if caught.value.hint:
        assert_safe(caught.value.hint, secret)
    if stage.endswith("exception"):
        assert caught.value is failure


@pytest.mark.windows
@pytest.mark.parametrize("representation", ["raw", "shell", "repr"])
def test_advisory_ancestor_warning_redacts_normalized_runtime_values(representation):
    secret, payload = hostile_text(representation)
    with patch("agentworks.output.warn") as warn, ancestor_diagnostics("grok-build", redactions=(secret,)):
        raise ExternalError(payload, hint=payload)
    message = warn.call_args.args[0]
    assert secret[: len(secret) // 2] not in message and "\x1b" not in message and "\x07" not in message
    assert len(message.encode()) < 2300


@pytest.mark.skipif(sys.platform != "linux", reason="runs agw-launch and the native probe locally")
def test_native_discovery_accepts_a_login_banner_without_a_newline(tmp_path, monkeypatch):
    home = tmp_path / "home"
    binary = tmp_path / "bin/grok"
    binary.parent.mkdir()
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    home.mkdir()
    (home / ".profile").write_text('printf "fixture-banner"\n')
    access = local_vm_access(monkeypatch, home=home, shell=account_shell(tmp_path))
    environment = {"PATH": f"{binary.parent}:/usr/bin:/bin"}
    assert probe_native(access, tool="grok", environment=environment, home=str(home)) == str(home / ".grok")


@pytest.mark.skipif(sys.platform != "linux", reason="executes the Linux guest inventory helper locally")
def test_machine_inventory_failure_identifies_the_actual_machine_path(tmp_path):
    root = tmp_path / r"fixture\agent\target"
    path = root / "etc/codex/skills"
    path.parent.mkdir(parents=True)
    path.write_text("not a directory")
    observed = probe(root, tool="codex", vm_only=True)
    assert str(path) in observed["problem_paths"]


def test_malformed_native_persona_metadata_keeps_its_path():
    from agentworks.artifacts.native.probe import _inventory_problems

    path = "/fixture/.grok/agents/operator.md"
    problems = _inventory_problems([{"kind": "agent", "path": path, "frontmatter": ""}], (), {})
    assert len(problems) == 1 and problems[0].path == path


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
