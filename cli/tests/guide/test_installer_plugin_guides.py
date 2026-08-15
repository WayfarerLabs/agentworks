"""Boundaries for first-party guide content owned by installer plugins."""

from __future__ import annotations

import importlib
import importlib.resources
import sys

import pytest

from agentworks.guide import (
    ActionId,
    ActionList,
    BlockId,
    ConceptAnchor,
    ConsentBoundary,
    GuideAction,
    Overview,
    TopicContribution,
    TopicSlug,
    validate_guide_action,
)
from agentworks.guide import service as guide_service
from agentworks.plugins.base import Plugin


def _topic(slug: str) -> TopicContribution:
    return TopicContribution(
        TopicSlug(slug),
        "Fixture topic",
        "Fixture summary.",
        ConceptAnchor(slug),
        (Overview(BlockId("overview"), "Fixture content."),),
    )


def test_installer_plugin_imports_do_not_read_guide_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plugin registration stays independent of packaged guide Markdown."""

    def forbid_guide_resource_io(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("plugin import must not read guide resources")

    monkeypatch.setattr(importlib.resources, "files", forbid_guide_resource_io)
    for module in ("agentworks.plugins.apt", "agentworks.plugins.install_command"):
        sys.modules.pop(module, None)
        importlib.import_module(module)


def test_installer_plugin_verification_actions_read_only_configured_resources() -> None:
    apt_contributions = importlib.import_module("agentworks.plugins.apt.guide_contributions").guide_contributions
    install_command_contributions = importlib.import_module(
        "agentworks.plugins.install_command.guide_contributions"
    ).guide_contributions

    expected_command = ("agw", "resource", "list", "--origin", "plugin", "--include-disabled")
    for contribution in (*apt_contributions(), *install_command_contributions()):
        actions = next(block.actions for block in contribution.blocks if isinstance(block, ActionList))
        verification = next(action for action in actions if str(action.id).startswith("verify-"))
        assert verification.consent is ConsentBoundary.READ_CONFIGURED_STATE
        assert verification.command == expected_command


def test_unavailable_first_party_guide_content_keeps_descriptor_and_unrelated_topics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apt = Plugin("apt", guide_topics=(_topic("plugin/apt/descriptor"),))
    healthy = Plugin("healthy", guide_topics=(_topic("plugin/healthy/overview"),))

    def unavailable() -> tuple[TopicContribution, ...]:
        raise OSError("missing packaged Markdown")

    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {apt.name: apt, healthy.name: healthy})
    monkeypatch.setitem(guide_service._FIRST_PARTY_PLUGIN_GUIDE_CONTRIBUTIONS, "apt", unavailable)

    catalog = guide_service.build_authored_catalog()

    assert {"concept-onboarding", "plugin/apt/descriptor", "plugin/healthy/overview"} <= set(catalog.names())
    assert [(issue.error.source, issue.error.topic, issue.error.field_path) for issue in catalog.issues] == [
        ("system-plugin:apt", None, "guide-content")
    ]


def test_first_party_action_validation_failure_is_scoped_to_its_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    apt = Plugin("apt", guide_topics=(_topic("plugin/apt/descriptor"),))

    def invalid_actions() -> tuple[TopicContribution, ...]:
        validate_guide_action(
            GuideAction(
                ActionId("invalid-action"),
                "Fixture precondition.",
                (),
                ConsentBoundary.READ_CONFIGURED_STATE,
                ("agw", "--Invalid"),
                "Fixture expected state.",
                None,
                "Fixture refusal alternative.",
            ),
            source="system-plugin:apt:plugin/apt/overview",
        )
        raise AssertionError("invalid action unexpectedly passed validation")

    monkeypatch.setattr("agentworks.plugins.SYSTEM_PLUGINS", {apt.name: apt})
    monkeypatch.setitem(guide_service._FIRST_PARTY_PLUGIN_GUIDE_CONTRIBUTIONS, "apt", invalid_actions)

    catalog = guide_service.build_authored_catalog()

    assert "plugin/apt/descriptor" in catalog.names()
    assert [(issue.error.source, issue.error.topic, issue.error.field_path) for issue in catalog.issues] == [
        ("system-plugin:apt", None, "value.command[1]")
    ]
