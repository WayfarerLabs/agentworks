"""Guide teaching owned by the optional install-command catalog plugin."""

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

_TOPIC = "plugin/install-command/overview"


def _markdown(block_id: str) -> str:
    resource = files("agentworks.plugins.install_command").joinpath("guide-content", "overview", f"{block_id}.md")
    return resource.read_text(encoding="utf-8").strip()


def _actions() -> tuple[GuideAction, ...]:
    actions = (
        GuideAction(
            ActionId("enable-install-command-plugin"),
            "The operator chose the shipped user install-command catalog after reviewing its disabled state.",
            (ActionInput("CONFIG_PATH", "The config.toml file to change.", True),),
            ConsentBoundary.MUTATE_AGENTWORKS,
            None,
            "Only CONFIG_PATH changes. Its [plugins].system list retains existing names and includes install-command.",
            None,
            "Leave CONFIG_PATH unchanged and the install-command plugin disabled.",
            "Edit only CONFIG_PATH. Add install-command to [plugins].system, preserving every existing plugin name. "
            "Create the [plugins] section and system list only when they are absent.",
        ),
        GuideAction(
            ActionId("verify-install-command-plugin"),
            "The operator wants a read-only check after deciding whether to enable the install-command catalog.",
            (),
            ConsentBoundary.READ_CONFIGURED_STATE,
            ("agw", "guide", "user-install-command/uv"),
            "The user-install-command/uv State block reports its system-plugin origin and enabled or disabled "
            "registry state.",
            None,
            "Do not read configured state; the plugin state remains unchanged.",
        ),
    )
    return tuple(validate_guide_action(action, f"system-plugin:install-command:{_TOPIC}") for action in actions)


def guide_contributions() -> tuple[TopicContribution, ...]:
    """Return inert install-command teaching loaded from package resources."""
    return (
        TopicContribution(
            TopicSlug(_TOPIC),
            "Optional install-command catalog",
            "Enable shipped user install commands only when admin or agent templates select them.",
            ConceptAnchor(_TOPIC),
            (
                Overview(BlockId("overview"), _markdown("overview")),
                AgentContract(BlockId("agent-contract"), _markdown("agent-contract")),
                Teaching(BlockId("teaching"), _markdown("teaching")),
                ActionList(BlockId("actions"), _actions()),
            ),
        ),
    )
