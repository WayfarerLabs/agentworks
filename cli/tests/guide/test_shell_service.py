from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Literal

import pytest
from typer.testing import CliRunner

from agentworks.cli._app import app
from agentworks.completions.spec import build_spec
from agentworks.guide.agent_mode import GuideMode, select_guide_mode
from agentworks.guide.catalog import CORE_INDEX_PATH, discover_concept_shells
from agentworks.guide.contract import GuideContentError, UnknownGuideTopicError
from agentworks.guide.service import list_guide_topics, render_guide
from agentworks.release_notes import RELEASE_TOPIC, ReleaseHistory, ReleaseSection, topic_version
from tests.guide.test_shell_commands import _validate_command_prefix_and_options


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

    response = render_guide(None, mode, package_root=tmp_path)
    lines = response.markdown.splitlines()
    row_slugs = [line.split("`", 2)[1] for line in lines if line.startswith("- `concept-")]

    assert row_slugs == ["concept-zulu", "concept-alpha", "concept-beta"]
    assert "### Imported fixture" in response.markdown
    assert "Included marker." in response.markdown
    assert ("Agent marker." in response.markdown) is (mode is GuideMode.AGENT)


@pytest.mark.parametrize("omitted", [0, 1, 2])
def test_index_footer_reports_the_structural_omitted_count(tmp_path: Path, omitted: int) -> None:
    _shell(tmp_path, "indexed", index_order=1)
    for index in range(omitted):
        _shell(tmp_path, f"omitted-{index}")

    footer = render_guide(None, GuideMode.HUMAN, package_root=tmp_path).markdown.splitlines()[-1]
    count = re.match(r"^(\d+)\b", footer)

    assert count is not None
    assert int(count.group(1)) == omitted


def test_index_discloses_an_executable_release_address_form() -> None:
    """The no-topic index must disclose a real, resolvable exact-release address, not just prose.

    Structural per no-prose-policing-tests: this does not pin the surrounding sentence, only that a
    `concept-release-notes/vMAJOR-MINOR-PATCH`-shaped `agw guide show` command appears, that its
    command prefix is genuine (reusing the authored-commands test's own CLI-spec validation), and
    that its topic is MAJOR-MINOR-PATCH shaped: concrete digits substituted for the placeholder
    components parse through the guide's own release-topic parser.
    """
    response = render_guide(None, GuideMode.HUMAN)

    match = re.search(r"`(agw guide show concept-release-notes/v[^`]+)`", response.markdown)
    assert match is not None, "no-topic index must disclose an exact-release address form"
    command = match.group(1)

    problem = _validate_command_prefix_and_options(command, build_spec(app))
    assert problem is None, f"{command!r}: {problem}"

    topic = command.removeprefix("agw guide show ")
    prefix = f"{RELEASE_TOPIC}/v"
    assert topic.startswith(prefix), f"{topic!r} is not a {RELEASE_TOPIC} address"
    components = topic.removeprefix(prefix).split("-")
    assert len(components) == 3, f"{topic!r} is not MAJOR-MINOR-PATCH shaped"

    concrete = prefix + "-".join(str(index + 1) for index in range(len(components)))
    assert topic_version(concrete) == "1.2.3"


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


def test_packaged_foundational_topics_are_adjacent_and_render_through_the_shell_service() -> None:
    slugs = (
        "concept-core-model",
        "concept-prerequisites",
        "concept-virtual-machines",
        "concept-tailscale",
    )
    catalog = discover_concept_shells()
    indexed = tuple(topic.slug for topic in catalog.indexed_topics())
    start = indexed.index(slugs[0])

    assert indexed[start : start + len(slugs)] == slugs
    assert all(render_guide(slug, GuideMode.HUMAN).markdown for slug in slugs)


def test_cli_exposes_only_default_list_and_single_topic_show() -> None:
    runner = CliRunner()
    default = runner.invoke(app, ["guide"])
    listed = runner.invoke(app, ["guide", "list"])
    topics = listed.stdout.splitlines()
    topic = topics[0]
    shown = runner.invoke(app, ["guide", "show", topic])
    direct = runner.invoke(app, ["guide", topic])
    multiple = runner.invoke(app, ["guide", "show", topic, topic])
    old_option = runner.invoke(app, ["guide", "--names-only"])

    assert default.exit_code == 0 and default.stdout
    assert listed.exit_code == 0
    assert topics
    assert shown.exit_code == 0 and shown.stdout
    assert direct.exit_code != 0
    assert multiple.exit_code != 0
    assert old_option.exit_code != 0


def test_group_mode_applies_to_default_and_show_while_list_ignores_it() -> None:
    runner = CliRunner()
    listed = runner.invoke(app, ["guide", "list"])
    topic = "concept-onboarding"
    assert topic in listed.stdout.splitlines()
    selected = [runner.invoke(app, ["guide", mode]) for mode in ("--agent", "--human")]
    shown = [runner.invoke(app, ["guide", mode, "show", topic]) for mode in ("--agent", "--human")]
    mode_lists = [runner.invoke(app, ["guide", mode, "list"]) for mode in ("--agent", "--human")]
    local_modes = [
        runner.invoke(app, ["guide", command, *([topic] if command == "show" else []), mode])
        for mode in ("--agent", "--human")
        for command in ("list", "show")
    ]

    assert all(result.exit_code == 0 and result.stdout for result in [*selected, *shown])
    assert selected[0].stdout != selected[1].stdout
    assert shown[0].stdout != shown[1].stdout
    assert all(result.exit_code == 0 and result.stdout == listed.stdout for result in mode_lists)
    assert all(result.exit_code != 0 and result.stderr for result in local_modes)


def test_selected_topic_resolves_after_complete_catalog_validation(tmp_path: Path) -> None:
    _shell(tmp_path, "known")

    with pytest.raises(UnknownGuideTopicError):
        render_guide("concept-missing", GuideMode.HUMAN, package_root=tmp_path)


@pytest.mark.parametrize("path", ["index", "list", "show", "completion"])
def test_unrelated_malformed_shell_atomically_blocks_every_catalog_path(tmp_path: Path, path: str) -> None:
    _shell(tmp_path, "known")
    malformed = tmp_path / "other" / "guide-content" / "malformed.md"
    malformed.parent.mkdir(parents=True)
    malformed.write_text("---\ndescription: Broken.\n---\n", encoding="utf-8")

    with pytest.raises(GuideContentError):
        if path == "index":
            render_guide(None, GuideMode.HUMAN, package_root=tmp_path)
        elif path == "show":
            render_guide("concept-known", GuideMode.HUMAN, package_root=tmp_path)
        else:
            # Shell completion consumes the same list service as the list command.
            list_guide_topics(package_root=tmp_path)


def test_static_index_list_and_selected_render_do_not_load_operator_state_modules() -> None:
    probe = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import sys; "
                "from agentworks.guide.agent_mode import GuideMode; "
                "from agentworks.guide.catalog import discover_concept_shells; "
                "from agentworks.guide.service import list_guide_topics, render_guide; "
                "forbidden = ('agentworks.config', 'agentworks.db', 'agentworks.declared_resource', "
                "'agentworks.resource_loading', 'agentworks.resource_names', 'agentworks.resources', "
                "'agentworks.secrets'); "
                "assert not any(name == root or name.startswith(root + '.') "
                "for name in sys.modules for root in forbidden); "
                "render_guide(None, GuideMode.HUMAN); "
                "list_guide_topics(); "
                "render_guide(discover_concept_shells().topics[0].slug, GuideMode.HUMAN); "
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
    body = "# Heading\n\n[Run](https://example.invalid)"
    history = ReleaseHistory((ReleaseSection("1.2.3", body),))
    monkeypatch.setattr("agentworks.guide.service.read_release_history", lambda: history)
    monkeypatch.setattr("agentworks.guide.render.read_release_history", lambda: history)

    response = render_guide("concept-release-notes/v1-2-3", GuideMode.AGENT)

    # Verbatim inside a fence: the fence, not escaping, is what keeps the heading and link inert.
    assert f"```text\n{body}\n```" in response.markdown


def test_exact_release_topic_widens_its_fence_around_an_embedded_fence_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "before\n```\nlooks like a closing fence\n```\nafter"
    history = ReleaseHistory((ReleaseSection("1.2.3", body),))
    monkeypatch.setattr("agentworks.guide.service.read_release_history", lambda: history)
    monkeypatch.setattr("agentworks.guide.render.read_release_history", lambda: history)

    response = render_guide("concept-release-notes/v1-2-3", GuideMode.AGENT)

    opening = re.search(r"^(`{4,})text$", response.markdown, re.MULTILINE)
    assert opening is not None, "a changelog line with a fence marker must widen the wrapping fence"
    fence = opening.group(1)
    closing = response.markdown.index(f"\n{fence}\n", opening.end())
    # The embedded ``` lines stay inside our wider fence rather than closing it early.
    assert response.markdown[opening.end() + 1 : closing] == body


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
