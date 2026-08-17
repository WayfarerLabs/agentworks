from __future__ import annotations

from pathlib import Path

import pytest

import agentworks.release_notes as release_notes_module
from agentworks.release_notes import (
    MAX_CHANGELOG_BYTES,
    MAX_RELEASE_SECTION_BYTES,
    ReleaseNotesError,
    parse_release_history,
    topic_version,
    version_topic,
)


def _history(*sections: tuple[str, str]) -> bytes:
    rendered = "\n\n".join(
        f"## [{version}](https://github.com/WayfarerLabs/agentworks/compare/v0.1.0...v{version}) (2026-08-10)\n\n{body}"
        for version, body in sections
    )
    return f"# Changelog\n\n{rendered}\n".encode()


def test_release_topic_mapping_is_strict() -> None:
    assert version_topic("1.2.3") == "concept-release-notes/v1-2-3"
    assert topic_version("concept-release-notes/v1-2-3") == "1.2.3"
    assert topic_version("concept-release-notes/v1-2") is None
    assert topic_version("concept-release-notes/v01-2-3") is None
    assert topic_version("plugin/example/v1-2-3") is None
    with pytest.raises(ReleaseNotesError):
        version_topic("1.2")


def test_release_history_preserves_valid_sections_in_source_order() -> None:
    history = parse_release_history(_history(("2.0.0", "Second."), ("1.0.0", "First.")))

    assert history.versions == ("2.0.0", "1.0.0")
    assert history.section("1.0.0").topic == "concept-release-notes/v1-0-0"
    with pytest.raises(ReleaseNotesError):
        history.section("3.0.0")


@pytest.mark.parametrize(
    "data",
    [
        b"# Changelog\n",
        b"# Changelog\n\n## [1.2] (2026-08-10)\n\nNotes.\n",
        _history(("1.2.3", "One."), ("1.2.3", "Two.")),
        _history(("1.2.3", "unsafe ${instruction}")),
        _history(("1.2.3", "unsafe\x1b[2J")),
        _history(("1.2.3", "unsafe ⟦delimiter⟧")),
        _history(("1.2.3", "")),
    ],
)
def test_malformed_or_unsafe_history_fails_closed(data: bytes) -> None:
    with pytest.raises(ReleaseNotesError):
        parse_release_history(data)


def test_release_history_bounds_fail_closed() -> None:
    with pytest.raises(ReleaseNotesError):
        parse_release_history(b"x" * (MAX_CHANGELOG_BYTES + 1))
    with pytest.raises(ReleaseNotesError):
        parse_release_history(_history(("1.2.3", "x" * (MAX_RELEASE_SECTION_BYTES + 1))))


def test_source_fallback_rejects_an_ambient_install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    installed_module = tmp_path / "site-packages" / "agentworks" / "release_notes.py"
    installed_module.parent.mkdir(parents=True)
    monkeypatch.setattr(release_notes_module, "__file__", str(installed_module))

    assert release_notes_module._source_tree_changelog() is None
