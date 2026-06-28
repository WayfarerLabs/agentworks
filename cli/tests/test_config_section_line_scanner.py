"""Tests for ``agentworks.source_location.scan_section_lines``.

The scanner is a small regex pre-pass over raw TOML text that builds a
``dict[tuple[str, ...], int]`` mapping each section header's dotted path
to its 1-based opening-line number. ``declared_at`` attachment in
``agentworks.config`` depends on it; ``tomllib`` itself loses line info.
"""

from __future__ import annotations

from textwrap import dedent

from agentworks.source_location import scan_section_lines


def test_top_level_sections() -> None:
    text = dedent(
        """\
        [operator]
        ssh_public_key = "x"

        [defaults]
        platform = "lima"
        """
    )
    assert scan_section_lines(text) == {
        ("operator",): 1,
        ("defaults",): 4,
    }


def test_dotted_subsections() -> None:
    text = dedent(
        """\
        [vm_templates.dev]
        cpus = 4

        [vm_templates.dev.env]
        FOO = "bar"

        [vm_templates.dev.env.nested]
        # supported by the scanner even though no agentworks kind uses it today
        x = 1
        """
    )
    assert scan_section_lines(text) == {
        ("vm_templates", "dev"): 1,
        ("vm_templates", "dev", "env"): 4,
        ("vm_templates", "dev", "env", "nested"): 7,
    }


def test_whitespace_around_brackets_and_dots() -> None:
    text = "[  vm_templates . dev  ]\nx = 1\n[ vm_templates.dev.env ]\ny = 2\n"
    assert scan_section_lines(text) == {
        ("vm_templates", "dev"): 1,
        ("vm_templates", "dev", "env"): 3,
    }


def test_trailing_comment_on_section_header() -> None:
    text = "[admin]  # operator's admin config\nusername = 'x'\n"
    assert scan_section_lines(text) == {("admin",): 1}


def test_commented_headers_ignored() -> None:
    text = dedent(
        """\
        # [vm_templates.commented]
        [vm_templates.real]
        cpus = 4
        """
    )
    assert scan_section_lines(text) == {("vm_templates", "real"): 2}


def test_array_of_tables_tolerated() -> None:
    text = dedent(
        """\
        [[deeply.arrayed]]
        x = 1

        [normal.section]
        y = 2
        """
    )
    assert scan_section_lines(text) == {
        ("deeply", "arrayed"): 1,
        ("normal", "section"): 4,
    }


def test_quoted_segments() -> None:
    text = dedent(
        """\
        [vm_templates."weird name"]
        cpus = 4

        [vm_templates.'literal name'.env]
        FOO = "bar"
        """
    )
    assert scan_section_lines(text) == {
        ("vm_templates", "weird name"): 1,
        ("vm_templates", "literal name", "env"): 4,
    }


def test_empty_text_returns_empty_map() -> None:
    assert scan_section_lines("") == {}


def test_text_with_no_sections() -> None:
    text = "# just a comment\nfoo = 'bar'\nbaz = 42\n"
    assert scan_section_lines(text) == {}


def test_duplicate_path_keeps_first_occurrence() -> None:
    # tomllib would already error on a duplicate section header at parse time;
    # the scanner's first-wins fallback is just a safety net for callers that
    # don't trip the tomllib check.
    text = "[admin]\nx = 1\n[admin]\ny = 2\n"
    assert scan_section_lines(text) == {("admin",): 1}
