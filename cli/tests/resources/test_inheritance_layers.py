"""``merge_layers`` order is a contract, and the thing it must agree with
is each kind's own resolver.

Callers read "the last layer that declared X" off this order to decide
which template an edge belongs to. If the order diverged from the merge
the resolver actually performs, an edge would be attributed to a layer
whose value the resolver overrode: a wrong answer that nothing else would
catch, since both orders produce a plausible-looking name.

So each kind is pinned by FOLDING the layers and getting the resolver's
own merged result, rather than by restating the expected order, which
would only pin this module against itself.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from agentworks.agents.template import AgentTemplate
from agentworks.agents.templates import effective_template as agent_effective
from agentworks.env.entry import EnvEntry
from agentworks.resources.inheritance import declarers, merge_layers
from agentworks.sessions.template import SessionTemplate
from agentworks.sessions.templates import effective_template as session_effective
from agentworks.vms.template import VMTemplate
from agentworks.vms.templates import effective_template as vm_effective
from agentworks.workspaces.template import WorkspaceTemplate
from agentworks.workspaces.templates import effective_template as workspace_effective


def _env(secret: str) -> dict[str, EnvEntry]:
    return {"K": EnvEntry(key="K", secret=secret)}


def _diamond(build: Callable[..., Any]) -> dict[str, Any]:
    """``kid`` inherits ``left`` then ``right``, both inheriting ``root``.

    A diamond and an ordering case at once: every layer declares the same
    env key, so the folded result is decided entirely by the order, and
    ``right`` is the one a left-to-right merge lets win.
    """
    return {
        "root": build(name="root", env=_env("root-secret")),
        "left": build(name="left", inherits=["root"], env=_env("left-secret")),
        "right": build(name="right", inherits=["root"], env=_env("right-secret")),
        "kid": build(name="kid", inherits=["left", "right"]),
    }


@pytest.mark.parametrize(
    ("build", "resolve"),
    [
        (VMTemplate, vm_effective),
        (WorkspaceTemplate, workspace_effective),
        (AgentTemplate, agent_effective),
        (SessionTemplate, session_effective),
    ],
    ids=["vm-template", "workspace-template", "agent-template", "session-template"],
)
def test_folding_the_layers_reproduces_the_resolvers_own_merge(
    build: Callable[..., Any],
    resolve: Callable[..., Any],
) -> None:
    rows = _diamond(build)
    folded: dict[str, EnvEntry] = {}
    for layer in merge_layers(rows, "kid"):
        folded.update(layer.env or {})

    resolved = resolve(rows, "kid")
    # The session kind's effective value wraps its resolved template,
    # because it carries the harness pair beside it.
    effective = resolved.resolved if hasattr(resolved, "resolved") else resolved
    assert folded == effective.env


def test_the_declarer_of_a_contested_key_is_the_layer_whose_value_survived() -> None:
    rows = _diamond(VMTemplate)
    layers = merge_layers(rows, "kid")
    by_env = declarers(layers, "vm-template", lambda t: t.env)
    assert by_env["K"] == ("vm-template", "right")
    assert vm_effective(rows, "kid").env["K"].secret == "right-secret"


def test_a_cyclic_chain_stops_rather_than_raising() -> None:
    """The build walk may not raise; the registry's cycle pass is what
    reports the loop."""
    rows = {
        "a": VMTemplate(name="a", inherits=["b"]),
        "b": VMTemplate(name="b", inherits=["a"]),
    }
    assert [layer.name for layer in merge_layers(rows, "a")] == ["b", "a"]


def test_an_unresolved_parent_contributes_nothing() -> None:
    rows = {"kid": VMTemplate(name="kid", inherits=["missing"])}
    assert [layer.name for layer in merge_layers(rows, "kid")] == ["kid"]
