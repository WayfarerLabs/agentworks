from __future__ import annotations

from pathlib import Path

import pytest

import agentworks.guide.render as render_module
from agentworks.guide.agent_mode import GuideMode
from agentworks.guide.catalog import discover_concept_shells
from agentworks.guide.contract import ConceptShell, GuideContentError, GuideSource
from agentworks.guide.render import render_shell, rewrite_relative_destinations


def _topic(markdown: str, *, package_path: str = "unit/guide-content/demo.md") -> ConceptShell:
    return ConceptShell(
        "concept-demo",
        "Demo",
        "Fixture.",
        GuideSource(package_path, f"cli/agentworks/{package_path}", markdown),
    )


def test_agent_fence_is_local_and_filters_before_include_work(tmp_path: Path) -> None:
    topic = _topic(
        "# Demo\n\nShared.\n\n<!-- agw:agent-only -->\n"
        '<!-- agw:include path="../outside.md" heading="Missing" -->\n'
        "Agent context.\n<!-- /agw:agent-only -->\n"
    )

    human = render_shell(topic, GuideMode.HUMAN, package_root=tmp_path)

    assert "Shared." in human
    assert "Agent context." not in human
    with pytest.raises(GuideContentError):
        render_shell(topic, GuideMode.AGENT, package_root=tmp_path)


def test_container_prefixed_control_lines_are_inert_but_column_zero_controls_execute(tmp_path: Path) -> None:
    included = tmp_path / "source.md"
    included.write_text("## Selected\n\nIncluded body.\n", encoding="utf-8")
    topic = _topic(
        "# Demo\n\n"
        "> <!-- agw:agent-only -->\n"
        '> <!-- agw:include path="missing.md" heading="Missing" -->\n'
        "> Quoted.\n"
        "> <!-- /agw:agent-only -->\n\n"
        "- Outer\n"
        "  - <!-- agw:agent-only -->\n"
        '    <!-- agw:include path="missing.md" heading="Missing" -->\n'
        "    Listed.\n"
        "    <!-- /agw:agent-only -->\n\n"
        "<!-- agw:agent-only -->\n"
        "Hidden.\n"
        "<!-- /agw:agent-only -->\n"
        '<!-- agw:include path="source.md" heading="Selected" -->\n'
    )

    rendered = render_shell(topic, GuideMode.HUMAN, package_root=tmp_path)

    assert "Quoted." in rendered
    assert "Listed." in rendered
    assert "Hidden." not in rendered
    assert "Included body." in rendered
    assert rendered.count('<!-- agw:include path="missing.md" heading="Missing" -->') == 2


def test_include_extracts_one_section_shifts_headings_and_stays_inert(tmp_path: Path) -> None:
    included = tmp_path / "docs" / "source.md"
    included.parent.mkdir()
    included.write_text(
        '# Root\n\n## Selected\n\n### Child\n\n<!-- agw:include path="bad.md" heading="Bad" -->\n\n## Next\n',
        encoding="utf-8",
    )
    topic = _topic('# Demo\n\n<!-- agw:include path="docs/source.md" heading="Selected" heading-offset="1" -->\n')

    rendered = render_shell(topic, GuideMode.HUMAN, package_root=tmp_path)

    assert "### Selected" in rendered
    assert "#### Child" in rendered
    assert "## Next" not in rendered
    assert '<!-- agw:include path="bad.md" heading="Bad" -->' in rendered


def test_include_heading_link_literal_is_parsed_before_shell_links_are_rewritten(tmp_path: Path) -> None:
    included = tmp_path / "docs" / "source.md"
    included.parent.mkdir()
    included.write_text("## [Selected](page.md)\n\nIncluded body.\n", encoding="utf-8")
    topic = _topic(
        "# Demo\n\n[Manual][manual]\n\n"
        '<!-- agw:include path="docs/source.md" heading="[Selected](page.md)" -->\n\n'
        "![Diagram][diagram]\n\n"
        "[manual]: docs/manual.md\n"
        "[diagram]: docs/diagram.png\n"
    )

    rendered = render_shell(topic, GuideMode.HUMAN, package_root=tmp_path)

    assert "Included body." in rendered
    assert "https://github.com/WayfarerLabs/agentworks/blob/main/cli/agentworks/docs/page.md" in rendered
    assert (
        "https://github.com/WayfarerLabs/agentworks/blob/main/cli/agentworks/unit/guide-content/docs/manual.md"
        in rendered
    )
    assert (
        "https://raw.githubusercontent.com/WayfarerLabs/agentworks/main/cli/agentworks/unit/guide-content/docs/diagram.png"
        in rendered
    )


def test_heading_offsets_shift_quoted_and_listed_atx_headings(tmp_path: Path) -> None:
    included = tmp_path / "source.md"
    included.write_text(
        "## Selected\n\n> ### Quoted\n\n- #### Listed\n\n## Next\n",
        encoding="utf-8",
    )
    topic = _topic('# Demo\n<!-- agw:include path="source.md" heading="Selected" heading-offset="1" -->\n')

    rendered = render_shell(topic, GuideMode.HUMAN, package_root=tmp_path)

    assert "> #### Quoted\n" in rendered
    assert "- ##### Listed\n" in rendered


@pytest.mark.parametrize("container_heading", ["> ###### Quoted\n", "- ###### Listed\n"])
def test_container_heading_offsets_remain_bounded(tmp_path: Path, container_heading: str) -> None:
    included = tmp_path / "source.md"
    included.write_text(f"## Selected\n\n{container_heading}", encoding="utf-8")
    topic = _topic('# Demo\n<!-- agw:include path="source.md" heading="Selected" heading-offset="1" -->\n')

    with pytest.raises(GuideContentError):
        render_shell(topic, GuideMode.HUMAN, package_root=tmp_path)


def test_included_text_cannot_capture_later_include_nodes(tmp_path: Path) -> None:
    first = tmp_path / "first.md"
    first.write_text(
        '## First\n\n<!-- agw:expanded-section:1 -->\n\n<!-- agw:include path="missing.md" heading="Missing" -->\n',
        encoding="utf-8",
    )
    second = tmp_path / "second.md"
    second.write_text("## Second\n\nSecond body.\n", encoding="utf-8")
    topic = _topic(
        '# Demo\n\n<!-- agw:include path="first.md" heading="First" -->\n\n'
        '<!-- agw:include path="second.md" heading="Second" -->\n'
    )

    rendered = render_shell(topic, GuideMode.HUMAN, package_root=tmp_path)

    assert "<!-- agw:expanded-section:1 -->" in rendered
    assert '<!-- agw:include path="missing.md" heading="Missing" -->' in rendered
    assert rendered.count("## Second") == 1


@pytest.mark.parametrize(
    "included",
    [
        "## Selected\n\n## Selected\n",
        "## Selected\n\nChild\n-----\n",
        "## Selected\n\n###### Too deep\n",
    ],
)
def test_invalid_selected_sections_fail_structurally(tmp_path: Path, included: str) -> None:
    path = tmp_path / "source.md"
    path.write_text(included, encoding="utf-8")
    topic = _topic('# Demo\n<!-- agw:include path="source.md" heading="Selected" heading-offset="1" -->\n')

    with pytest.raises(GuideContentError):
        render_shell(topic, GuideMode.HUMAN, package_root=tmp_path)


@pytest.mark.parametrize(
    "container",
    [
        "> Heading\n> ---\n",
        "- Heading\n  ---\n",
    ],
)
def test_included_setext_headings_in_supported_containers_fail(tmp_path: Path, container: str) -> None:
    path = tmp_path / "source.md"
    path.write_text(f"## Selected\n\n{container}\n## Next\n", encoding="utf-8")
    topic = _topic('# Demo\n<!-- agw:include path="source.md" heading="Selected" -->\n')

    with pytest.raises(GuideContentError):
        render_shell(topic, GuideMode.HUMAN, package_root=tmp_path)


def test_heading_match_preserves_a_nonclosing_trailing_hash(tmp_path: Path) -> None:
    path = tmp_path / "source.md"
    path.write_text("## C#\n\nSelected.\n\n## Next\n", encoding="utf-8")
    topic = _topic('# Demo\n<!-- agw:include path="source.md" heading="C#" -->\n')

    rendered = render_shell(topic, GuideMode.HUMAN, package_root=tmp_path)

    assert "## C#" in rendered
    assert "## Next" not in rendered


def test_inline_and_reference_destinations_use_source_aware_canonical_urls() -> None:
    source = GuideSource(
        "area/guide-content/demo.md",
        "cli/agentworks/area/guide-content/demo.md",
        "",
    )
    markdown = (
        "[Inline](../../command-reference.md#named-consoles)\n\n"
        "![Inline image](<images/view one.png>)\n\n"
        "[Reference][manual] and ![Reference image][diagram].\n\n"
        "[manual]: ../../README.md\n"
        "[diagram]: images/view.png\n"
    )

    rendered = rewrite_relative_destinations(markdown, source)

    assert (
        "https://github.com/WayfarerLabs/agentworks/blob/main/cli/agentworks/command-reference.md#named-consoles"
        in rendered
    )
    assert (
        "https://raw.githubusercontent.com/WayfarerLabs/agentworks/main/cli/agentworks/area/guide-content/images/view%20one.png"
        in rendered
    )
    assert "https://github.com/WayfarerLabs/agentworks/blob/main/cli/agentworks/README.md" in rendered
    assert (
        "https://raw.githubusercontent.com/WayfarerLabs/agentworks/main/cli/agentworks/area/guide-content/images/view.png"
        in rendered
    )


def test_link_scanner_distinguishes_prose_escapes_and_balanced_destinations() -> None:
    source = GuideSource("source.md", "README.md", "")
    markdown = (
        "Plain [brackets] and \\[escaped](docs/ignored.md).\n\n"
        "[Balanced](docs/a(b).md#part) and [Escaped](docs/a\\(b\\).md).\n\n"
        "[Reference \\]][label\\]].\n\n[label\\]]: docs/reference.md#part\n"
    )

    rendered = rewrite_relative_destinations(markdown, source)

    assert "Plain [brackets]" in rendered
    assert "\\[escaped](docs/ignored.md)" in rendered
    assert rendered.count("docs/a%28b%29.md#part") == 1
    assert rendered.count("docs/a%28b%29.md)") == 1
    assert "docs/reference.md#part" in rendered


def test_empty_inline_destinations_remain_current_document_links() -> None:
    source = GuideSource("source.md", "README.md", "")
    markdown = '[Empty]() and [Titled]( "title") and [Angled](<> "title").\n'

    assert rewrite_relative_destinations(markdown, source) == markdown


def test_missing_explicit_reference_fails_but_plain_brackets_remain_prose() -> None:
    source = GuideSource("source.md", "README.md", "")

    assert rewrite_relative_destinations("Plain [brackets].\n", source) == "Plain [brackets].\n"
    with pytest.raises(GuideContentError):
        rewrite_relative_destinations("[Explicit][missing].\n", source)


def test_reference_definitions_are_section_local_and_kind_specific() -> None:
    source = GuideSource("source.md", "README.md", "")

    with pytest.raises(GuideContentError):
        rewrite_relative_destinations("[Missing][ref]\n", source)
    with pytest.raises(GuideContentError):
        rewrite_relative_destinations("[Link][same] ![Image][same]\n\n[same]: docs/file.md\n", source)


def test_absolute_https_fragments_and_code_fences_pass_unchanged() -> None:
    source = GuideSource("source.md", "README.md", "")
    markdown = (
        "[Remote](https://example.com/a?q=1#part) [Local](#part)\n\n```markdown\n![Literal](docs/image.png)\n```\n"
    )

    assert rewrite_relative_destinations(markdown, source) == markdown


def test_core_model_uses_root_mapping_for_real_images_and_fragment() -> None:
    topic = discover_concept_shells().lookup("concept-core-model")
    assert topic is not None

    rendered = render_shell(topic, GuideMode.HUMAN)

    assert rendered.count("https://raw.githubusercontent.com/WayfarerLabs/agentworks/main/docs/images/") == 2
    assert "https://github.com/WayfarerLabs/agentworks/blob/main/cli/command-reference.md#named-consoles" in rendered


def test_root_readme_fallback_requires_the_fixed_editable_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    module = repository / "cli" / "agentworks" / "guide" / "render.py"
    module.parent.mkdir(parents=True)
    (repository / ".git").mkdir()
    (repository / "cli" / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (repository / "README.md").write_text("## Selected\n\nEditable source.\n", encoding="utf-8")
    monkeypatch.setattr(render_module, "__file__", str(module))
    topic = _topic('# Demo\n<!-- agw:include path="_guide_sources/README.md" heading="Selected" -->\n')

    assert "Editable source." in render_shell(topic, GuideMode.HUMAN, package_root=tmp_path / "empty")
