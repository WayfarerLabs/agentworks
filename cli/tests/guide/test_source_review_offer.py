"""The source-review topic owns bounded canonical review actions."""

from __future__ import annotations

import socket
import urllib.request
from pathlib import Path

from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.errors import ConfigError, ConfigFileNotFoundError
from agentworks.guide import ActionList, ConsentBoundary, GuideMode
from agentworks.guide.contributions import FOCUSED_SOURCE_REVIEW_PATHS, guide_contributions, source_review_actions
from agentworks.guide.service import render_guide


def _broken() -> object:
    raise ConfigError("clean-home config is absent")


def _missing() -> object:
    raise ConfigFileNotFoundError("clean-home config is absent")


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
    for path in FOCUSED_SOURCE_REVIEW_PATHS:
        assert path in focused.manual_steps
    repository_root = Path(__file__).resolve().parents[3]
    missing_paths = tuple(path for path in FOCUSED_SOURCE_REVIEW_PATHS if not (repository_root / path).exists())
    assert not missing_paths, f"focused source-review scope paths missing at tested HEAD: {', '.join(missing_paths)}"


def test_source_review_topic_owns_both_actions() -> None:
    topic = next(topic for topic in guide_contributions() if topic.topic == "concept-source-review")
    block = next(block for block in topic.blocks if isinstance(block, ActionList))

    assert [str(action.id) for action in block.actions] == [
        "inspect-focused-source",
        "inspect-full-source",
    ]

    index = render_guide((), GuideMode.AGENT, load_config_fn=_broken)
    human_index = render_guide((), GuideMode.HUMAN, load_config_fn=_broken)
    selected = render_guide(("concept-source-review",), GuideMode.AGENT, load_config_fn=_broken)

    assert human_index.markdown != index.markdown
    assert "inspect-focused-source" not in index.markdown
    assert "inspect-full-source" not in index.markdown
    assert selected.markdown.index("inspect-focused-source") < selected.markdown.index("inspect-full-source")


def test_missing_config_is_a_success_for_index_and_selected_topic_without_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("no-topic guide attempted network access")

    monkeypatch.setattr("agentworks.config.CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(urllib.request, "urlopen", fail_network)

    result = CliRunner().invoke(app, ["guide", "--agent"])

    assert result.exit_code == 0, result.output
    assert result.stdout
    assert result.stderr == ""
    assert not (tmp_path / "config.toml").exists()

    selected_topic = CliRunner().invoke(app, ["guide", "--agent", "concept-onboarding"])

    assert selected_topic.exit_code == 0
    assert selected_topic.stdout
    assert selected_topic.stderr == ""
    assert not (tmp_path / "config.toml").exists()


def test_injected_missing_config_has_the_same_exit_behavior(monkeypatch) -> None:
    def fail_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("no-topic guide attempted network access")

    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(urllib.request, "urlopen", fail_network)

    index = render_guide((), GuideMode.AGENT, load_config_fn=_missing)
    selected = render_guide(("concept-onboarding",), GuideMode.AGENT, load_config_fn=_missing)

    assert index.exit_code == 0
    assert selected.exit_code == 0


def test_other_config_errors_do_not_fail_valid_guide_requests() -> None:
    assert render_guide((), GuideMode.AGENT, load_config_fn=_broken).exit_code == 0
    assert render_guide(("concept-onboarding",), GuideMode.AGENT, load_config_fn=_broken).exit_code == 0


def test_malformed_config_degrades_a_selected_topic(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[broken")
    monkeypatch.setattr("agentworks.config.CONFIG_PATH", config_path)

    result = CliRunner().invoke(app, ["guide", "--agent", "concept-onboarding"])

    assert result.exit_code == 0
    assert result.stdout
    assert result.stderr == ""
