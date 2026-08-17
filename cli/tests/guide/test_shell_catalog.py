from __future__ import annotations

from pathlib import Path

import pytest

from agentworks.guide.catalog import CORE_INDEX_PATH, discover_concept_shells
from agentworks.guide.contract import GuideContentError
from agentworks.guide.directives import bounded_include_path


def _index(root: Path, *, frontmatter: str = "description: Fixture index.", body: str = "# Index\n") -> Path:
    path = root / CORE_INDEX_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    return path


def _shell(
    root: Path,
    relative: str,
    *,
    description: str = "Fixture topic.",
    index_order: str | None = None,
    body: str = "# Fixture\n",
) -> Path:
    index_path = root / CORE_INDEX_PATH
    if not index_path.exists():
        _index(root)
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    order = "" if index_order is None else f"index-order: {index_order}\n"
    path.write_text(f"---\ndescription: {description}\n{order}---\n{body}", encoding="utf-8")
    return path


def test_direct_markdown_child_defines_identity_summary_and_title(tmp_path: Path) -> None:
    _shell(tmp_path, "area/guide-content/demo-topic.md", description="A fixture summary.", body="# Demo title\n")

    catalog = discover_concept_shells(tmp_path)

    assert catalog.names() == ("concept-demo-topic",)
    topic = catalog.topics[0]
    assert topic.description == "A fixture summary."
    assert topic.title == "Demo title"
    assert topic.index_order is None
    assert topic.source.package_path == "area/guide-content/demo-topic.md"
    assert catalog.index.source.package_path == CORE_INDEX_PATH


def test_only_direct_markdown_children_of_guide_content_are_discovered(tmp_path: Path) -> None:
    _shell(tmp_path, "one/guide-content/alpha.md")
    _shell(tmp_path, "one/guide-content/nested/ignored.md")
    _shell(tmp_path, "ordinary/beta.md")

    assert discover_concept_shells(tmp_path).names() == ("concept-alpha",)


def test_discovery_order_is_package_path_order(tmp_path: Path) -> None:
    _shell(tmp_path, "z/guide-content/first.md")
    _shell(tmp_path, "a/guide-content/second.md")

    assert discover_concept_shells(tmp_path).names() == ("concept-second", "concept-first")


@pytest.mark.parametrize(("value", "expected"), [("0", 0), ("0000", 0), ("9999", 9999)])
def test_optional_index_order_accepts_the_closed_bounded_decimal_form(
    tmp_path: Path, value: str, expected: int
) -> None:
    _shell(tmp_path, "guide-content/ordered.md", index_order=value)

    assert discover_concept_shells(tmp_path).topics[0].index_order == expected


@pytest.mark.parametrize(
    "frontmatter",
    [
        "index-order: 1\ndescription: Fixture.",
        "description: Fixture.\ndescription: Duplicate.",
        "description: Fixture.\nindex-order: 1\nindex-order: 2",
        "description: Fixture.\nunknown: value",
        "description: Fixture.\nindex-order: -1",
        "description: Fixture.\nindex-order: 10000",
        "description: Fixture.\nindex-order: ١",
    ],
)
def test_frontmatter_rejects_unknown_duplicate_misordered_or_unbounded_fields(tmp_path: Path, frontmatter: str) -> None:
    _index(tmp_path)
    path = tmp_path / "guide-content" / "bad.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\n{frontmatter}\n---\n# Bad\n", encoding="utf-8")

    with pytest.raises(GuideContentError):
        discover_concept_shells(tmp_path)


def test_required_reserved_index_obeys_shell_structure_and_has_no_order(tmp_path: Path) -> None:
    _shell(tmp_path, "guide-content/valid.md")
    _index(tmp_path, frontmatter="description: Fixture index.\nindex-order: 1")

    with pytest.raises(GuideContentError):
        discover_concept_shells(tmp_path)


def test_missing_or_misplaced_reserved_index_fails_structurally(tmp_path: Path) -> None:
    path = tmp_path / "guide-content" / "ordinary.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ndescription: Fixture.\n---\n# Ordinary\n", encoding="utf-8")

    with pytest.raises(GuideContentError):
        discover_concept_shells(tmp_path)

    _index(tmp_path)
    misplaced = tmp_path / "area" / "guide-content" / "_index.md"
    misplaced.parent.mkdir(parents=True)
    misplaced.write_text("---\ndescription: Wrong index.\n---\n# Wrong\n", encoding="utf-8")

    with pytest.raises(GuideContentError):
        discover_concept_shells(tmp_path)


def test_other_underscore_markdown_shell_names_are_reserved(tmp_path: Path) -> None:
    _shell(tmp_path, "guide-content/_private.md")

    with pytest.raises(GuideContentError):
        discover_concept_shells(tmp_path)


def test_include_path_preserves_only_bounded_raw_segments() -> None:
    assert bounded_include_path("docs/source.md") == ("docs", "source.md")
    for malformed in ("docs/./source.md", "docs//source.md"):
        with pytest.raises(GuideContentError):
            bounded_include_path(malformed)


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
            '---\ndescription: Bad.\n---\n# One\n<!-- agw:include path="./source.md" heading="Bad" -->\n',
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
        "# Demo\n\n- > List quote heading\n  > ===\n",
        "# Demo\n\n> - Quote list heading\n>   ===\n",
        "# Demo\n\n- Outer\n  - Nested list heading\n    ===\n",
        "# Demo\n\n- Outer\n  - Inner\n    > Three-deep heading\n    > ===\n",
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


@pytest.mark.parametrize(
    "body",
    [
        "# Demo\n\n\t### Tab-indented heading\n",
        "# Demo\n\n- Item\n  \t### Tab-indented continuation\n",
    ],
)
def test_leading_tab_indentation_fails_closed(tmp_path: Path, body: str) -> None:
    _shell(tmp_path, "guide-content/tabs.md", body=body)

    with pytest.raises(GuideContentError):
        discover_concept_shells(tmp_path)


def test_repository_catalog_selects_eight_featured_concepts_from_frontmatter() -> None:
    catalog = discover_concept_shells()
    indexed = catalog.indexed_topics()

    assert len(indexed) == 8
    assert all(topic.index_order is not None for topic in indexed)
    names = set(catalog.names())
    assert {"concept-apt", "concept-core-model", "concept-install-commands"} <= names
    assert catalog.index.source.package_path == CORE_INDEX_PATH
