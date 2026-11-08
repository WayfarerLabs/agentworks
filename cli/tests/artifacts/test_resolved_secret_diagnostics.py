"""Diagnostic redaction protects resolved secrets without destroying plain paths."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest

from agentworks.artifacts.native.probe import probe_native
from agentworks.errors import ExternalError
from agentworks.harness_setup.runner import SetupRunner
from agentworks.native_files import NativeFiles
from agentworks.ssh import SSHError, SSHLogger, SSHResult
from agentworks.transports import Transport
from tests.native_setup_fixtures import LocalFixtureTransport
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
    runner = Mock(spec=Transport)
    runner.logger = None
    runner.run.side_effect = [
        SSHResult(0, "\nAGW_PYTHON3=available\n", ""),
        SSHResult(9, "", "login failed TOKEN=" + secret + " " + path),
    ]
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
        runner=runner,
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
            runner,
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
                runner,
                tool="codex",
                integration="codex",
                context=prepared.context,
                render=lambda _: plan,
                workspace="/work",
            )
        diagnostic = str(caught.value)
    assert secret not in diagnostic and path in diagnostic


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
