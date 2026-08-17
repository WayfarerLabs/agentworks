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


def test_repository_catalog_contains_every_fixed_destination() -> None:
    from agentworks.guide.trail_sign import TRAIL_DESTINATIONS

    names = set(discover_concept_shells().names())
    assert {destination.slug for destination in TRAIL_DESTINATIONS} <= names
    assert {"concept-apt", "concept-core-model", "concept-install-commands"} <= names
