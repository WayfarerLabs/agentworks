from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Literal

import pytest
from typer.testing import CliRunner

from agentworks.cli._app import app
from agentworks.guide.agent_mode import GuideMode, select_guide_mode
from agentworks.guide.catalog import CORE_INDEX_PATH
from agentworks.guide.contract import UnknownGuideTopicError
from agentworks.guide.service import list_guide_topics, render_guide
from agentworks.release_notes import ReleaseHistory, ReleaseSection


def _index(root: Path, body: str = "# Fixture index\n\n## Topics\n") -> None:
    path = root / CORE_INDEX_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ndescription: Fixture index.\n---\n{body}", encoding="utf-8")


def _shell(root: Path, name: str, *, index_order: int | None = None) -> None:
    if not (root / CORE_INDEX_PATH).exists():
        _index(root)
    path = root / "guide-content" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    order = "" if index_order is None else f"index-order: {index_order}\n"
    path.write_text(f"---\ndescription: {name} fixture.\n{order}---\n# {name}\n", encoding="utf-8")


@pytest.mark.parametrize("mode", tuple(GuideMode))
def test_no_topic_renders_index_shell_and_catalog_rows_without_release_history(
    tmp_path: Path, mode: GuideMode, monkeypatch: pytest.MonkeyPatch
) -> None:
    include = tmp_path / "docs" / "source.md"
    include.parent.mkdir(parents=True)
    include.write_text("## Imported fixture\n\nIncluded marker.\n", encoding="utf-8")
    _index(
        tmp_path,
        "# Fixture index\n\nShared marker.\n\n<!-- agw:agent-only -->\nAgent marker.\n<!-- /agw:agent-only -->\n\n"
        '<!-- agw:include path="docs/source.md" heading="Imported fixture" heading-offset="1" -->\n',
    )
    _shell(tmp_path, "alpha", index_order=20)
    _shell(tmp_path, "zulu", index_order=10)
    _shell(tmp_path, "beta", index_order=20)
    _shell(tmp_path, "omitted")

    def forbidden() -> ReleaseHistory:
        raise AssertionError("release history loaded")

    monkeypatch.setattr("agentworks.guide.service.read_release_history", forbidden)

    response = render_guide((), mode, package_root=tmp_path)
    lines = response.markdown.splitlines()
    row_slugs = [line.split("`", 2)[1] for line in lines if line.startswith("- `concept-")]

    assert row_slugs == ["concept-zulu", "concept-alpha", "concept-beta"]
    assert "`agw guide list`" in lines[-1]
    assert "### Imported fixture" in response.markdown
    assert "Included marker." in response.markdown
    assert ("Agent marker." in response.markdown) is (mode is GuideMode.AGENT)


@pytest.mark.parametrize(
    ("omitted", "expected_noun", "expected_verb"),
    [(0, "concepts", "are"), (1, "concept", "is"), (2, "concepts", "are")],
)
def test_index_footer_count_uses_matching_number_grammar(
    tmp_path: Path, omitted: int, expected_noun: str, expected_verb: str
) -> None:
    _shell(tmp_path, "indexed", index_order=1)
    for index in range(omitted):
        _shell(tmp_path, f"omitted-{index}")

    footer = render_guide((), GuideMode.HUMAN, package_root=tmp_path).markdown.splitlines()[-1]
    grammar = re.match(r"^(\d+) other (concepts?) (is|are)\b", footer)

    assert grammar is not None
    assert grammar.groups() == (str(omitted), expected_noun, expected_verb)
    assert "`agw guide list`" in footer


def test_list_uses_static_shells_and_packaged_release_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _shell(tmp_path, "beta")
    _shell(tmp_path, "alpha")
    monkeypatch.setattr(
        "agentworks.guide.service.read_release_history",
        lambda: ReleaseHistory((ReleaseSection("1.2.3", "Fixture evidence."),)),
    )

    response = list_guide_topics(package_root=tmp_path)

    assert response.markdown.splitlines() == [
        "concept-alpha",
        "concept-beta",
        "concept-release-notes/v1-2-3",
    ]


def test_list_is_an_exact_reserved_cli_positional_and_names_only_is_removed() -> None:
    listed = CliRunner().invoke(app, ["guide", "list"])
    help_result = CliRunner().invoke(app, ["guide", "--help"])
    old_option = CliRunner().invoke(app, ["guide", "--names-only"])
    mixed = CliRunner().invoke(app, ["guide", "list", "concept-onboarding"])

    assert listed.exit_code == 0
    assert "concept-onboarding" in listed.stdout.splitlines()
    assert help_result.exit_code == 0
    assert "list" in help_result.stdout
    assert old_option.exit_code != 0
    assert mixed.exit_code != 0
    assert mixed.exception is not None
    assert "agw guide list" in str(mixed.exception)


def test_selected_requests_resolve_atomically(tmp_path: Path) -> None:
    _shell(tmp_path, "known")

    with pytest.raises(UnknownGuideTopicError):
        render_guide(("concept-known", "concept-missing"), GuideMode.HUMAN, package_root=tmp_path)


def test_static_index_list_and_selected_render_do_not_load_operator_state_modules() -> None:
    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import sys; "
                "from agentworks.guide.agent_mode import GuideMode; "
                "from agentworks.guide.service import list_guide_topics, render_guide; "
                "forbidden = ('agentworks.config', 'agentworks.db', 'agentworks.declared_resource', "
                "'agentworks.resource_loading', 'agentworks.resource_names', 'agentworks.resources', "
                "'agentworks.secrets'); "
                "assert not any(name == root or name.startswith(root + '.') "
                "for name in sys.modules for root in forbidden); "
                "render_guide((), GuideMode.HUMAN); "
                "list_guide_topics(); "
                "render_guide(('concept-management',), GuideMode.HUMAN); "
                "assert not any(name == root or name.startswith(root + '.') "
                "for name in sys.modules for root in forbidden)"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert probe.returncode == 0


def test_exact_release_topic_is_direct_inert_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    history = ReleaseHistory((ReleaseSection("1.2.3", "# Heading\n\n[Run](https://example.invalid)"),))
    monkeypatch.setattr("agentworks.guide.service.read_release_history", lambda: history)
    monkeypatch.setattr("agentworks.guide.render.read_release_history", lambda: history)

    response = render_guide(("concept-release-notes/v1-2-3",), GuideMode.AGENT)

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
