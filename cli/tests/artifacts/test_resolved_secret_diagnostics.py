"""Diagnostic redaction protects resolved secrets without destroying plain paths."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest

from agentworks.artifacts.native.probe import probe_native
from agentworks.errors import ExternalError
from agentworks.execution.results import Completed, Result
from agentworks.harness_setup.runner import SetupRunner
from agentworks.native_files import NativeFiles
from agentworks.ssh import SSHError, SSHLogger, SSHResult
from agentworks.transports import Transport
from tests.native_setup_fixtures import LocalFixtureTransport
from tests.test_native_harness_setup import transport as transport
from tests.vm_access_support import Admin, RecordingVMAccess


@pytest.mark.parametrize("raised", [False, True])
def test_plain_environment_values_do_not_redact_the_offending_native_path(raised: bool) -> None:
    path = "/home/agt-tester/.grok/agents/operator.md"
    access = RecordingVMAccess([], Admin())
    failure = ExternalError(path)
    access.run_outcomes = [failure if raised else Result(Completed(7), b"", path.encode())]
    with pytest.raises(ExternalError) as caught:
        probe_native(access, tool="grok", environment={"USER": "tester", "LETTER": "e", "TYPE": "agent"})
    assert path in str(caught.value)
    if raised:
        assert caught.value is failure


@pytest.mark.parametrize("delivery", ["ancestor", "session"])
def test_actual_session_delivery_caller_redacts_probe_failures(delivery, monkeypatch):
    from agentworks.artifacts.application import ArtifactApplication, ArtifactFile, OwnedArtifactFile
    from agentworks.artifacts.model import ArtifactGroup, ArtifactInputs, ArtifactOwner
    from agentworks.artifacts.native.common import NativeSessionArtifacts, prepare_session_delivery
    from agentworks.artifacts.routing import RoutingResult
    from agentworks.artifacts.session import prepare_session_artifacts
    from agentworks.artifacts.state import CapturedArtifacts
    from agentworks.secrets.orchestration import SecretTarget
    from agentworks.sessions.templates import ResolvedSessionTemplate

    secret = uuid4().hex
    path = "/home/worker/.codex/agents/operator.toml"
    ancestor = OwnedArtifactFile(path=path, sha256="a" * 64, origins=("b" * 64,))
    group = ArtifactGroup(ArtifactOwner("session", "session", "fixture"))
    monkeypatch.setattr(
        "agentworks.artifacts.session.capture_owner", lambda *a, **k: CapturedArtifacts("c" * 64, group)
    )
    monkeypatch.setattr(
        "agentworks.artifacts.routing.session_artifacts",
        lambda *a, **k: RoutingResult(
            ArtifactInputs(local=group), ancestor_files=(ancestor,) if delivery == "ancestor" else ()
        ),
    )
    monkeypatch.setattr("agentworks.artifacts.session.read_native_setup", lambda *a: Mock(records=()))
    access = RecordingVMAccess([], Admin())
    access.run_outcomes = [Result(Completed(9), b"", ("login failed TOKEN=" + secret + " " + path).encode())]
    warnings: list[str] = []
    monkeypatch.setattr("agentworks.output.warn", warnings.append)
    prepared = prepare_session_artifacts(
        Mock(),
        Mock(),
        name="fixture",
        template=ResolvedSessionTemplate("fixture"),
        vm=Mock(),
        workspace=Mock(),
        agent_name="worker",
        access=access,
        environment={"TOKEN": secret},
        secret_target=SecretTarget(vm={}),
        linux_user="worker",
        redactions=(secret,),
    )
    plan = NativeSessionArtifacts(
        application=ArtifactApplication(
            files=(ArtifactFile(prepared.context.directory + "/rule.md", b"rule", ("b" * 64,)),)
        )
        if delivery == "session"
        else ArtifactApplication()
    )
    if delivery == "ancestor":
        actual, _ = prepare_session_delivery(
            access,
            tool="codex",
            integration="codex",
            context=prepared.context,
            render=lambda _: plan,
            workspace="/work",
        )
        assert actual == plan and len(warnings) == 1
        diagnostic = warnings[0]
    else:
        with pytest.raises(ExternalError) as caught:
            prepare_session_delivery(
                access,
                tool="codex",
                integration="codex",
                context=prepared.context,
                render=lambda _: plan,
                workspace="/work",
            )
        diagnostic = str(caught.value)
    assert secret not in diagnostic and path in diagnostic


def test_logger_only_secret_is_removed_before_terminal_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        SetupRunner(runner, {"USER": "tester", "LETTER": "e"}).run("fixture")
    assert caught.value is failure
    assert marker not in str(failure) and marker not in (failure.hint or "")
    assert path in str(failure)


def test_logger_only_secret_is_removed_from_setup_probe_diagnostics(
    db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A secret only the operation log registered reaches the setup probe, which removes it from its error."""
    from agentworks.artifacts.model import ArtifactType
    from agentworks.harness_setup.inputs import SetupInputs
    from agentworks.harness_setup.lifecycle import apply_agent_setup
    from agentworks.harness_setup.model import NativeSetupState
    from agentworks.plugins.claude.harness_integration import ClaudeCodeIntegration
    from agentworks.secrets.orchestration import SecretTarget
    from tests.artifacts._fixtures import received
    from tests.artifacts.test_native_delivery import artifact
    from tests.vm_access_support import install_access_recorder

    marker = uuid4().hex.upper()
    secret = "x" + marker + "\nsecond-part"
    path = "/home/worker/.claude/skills/review/SKILL.md"
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path / "logs")
    install_access_recorder(monkeypatch)
    monkeypatch.setattr("agentworks.transports.transport_for_user", lambda *args, **kwargs: Mock(spec=Transport))
    monkeypatch.setattr("agentworks.harness_setup.lifecycle.site_platform_name", lambda *args: "fixture")
    invocations = []

    def capture(db, registry, inputs, invocation, **kwargs):
        invocations.append(invocation)
        return NativeSetupState()

    monkeypatch.setattr("agentworks.harness_setup.lifecycle.run_setup", capture)
    apply_agent_setup(
        db,
        Mock(),
        Mock(),
        inputs=SetupInputs("agent", "agent", "agent", {}, SecretTarget(vm={})),
        vm=db.insert_vm("vm", "site", "host"),
        username="worker",
        values={"TOKEN": "resolved-value"},
        logger=SSHLogger("resolved-values", "probe", redactions=(secret,)),
        held=Mock(),
        operation="agent-init",
        failures=[],
    )
    (invocation,) = invocations
    access = RecordingVMAccess([], Admin())
    access.run_outcomes = [Result(Completed(1), b"", ("\x1b[" + secret + " " + path).encode())]
    setup = ClaudeCodeIntegration.for_setup(owner_kind="agent", owner_name="agent", facet="user", config={})
    with pytest.raises(ExternalError) as caught:
        setup.user_init(replace(invocation, access=access, artifacts=received(artifact(ArtifactType.SKILL))))
    assert marker not in str(caught.value) and marker not in (caught.value.hint or "")
    assert path in str(caught.value)


@pytest.mark.skipif(sys.platform != "linux", reason="executes Linux guest native-file helpers")
def test_native_staging_ignores_a_startup_banner_and_cleans_its_directory(
    transport: LocalFixtureTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = transport.run

    def with_banner(command: str, **kwargs: Any) -> SSHResult:
        result = original(command, **kwargs)
        return SSHResult(result.returncode, "fixture welcome" + result.stdout, result.stderr)

    monkeypatch.setattr(transport, "run", with_banner)
    with NativeFiles(transport) as files:
        destination = str(transport.home / "settings.json")
        assert not files.directory(str(transport.home / "config"))
        assert files.directory(str(transport.home / "config"), create=True)
        assert files.read(destination) is None
        assert files.fingerprint(destination) is None
        files.publish(destination, b"{}", expected=None)
        assert files.read(destination) == b"{}"
        observed = files.fingerprint(destination)
        assert observed is not None
        files.remove(destination, expected=observed[0])
        assert files.read(destination) is None
    assert not list((transport.root / "tmp").iterdir())
