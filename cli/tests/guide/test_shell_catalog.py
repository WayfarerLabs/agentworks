from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.guide.catalog import discover_concept_shells
from agentworks.guide.contract import GuideContentError


def _shell(root: Path, relative: str, *, description: str = "Fixture topic.", body: str = "# Fixture\n") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ndescription: {description}\n---\n{body}", encoding="utf-8")
    return path


def test_direct_markdown_child_defines_identity_summary_and_title(tmp_path: Path) -> None:
    _shell(tmp_path, "area/guide-content/demo-topic.md", description="A fixture summary.", body="# Demo title\n")

    catalog = discover_concept_shells(tmp_path)

    assert catalog.names() == ("concept-demo-topic",)
    topic = catalog.topics[0]
    assert topic.description == "A fixture summary."
    assert topic.title == "Demo title"
    assert topic.source.package_path == "area/guide-content/demo-topic.md"


def test_only_direct_markdown_children_of_guide_content_are_discovered(tmp_path: Path) -> None:
    _shell(tmp_path, "one/guide-content/alpha.md")
    _shell(tmp_path, "one/guide-content/nested/ignored.md")
    _shell(tmp_path, "ordinary/beta.md")

    assert discover_concept_shells(tmp_path).names() == ("concept-alpha",)


def test_discovery_order_is_package_path_order(tmp_path: Path) -> None:
    _shell(tmp_path, "z/guide-content/first.md")
    _shell(tmp_path, "a/guide-content/second.md")

    assert discover_concept_shells(tmp_path).names() == ("concept-second", "concept-first")


@pytest.mark.parametrize(
    ("relative", "text"),
    [
        ("guide-content/bad_name.md", "---\ndescription: Bad.\n---\n# Title\n"),
        ("guide-content/bad.md", "---\ndescription: Bad.\nextra: value\n---\n# Title\n"),
        ("guide-content/bad.md", "---\ndescription: Bad.\n---\nTitle\n=====\n"),
        ("guide-content/bad.md", "---\ndescription: Bad.\n---\n# One\n# Two\n"),
        ("guide-content/bad.md", "---\ndescription: Bad.\n---\n# One\n<!-- agw:agent-only -->\n"),
        ("guide-content/bad.md", "---\ndescription: Bad.\n---\n# One\n<!-- agw:unknown -->\n"),
        (
            "guide-content/bad.md",
            '---\ndescription: Bad.\n---\n# One\n<!-- agw:include path="../bad.md" heading="Bad" -->\n',
        ),
        (
            "guide-content/bad.md",
            "---\ndescription: Bad.\n---\n# One\n"
            '<!-- agw:include path="source.md" heading="Bad" heading-offset="١" -->\n',
        ),
    ],
)
def test_malformed_shells_fail_the_catalog(tmp_path: Path, relative: str, text: str) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")

    with pytest.raises(GuideContentError):
        discover_concept_shells(tmp_path)


def test_duplicate_global_slug_fails_instead_of_shadowing(tmp_path: Path) -> None:
    _shell(tmp_path, "a/guide-content/shared.md")
    _shell(tmp_path, "b/guide-content/shared.md")

    with pytest.raises(GuideContentError):
        discover_concept_shells(tmp_path)


def test_directive_looking_text_inside_code_fences_is_inert(tmp_path: Path) -> None:
    _shell(tmp_path, "guide-content/code.md", body="# Code\n\n```markdown\n<!-- agw:unknown -->\n```\n")

    assert discover_concept_shells(tmp_path).names() == ("concept-code",)


def test_fence_info_and_suffix_lines_keep_directives_inert(tmp_path: Path) -> None:
    _shell(
        tmp_path,
        "guide-content/code.md",
        body=("# Code\n\n```markdown\n<!-- agw:unknown -->\n``` suffix\n<!-- agw:unknown -->\n````\n"),
    )

    assert discover_concept_shells(tmp_path).names() == ("concept-code",)


def test_container_prefixed_control_lines_are_ordinary_markdown(tmp_path: Path) -> None:
    _shell(
        tmp_path,
        "guide-content/containers.md",
        body=(
            "# Containers\n\n"
            "> <!-- agw:agent-only -->\n"
            '> <!-- agw:include path="../bad.md" heading="Bad" -->\n'
            "> <!-- /agw:agent-only -->\n\n"
            "- Outer\n"
            "  - <!-- agw:agent-only -->\n"
            '    <!-- agw:include path="../bad.md" heading="Bad" -->\n'
            "    <!-- /agw:agent-only -->\n"
        ),
    )

    assert discover_concept_shells(tmp_path).names() == ("concept-containers",)


@pytest.mark.parametrize(
    "body",
    [
        "# Demo\n\n> Quoted heading\n> ---\n",
        "# Demo\n\n- Listed heading\n  ===\n",
    ],
)
def test_setext_headings_in_supported_containers_fail(tmp_path: Path, body: str) -> None:
    _shell(tmp_path, "guide-content/setext.md", body=body)

    with pytest.raises(GuideContentError):
        discover_concept_shells(tmp_path)


def test_setext_looking_content_inside_a_container_fence_is_inert(tmp_path: Path) -> None:
    _shell(
        tmp_path,
        "guide-content/code.md",
        body="# Demo\n\n- ```markdown\n  Heading\n  ---\n  <!-- agw:unknown -->\n  ```\n",
    )

    assert discover_concept_shells(tmp_path).names() == ("concept-code",)


def test_repository_catalog_contains_every_fixed_destination() -> None:
    from agentworks.guide.trail_sign import TRAIL_DESTINATIONS

    names = set(discover_concept_shells().names())
    assert {destination.slug for destination in TRAIL_DESTINATIONS} <= names
    assert {"concept-apt", "concept-core-model", "concept-install-commands"} <= names
