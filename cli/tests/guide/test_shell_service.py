from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from typer.testing import CliRunner

from agentworks.cli._app import app
from agentworks.guide.agent_mode import GuideMode, select_guide_mode
from agentworks.guide.contract import UnknownGuideTopicError
from agentworks.guide.service import render_guide
from agentworks.guide.trail_sign import TRAIL_DESTINATIONS
from agentworks.release_notes import ReleaseHistory, ReleaseSection


def _shell(root: Path, name: str) -> None:
    path = root / "guide-content" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ndescription: Fixture.\n---\n# {name}\n", encoding="utf-8")


@pytest.mark.parametrize("mode", tuple(GuideMode))
def test_no_topic_trail_sign_bypasses_the_catalog(mode: GuideMode, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("catalog loaded")

    monkeypatch.setattr("agentworks.guide.service.discover_concept_shells", forbidden)

    response = render_guide((), mode)

    assert response.exit_code == 0
    for destination in TRAIL_DESTINATIONS:
        assert destination.slug in response.markdown


def test_names_only_uses_static_shells_and_packaged_release_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _shell(tmp_path, "beta")
    _shell(tmp_path, "alpha")
    monkeypatch.setattr(
        "agentworks.guide.service.read_release_history",
        lambda: ReleaseHistory((ReleaseSection("1.2.3", "Fixture evidence."),)),
    )

    response = render_guide((), GuideMode.AGENT, names_only=True, package_root=tmp_path)

    assert response.markdown.splitlines() == [
        "concept-alpha",
        "concept-beta",
        "concept-release-notes/v1-2-3",
    ]


def test_selected_requests_resolve_atomically(tmp_path: Path) -> None:
    _shell(tmp_path, "known")

    with pytest.raises(UnknownGuideTopicError):
        render_guide(("concept-known", "concept-missing"), GuideMode.HUMAN, package_root=tmp_path)


def test_exact_release_topic_is_direct_inert_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    history = ReleaseHistory((ReleaseSection("1.2.3", "# Heading\n\n[Run](https://example.invalid)"),))
    monkeypatch.setattr("agentworks.guide.service.read_release_history", lambda: history)
    monkeypatch.setattr("agentworks.guide.render.read_release_history", lambda: history)

    response = render_guide(("concept-release-notes/v1-2-3",), GuideMode.AGENT)

    assert response.exit_code == 0
    assert "\\# Heading" in response.markdown
    assert "[Run](https://example.invalid)" not in response.markdown


@pytest.mark.parametrize(
    ("explicit", "environment", "tty", "expected"),
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
    environment: dict[str, str],
    tty: bool,
    expected: GuideMode,
) -> None:
    assert select_guide_mode(explicit, environment, tty) is expected


def test_removed_evidence_option_is_rejected_by_the_cli() -> None:
    result = CliRunner().invoke(app, ["guide", "--evidence", "fixture"])

    assert result.exit_code != 0
