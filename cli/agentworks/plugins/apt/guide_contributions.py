"""Guide teaching owned by the optional apt catalog plugin."""

from __future__ import annotations

from importlib.resources import files

from agentworks.guide.contract import (
    ActionId,
    ActionInput,
    ActionList,
    AgentContract,
    BlockId,
    ConceptAnchor,
    ConsentBoundary,
    GuideAction,
    Overview,
    Teaching,
    TopicContribution,
    TopicSlug,
    validate_guide_action,
)

_TOPIC = "plugin/apt/overview"


def _markdown(block_id: str) -> str:
    resource = files("agentworks.plugins.apt").joinpath("guide-content", "overview", f"{block_id}.md")
    return resource.read_text(encoding="utf-8").strip()


def _actions() -> tuple[GuideAction, ...]:
    actions = (
        GuideAction(
            ActionId("enable-apt-plugin"),
            "The operator chose the shipped apt catalog after reviewing its disabled state and dependencies.",
            (ActionInput("CONFIG_PATH", "The config.toml file to change.", True),),
            ConsentBoundary.MUTATE_AGENTWORKS,
            None,
            "Only CONFIG_PATH changes. Its [plugins].system list retains existing names and includes apt.",
            None,
            "Leave CONFIG_PATH unchanged and the apt plugin disabled.",
            "Edit only CONFIG_PATH. Add apt to [plugins].system, preserving every existing plugin name. "
            "Create the [plugins] section and system list only when they are absent.",
        ),
        GuideAction(
            ActionId("verify-apt-plugin"),
            "The operator wants a read-only check after deciding whether to enable the apt catalog.",
            (),
            ConsentBoundary.READ_CONFIGURED_STATE,
            ("agw", "doctor"),
            "The System plugins roster identifies apt and reports its configured enabled or disabled state.",
            None,
            "Do not read configured state; the plugin state remains unchanged.",
        ),
    )
    return tuple(validate_guide_action(action, f"system-plugin:apt:{_TOPIC}") for action in actions)


def guide_contributions() -> tuple[TopicContribution, ...]:
    """Return inert apt teaching loaded from package resources."""
    return (
        TopicContribution(
            TopicSlug(_TOPIC),
            "Optional apt catalog",
            "Enable the shipped apt sources and package sets only when a template selects them.",
            ConceptAnchor(_TOPIC),
            (
                Overview(BlockId("overview"), _markdown("overview")),
                AgentContract(BlockId("agent-contract"), _markdown("agent-contract")),
                Teaching(BlockId("teaching"), _markdown("teaching")),
                ActionList(BlockId("actions"), _actions()),
            ),
        ),
    )
