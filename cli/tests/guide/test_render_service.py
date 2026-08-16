from __future__ import annotations

from typing import Literal

import pytest

from agentworks.guide import GuideMode, UnknownGuideTopicError
from agentworks.guide.agent_mode import select_guide_mode
from agentworks.guide.contributions import guide_contributions
from agentworks.guide.render import render_topic, sanitize_terminal_output
from agentworks.guide.service import build_authored_catalog, render_guide


@pytest.mark.parametrize(
    ("explicit", "environ", "tty", "expected"),
    [
        ("human", {"CLAUDECODE": "1"}, False, GuideMode.HUMAN),
        ("agent", {}, True, GuideMode.AGENT),
        (None, {"CLAUDECODE": "1"}, True, GuideMode.AGENT),
        (None, {"CLAUDECODE": "0"}, True, GuideMode.HUMAN),
        (None, {}, False, GuideMode.AGENT),
    ],
)
def test_mode_precedence(
    explicit: Literal["agent", "human"] | None,
    environ: dict[str, str],
    tty: bool,
    expected: GuideMode,
) -> None:
    assert select_guide_mode(explicit, environ, tty) is expected


def test_human_and_agent_rendering_have_semantic_parity() -> None:
    topic = next(item for item in guide_contributions() if item.topic == "concept-management")

    human = render_topic(topic, GuideMode.HUMAN)
    agent = render_topic(topic, GuideMode.AGENT)

    assert {block.key for block in human.blocks} == {block.key for block in agent.blocks}
    assert {block.key: block.source_payload for block in human.blocks} == {
        block.key: block.source_payload for block in agent.blocks
    }


def test_static_topics_do_not_load_live_context() -> None:
    def forbidden(*_args: object) -> object:
        raise AssertionError("static guide topics must not load live context")

    response = render_guide(
        ("concept-management",),
        GuideMode.AGENT,
        load_config_fn=forbidden,  # type: ignore[arg-type]
        load_registry_fn=forbidden,  # type: ignore[arg-type]
    )

    assert response.exit_code == 0


def test_names_only_is_the_authored_catalog_without_live_loading() -> None:
    def forbidden(*_args: object) -> object:
        raise AssertionError("names-only must not load live context")

    response = render_guide(
        (),
        GuideMode.AGENT,
        names_only=True,
        load_config_fn=forbidden,  # type: ignore[arg-type]
        load_registry_fn=forbidden,  # type: ignore[arg-type]
    )

    names = tuple(response.markdown.splitlines())
    assert response.exit_code == 0
    assert names == build_authored_catalog().names()
    assert all(name.startswith("concept-") or name.startswith("plugin/") for name in names)


@pytest.mark.parametrize("requested", ["vm-template", "vm-template/demo", "vm-platform/lima"])
def test_removed_resource_and_schema_topics_are_unknown(requested: str) -> None:
    with pytest.raises(UnknownGuideTopicError):
        render_guide((requested,), GuideMode.AGENT)


def test_atomic_unknown_request_raises_before_a_response_exists() -> None:
    with pytest.raises(UnknownGuideTopicError):
        render_guide(("concept-management", "concept-missing"), GuideMode.AGENT)


_DISALLOWED_TERMINAL_CODEPOINTS = tuple((*range(0x00, 0x09), *range(0x0B, 0x20), 0x7F, *range(0x80, 0xA0)))


@pytest.mark.parametrize("codepoint", _DISALLOWED_TERMINAL_CODEPOINTS)
def test_terminal_sanitizer_strips_every_disallowed_control(codepoint: int) -> None:
    assert sanitize_terminal_output(f"before{chr(codepoint)}after") == "beforeafter"


def test_terminal_sanitizer_preserves_line_feed_and_tab() -> None:
    assert sanitize_terminal_output("before\n\tafter") == "before\n\tafter"
