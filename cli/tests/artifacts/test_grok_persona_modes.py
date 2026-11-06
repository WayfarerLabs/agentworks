"""A headless-only native carrier cannot claim interactive persona delivery."""

import json
from collections.abc import Sequence

import pytest

from agentworks.artifacts.model import ArtifactType
from agentworks.plugins.grok.artifacts import session_artifacts
from tests.artifacts.test_native_delivery import artifact, context


@pytest.mark.parametrize("args", [[], ["--model", "example"], ["--", "--single", "task"]])
def test_interactive_personas_remain_unhandled_even_with_workaround(args: Sequence[str]) -> None:
    item = artifact(ArtifactType.AGENT)
    result = session_artifacts(
        context(item), configured=None, extra_args=args, enabled_workarounds=["session-agent-definitions"]
    )
    assert result.argv == ()
    assert result.application.files == ()
    assert tuple(deferred.input_id for deferred in result.application.deferred) == (item.identity,)


@pytest.mark.parametrize(
    "args",
    [["-p", "task"], ["-ptask"], ["--single=task"], ["--prompt-file", "/fixture/prompt"], ["--prompt-json=[]"]],
)
def test_headless_personas_use_the_supported_native_carrier(args: Sequence[str]) -> None:
    item = artifact(ArtifactType.AGENT)
    result = session_artifacts(
        context(item), configured=None, extra_args=args, enabled_workarounds=["session-agent-definitions"]
    )
    definitions = json.loads(result.argv[result.argv.index("--agents") + 1])
    assert definitions[item.content.name]["prompt"] == item.content.text
    assert definitions[item.content.name]["description"] == item.content.description
    assert result.application.deferred == ()
    assert all(file.origins == (item.origin_identity,) for file in result.application.files)


def test_unhandled_interactive_personas_do_not_block_independent_rules() -> None:
    agent, rule = artifact(ArtifactType.AGENT), artifact(ArtifactType.RULE)
    result = session_artifacts(
        context(agent, rule),
        configured=None,
        extra_args=[],
        enabled_workarounds=["session-rules", "session-agent-definitions"],
    )
    assert "--rules" in result.argv and "--agents" not in result.argv
    assert tuple(deferred.input_id for deferred in result.application.deferred) == (agent.identity,)
