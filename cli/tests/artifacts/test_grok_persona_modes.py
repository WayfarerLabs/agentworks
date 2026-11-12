"""Private session personas need verified native delivery in every launch mode."""

from collections.abc import Sequence

import pytest

from agentworks.artifacts.model import ArtifactType
from agentworks.artifacts.session import validate_session_application
from agentworks.plugins.grok.artifacts import session_artifacts
from agentworks.plugins.grok.harness_integration import GrokBuildConfig
from tests.artifacts.test_native_delivery import artifact, context

_MODES = [
    [],
    ["--model", "example"],
    ["--", "--single", "task"],
    ["-p", "task"],
    ["-ptask"],
    ["--single=task"],
    ["--prompt-file", "/fixture/prompt"],
    ["--prompt-json=[]"],
]


@pytest.mark.parametrize("args", _MODES)
@pytest.mark.parametrize("workarounds", [[], ["session-agent-definitions"]])
def test_personas_remain_unhandled_in_every_mode(args: Sequence[str], workarounds: list[str]) -> None:
    item = artifact(ArtifactType.AGENT, options={"grok-build": {"hooks": {}}})
    prepared = context(item)
    result = session_artifacts(prepared, extra_args=args, enabled_workarounds=workarounds)
    assert result.argv == ()
    assert result.application.files == ()
    assert tuple(deferred.input_id for deferred in result.application.deferred) == (item.identity,)
    assert validate_session_application(result.application, prepared, integration="grok-build") == result.application


@pytest.mark.parametrize("args", [[], ["--single", "task"]])
def test_unhandled_personas_do_not_block_independent_rules_and_hints(args: Sequence[str]) -> None:
    hint, rule, skill, agent = (
        artifact(kind, name=kind.value)
        for kind in (ArtifactType.HINT, ArtifactType.RULE, ArtifactType.SKILL, ArtifactType.AGENT)
    )
    prepared = context(agent, rule, hint, skill)
    result = session_artifacts(
        prepared,
        extra_args=args,
        enabled_workarounds=["session-home-skills", "session-agent-definitions"],
    )
    assert result.argv == () and result.application.files
    assert {deferred.input_id for deferred in result.application.deferred} == {agent.identity}
    assert validate_session_application(result.application, prepared, integration="grok-build") == result.application


def test_compatible_workaround_name_does_not_restrict_operator_native_selection() -> None:
    config = GrokBuildConfig(agent="operator-persona")
    item = artifact(ArtifactType.AGENT)
    result = session_artifacts(
        context(item),
        extra_args=["--agents", '{"operator-persona": {"prompt": "operator content"}}', "--no-subagents"],
        enabled_workarounds=config.enabled_workarounds,
    )
    assert config.agent == "operator-persona"
    assert result.argv == ()
    assert result.application.files == ()
    assert tuple(deferred.input_id for deferred in result.application.deferred) == (item.identity,)


def test_unsupported_persona_allows_launch_without_native_artifact_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.capabilities.base import RunContext
    from agentworks.capabilities.harness_integration import HarnessLaunchIntent
    from agentworks.plugins.grok.harness_integration import GrokBuildIntegration

    monkeypatch.setattr(
        "agentworks.artifacts.native.probe.probe_native",
        lambda *args, **kwargs: pytest.fail("unexpected artifact probe"),
    )
    item = artifact(ArtifactType.AGENT)
    integration = GrokBuildIntegration(
        "grok-build",
        {"extra_args": ["--single", "task"]},
        session_name="s1",
        vm_name="box",
        workspace_name="ws",
        workspace_path="/ws",
        target=None,
        admin=True,
        state={},
        artifact_context=context(item),
    )
    monkeypatch.setattr(integration, "_resume_or_launch", lambda *args, **kwargs: "native-command")
    result = integration.start(RunContext(), intent=HarnessLaunchIntent.CREATE)
    assert result.command == "native-command"
    assert result.artifacts.files == ()
    assert tuple(deferred.input_id for deferred in result.artifacts.deferred) == (item.identity,)
