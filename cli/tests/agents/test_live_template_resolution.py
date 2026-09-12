"""Live agent resolution only loads migration context for stored legacy fields."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from agentworks.agents import templates
from agentworks.agents.template import AgentTemplate
from agentworks.db import VersionedPayload
from agentworks.origin import Origin
from agentworks.resources.inheritance import LayerSourceKind
from agentworks.resources.registry import Registry
from agentworks.schema import CapabilityBlock

if TYPE_CHECKING:
    from agentworks.db import Database
    from agentworks.db.instance_state import JsonObject


@pytest.mark.parametrize("provenance", [False, True])
@pytest.mark.parametrize("overlay", [None, {"shell": "zsh"}, {"claude_plugins": ["extra@market"]}])
def test_live_resolution_walks_base_only_when_legacy_context_is_needed(
    db: Database, provenance: bool, overlay: JsonObject | None
) -> None:
    registry = Registry.empty()
    registry.add(
        "agent-template",
        "base",
        AgentTemplate.model_validate(
            {
                "name": "base",
                "harness_integrations": [CapabilityBlock.of("claude-code", plugins=["base@market"])],
            }
        ),
        Origin.built_in(source="fixture"),
    )
    registry.add(
        "agent-template",
        "child",
        AgentTemplate.model_validate({"name": "child", "inherits": ["base"]}),
        Origin.built_in(source="fixture"),
    )
    if overlay is not None:
        db.instance_state.put_desired_overlay("agent", "instance", VersionedPayload(1, overlay))

    with patch.object(
        templates, "resolve_from_dict_with_provenance", wraps=templates.resolve_from_dict_with_provenance
    ) as walk:
        if provenance:
            resolution = templates.resolve_live_template_with_provenance(db, registry, "instance", "child")
            value = resolution.value
            if overlay == {"shell": "zsh"}:
                assert resolution.provenance[("shell",)][-1].kind is LayerSourceKind.INSTANCE
        else:
            value = templates.resolve_live_template(db, registry, "instance", "child")

    legacy = overlay is not None and "claude_plugins" in overlay
    assert walk.call_count == (2 if legacy else 1)
    assert value.shell == ("zsh" if overlay == {"shell": "zsh"} else "bash")
    assert value.harness_integrations[0].config["plugins"] == (
        ["base@market", "extra@market"] if legacy else ["base@market"]
    )
