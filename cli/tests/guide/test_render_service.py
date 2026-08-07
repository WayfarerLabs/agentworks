from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from agentworks.errors import ConfigError
from agentworks.guide import GuideMode, UnknownGuideTopicError
from agentworks.guide.agent_mode import select_guide_mode
from agentworks.guide.contributions import guide_contributions
from agentworks.guide.render import render_topic
from agentworks.guide.service import _dynamic_topic, render_guide
from agentworks.guide.view import build_guide_view
from agentworks.plugins.base import Plugin
from agentworks.resources import ResourceReference
from agentworks.resources.graph import Enablement, Readiness
from agentworks.resources.reference import ReferenceEntry

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.resources import Registry


def _broken() -> object:
    raise ConfigError("broken settings", hint="Fix the named setting.")


@pytest.mark.parametrize(
    ("explicit", "environ", "tty", "expected"),
    [
        ("human", {"CLAUDECODE": "1"}, False, GuideMode.HUMAN),
        ("agent", {}, True, GuideMode.AGENT),
        (None, {"CLAUDECODE": "1"}, True, GuideMode.AGENT),
        (None, {"CLAUDECODE": "true", "CODEX_HOME": "/tmp/x"}, True, GuideMode.HUMAN),
        (None, {}, False, GuideMode.AGENT),
    ],
)
def test_mode_precedence(explicit, environ, tty: bool, expected: GuideMode) -> None:
    assert select_guide_mode(explicit, environ, tty) is expected


def test_human_and_agent_rendering_have_semantic_parity() -> None:
    topic = next(topic for topic in guide_contributions() if topic.topic == "concept-management")
    human = render_topic(topic, None, GuideMode.HUMAN, unavailable="unused")
    agent = render_topic(topic, None, GuideMode.AGENT, unavailable="unused")
    assert {(block.key, block.source_payload) for block in human.blocks} == {
        (block.key, block.source_payload) for block in agent.blocks
    }
    assert human.markdown != agent.markdown


def test_broken_config_keeps_authored_content_and_marks_dynamic_facts() -> None:
    response = render_guide(("concept-onboarding", "vm-template/demo"), GuideMode.AGENT, load_config_fn=_broken)
    assert response.exit_code == 1
    assert "Progressive onboarding" in response.markdown
    assert response.markdown.count("Configuration error: broken settings") == 1
    assert response.markdown.count("Live facts unavailable: see the system failure below") == 4


def test_atomic_unknown_request_raises_before_a_response_exists() -> None:
    with pytest.raises(UnknownGuideTopicError):
        render_guide(("concept-management", "unknown-kind/demo"), GuideMode.HUMAN, load_config_fn=_broken)


def test_names_only_degrades_to_authored_and_code_owned_kinds() -> None:
    response = render_guide((), GuideMode.AGENT, names_only=True, load_config_fn=_broken)
    assert response.exit_code == 0
    assert "concept-onboarding\n" in response.markdown
    assert "vm-template\n" in response.markdown
    assert "Guide content unavailable" not in response.markdown


def test_rendering_has_no_power(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("power invoked")

    import agentworks.output as output
    import agentworks.secrets.resolve as secrets
    import agentworks.transports as transports
    from agentworks.db import Database

    monkeypatch.setattr(output, "prompt", denied)
    monkeypatch.setattr(secrets, "resolve_secrets", denied)
    monkeypatch.setattr(transports, "transport", denied)
    monkeypatch.setattr(Database, "insert_vm", denied)
    response = render_guide(("concept-onboarding",), GuideMode.AGENT, load_config_fn=_broken)
    assert response.markdown


class _LiveRegistry:
    is_finalized = True

    class Graph:
        def impl_of(self, *args: object) -> None:
            raise AssertionError("impl_of invoked")

    graph = Graph()

    def finalize(self) -> None:
        raise AssertionError("finalize invoked")

    def iter_kind_items(self, kind: str):
        if kind == "vm-template":
            return iter((("demo", object()),))
        return iter(())


def test_successful_live_rendering_uses_no_denied_power(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("power invoked")

    import agentworks.output as output
    import agentworks.secrets.resolve as secrets
    import agentworks.transports as transports
    from agentworks.db import Database

    monkeypatch.setattr(output, "prompt", denied)
    monkeypatch.setattr(secrets, "resolve_secrets", denied)
    monkeypatch.setattr(transports, "transport", denied)
    monkeypatch.setattr(Database, "_migrate", denied)
    registry = _LiveRegistry()
    config = cast("Config", object())
    typed_registry = cast("Registry", registry)
    database = cast("Database", object())
    response = render_guide(
        ("vm-template",),
        GuideMode.AGENT,
        load_config_fn=lambda: config,
        load_registry_fn=lambda config: typed_registry,
        db=database,
    )
    assert response.exit_code == 0
    assert "vm-template/demo" in response.markdown


def test_live_dynamic_block_payloads_have_semantic_parity() -> None:
    registry = _LiveRegistry()
    topic = _dynamic_topic(registry, "vm-template")  # type: ignore[arg-type]
    view = build_guide_view(topic, registry, object())  # type: ignore[arg-type]
    human = render_topic(topic, view, GuideMode.HUMAN)
    agent = render_topic(topic, view, GuideMode.AGENT)
    assert [(block.key, block.source_payload) for block in human.blocks] == [
        (block.key, block.source_payload) for block in agent.blocks
    ]
    assert human.blocks[0].source_payload == "- `vm-template/demo`"


class _ExactGraph:
    def readiness_of(self, kind: str, name: str) -> Readiness:
        return Readiness.ready()

    def enablement_of(self, kind: str, name: str) -> Enablement:
        return Enablement.enabled

    def edges_of(self, kind: str, name: str) -> tuple[ResourceReference, ...]:
        return (ResourceReference("token", "secret", "authenticates demo", (kind, name)),)

    def dependents_of(self, kind: str, name: str) -> tuple[ReferenceEntry, ...]:
        return (ReferenceEntry(("session-template", "consumer"), "selects demo"),)


class _ExactRegistry:
    is_finalized = True
    graph = _ExactGraph()
    resource = SimpleNamespace(name="demo", description="Demo template.", origin=None)

    def finalize(self) -> None:
        raise AssertionError("finalize invoked")

    def iter_kind_items(self, kind: str):
        if kind == "vm-template":
            return iter((("demo", self.resource),))
        return iter(())

    def lookup(self, kind: str, name: str) -> object:
        assert (kind, name) == ("vm-template", "demo")
        return self.resource


class _ExactDatabase:
    def list_vms(self) -> list[object]:
        return [SimpleNamespace(name="vm-one", template="demo")]


def test_live_exact_topic_semantic_parity_covers_state_relationships_and_instances() -> None:
    registry = _ExactRegistry()
    topic = _dynamic_topic(registry, "vm-template/demo")  # type: ignore[arg-type]
    view = build_guide_view(topic, registry, _ExactDatabase())  # type: ignore[arg-type]
    human = render_topic(topic, view, GuideMode.HUMAN)
    agent = render_topic(topic, view, GuideMode.AGENT)
    human_semantics = [(block.key, block.source_payload) for block in human.blocks]
    assert human_semantics == [(block.key, block.source_payload) for block in agent.blocks]
    payloads = {block.key.block_id: block.source_payload or "" for block in human.blocks}
    assert "ready" in payloads["state"]
    assert "Uses `secret/token`" in payloads["relationships"]
    assert "Used by `session-template/consumer`" in payloads["relationships"]
    assert payloads["instances"] == "- `vm/vm-one`"


@pytest.mark.parametrize("malformed", [False, True])
def test_unusable_state_database_fails_soft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, malformed: bool) -> None:
    import agentworks.db as db_package

    path = tmp_path / "state.db"
    if malformed:
        path.write_bytes(b"not sqlite")
    else:
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_version VALUES (0)")
        connection.commit()
        connection.close()
    monkeypatch.setattr(db_package, "DB_PATH", path)
    registry = _LiveRegistry()
    config = cast("Config", object())
    typed_registry = cast("Registry", registry)
    response = render_guide(
        ("vm-template",),
        GuideMode.HUMAN,
        load_config_fn=lambda: config,
        load_registry_fn=lambda config: typed_registry,
    )
    assert response.exit_code == 1
    assert "Live facts unavailable" in response.markdown
    assert "state database" in response.markdown


def test_broken_finalization_discards_partial_registry() -> None:
    partial = object()
    config = cast("Config", object())

    def fail_after_partial(config: object) -> object:
        assert partial is not None
        raise ConfigError("finalization failed")

    response = render_guide(
        ("vm-template/demo",),
        GuideMode.AGENT,
        load_config_fn=lambda: config,
        load_registry_fn=fail_after_partial,  # type: ignore[arg-type]
    )
    assert response.exit_code == 1
    assert "Current facts for vm-template/demo" in response.markdown
    assert "finalization failed" in response.markdown


def test_plugin_guide_topics_are_normalized_to_an_inert_tuple() -> None:
    topic = guide_contributions()[0]
    plugin = Plugin("guide-test", guide_topics=[topic])  # type: ignore[arg-type]
    assert plugin.guide_topics == (topic,)
