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
