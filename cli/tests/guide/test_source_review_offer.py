"""The no-topic guide owns the optional canonical source-review offer."""

from __future__ import annotations

import socket
import urllib.request
from pathlib import Path

from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.errors import ConfigError
from agentworks.guide import ConsentBoundary, GuideMode
from agentworks.guide.contributions import guide_contributions, source_review_actions
from agentworks.guide.render import render_index
from agentworks.guide.service import render_guide


def _broken() -> object:
    raise ConfigError("clean-home config is absent")


def test_source_review_actions_are_scoped_inert_and_independent() -> None:
    focused, full = source_review_actions()

    assert [str(action.id) for action in (focused, full)] == [
        "inspect-focused-source",
        "inspect-full-source",
    ]
    assert all(action.consent is ConsentBoundary.READ_CANONICAL_SOURCE for action in (focused, full))
    assert all(action.command is None and action.verification is None for action in (focused, full))
    assert all(tuple(item.name for item in action.required_inputs) == ("VERSION",) for action in (focused, full))
    assert focused.manual_steps is not None
    assert full.manual_steps is not None
    for path in (
        "cli/pyproject.toml",
        "cli/uv.lock",
        "cli/agentworks/",
        "packaging/agentworks/",
        "plugins/claude-code/agentworks/",
        "plugins/codex/agentworks/",
        ".github/workflows/release.yml",
    ):
        assert path in focused.manual_steps
    for action in (focused, full):
        assert "Install or update authorization alone is not source-review authorization" in action.precondition
        assert "Do not execute candidate code" in (action.manual_steps or "")
        assert "Make no canonical-repository request and claim no source review" in action.refusal_alternative
        assert "separately authorized or completed install or update" in action.refusal_alternative
    assert "substantial" in full.precondition
    assert "significant model usage" in full.precondition


def test_no_topic_offer_uses_exact_installed_tag_and_preserves_assistant_decision(
    monkeypatch,
) -> None:
    monkeypatch.setattr("agentworks.version.resolve_version", lambda: "9.8.7")

    human = render_index(guide_contributions(), GuideMode.HUMAN)
    agent = render_index(guide_contributions(), GuideMode.AGENT)

    for markdown in (human, agent):
        contract_at = markdown.index("The Agentworks assistant agent runs on the intended workstation")
        review_at = markdown.index("## ⟦AGW framework⟧ Optional canonical source review")
        intent_at = markdown.index("## ⟦AGW framework⟧ Intent map")
        topics_at = markdown.index("## ⟦AGW framework⟧ Topics")
        assert contract_at < review_at < intent_at < topics_at
        assert "Installed canonical review target: `v9.8.7`" in markdown
        assert "https://github.com/WayfarerLabs/agentworks/tree/v9.8.7" in markdown
        assert "offer three concise choices once: focused review, full review, or decline review" in markdown
        assert "full review may consume significant model usage" in markdown
        assert "Install or update authorization does not authorize review" in markdown
        assert "review does not authorize installation, update, or candidate execution" in markdown
        assert "Candidate execution is a separate action" in markdown
        assert markdown.count("Authorization class: `read-canonical-source`") == 2
        assert "### `inspect-focused-source`" in markdown
        assert "### `inspect-full-source`" in markdown
        assert "does not route the request or grant authority" in markdown

    human_semantics = human.replace("## ⟦AGW framework⟧ Security and consent", "## ⟦AGW framework⟧ Disclosure")
    agent_semantics = agent.replace("## ⟦AGW framework⟧ Agent operating contract", "## ⟦AGW framework⟧ Disclosure")
    assert human_semantics == agent_semantics


def test_no_topic_offer_fails_closed_when_distribution_has_no_stable_tag(monkeypatch) -> None:
    monkeypatch.setattr("agentworks.version.resolve_version", lambda: "unknown")

    markdown = render_index(guide_contributions(), GuideMode.AGENT)

    assert "does not report a canonical stable release tag" in markdown
    assert "exact intended or installed stable `VERSION`" in markdown
    assert "tree/vunknown" not in markdown
    assert "inspect-focused-source" in markdown
    assert "inspect-full-source" in markdown


def test_clean_home_agent_guide_surfaces_offer_without_network_or_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("no-topic guide attempted network access")

    monkeypatch.setattr("agentworks.config.CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr("agentworks.version.resolve_version", lambda: "0.14.0")
    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(urllib.request, "urlopen", fail_network)

    result = CliRunner().invoke(app, ["guide", "--agent"])

    assert result.exit_code == 1
    assert "Installed canonical review target: `v0.14.0`" in result.stdout
    assert "### `inspect-focused-source`" in result.stdout
    assert "### `inspect-full-source`" in result.stdout
    assert "clean-home config is absent" not in result.stdout
    assert not (tmp_path / "config.toml").exists()


def test_no_topic_rendering_attempts_no_network_when_configuration_is_absent(monkeypatch) -> None:
    def fail_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("no-topic guide attempted network access")

    monkeypatch.setattr("agentworks.version.resolve_version", lambda: "0.14.0")
    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(urllib.request, "urlopen", fail_network)

    response = render_guide((), GuideMode.AGENT, load_config_fn=_broken)

    assert response.exit_code == 1
    assert "Optional canonical source review" in response.markdown
    assert "Configuration error: clean-home config is absent" in response.markdown
