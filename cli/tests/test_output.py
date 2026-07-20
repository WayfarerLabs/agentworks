"""Unit tests for pure helpers in ``agentworks.output``."""

from __future__ import annotations

from agentworks import output


def test_count_pluralizes_regular_nouns() -> None:
    assert output.count(0, "package") == "0 packages"
    assert output.count(1, "package") == "1 package"
    assert output.count(2, "package") == "2 packages"


def test_count_multiword_noun() -> None:
    assert output.count(1, "apt package") == "1 apt package"
    assert output.count(3, "apt package") == "3 apt packages"


def test_count_irregular_plural() -> None:
    assert output.count(1, "PATH entry", "PATH entries") == "1 PATH entry"
    assert output.count(4, "PATH entry", "PATH entries") == "4 PATH entries"


def test_render_table_headers_and_rule_line() -> None:
    lines = output.render_table(["NAME", "STATUS"], [["s1", "running"]])
    # Header first, then a dashed rule matching the header's rendered width.
    assert lines[0] == "NAME  STATUS"
    assert lines[1] == "-" * len(lines[0])
    assert lines[2] == "s1    running"


def test_render_table_short_column_keeps_natural_width() -> None:
    # A column whose cells all fit under the cap stays at its natural
    # width; it is not padded out to max_col_width.
    lines = output.render_table(["NAME", "MODE"], [["s1", "admin"]])
    # NAME column is 4 wide (header), two-space gap, then MODE (5 wide).
    assert lines[0] == "NAME  MODE"
    assert lines[2] == "s1    admin"


def test_render_table_caps_over_width_cell_with_ellipsis() -> None:
    # A 21-char cell is truncated to first 17 chars plus "..." = 20 chars.
    over = "a" * 21
    lines = output.render_table(["NAME"], [[over]], max_col_width=20)
    rendered_cell = lines[2]
    assert rendered_cell == "a" * 17 + "..."
    assert len(rendered_cell) == 20


def test_render_table_exact_cap_cell_not_truncated() -> None:
    # A cell of exactly max_col_width is left intact (no ellipsis).
    exact = "b" * 20
    lines = output.render_table(["NAME"], [[exact]], max_col_width=20)
    assert lines[2] == exact


def test_truncate_cell_degenerate_widths_never_overflow() -> None:
    # width <= 3 cannot fit the "..." marker; hard-truncate instead of
    # producing a negative slice (which would over-long the cell).
    assert output._truncate_cell("abcde", 3) == "abc"
    assert output._truncate_cell("abcde", 2) == "ab"
    assert output._truncate_cell("abcde", 1) == "a"
    assert output._truncate_cell("abcde", 0) == ""
    for width in range(0, 6):
        assert len(output._truncate_cell("abcde", width)) <= width


def test_render_table_degenerate_max_col_width() -> None:
    # A cap of 3 still renders coherently: every cell fits its column.
    lines = output.render_table(["AB"], [["ABCDE"]], max_col_width=3)
    assert lines[0] == "AB"
    assert lines[1] == "--"
    assert lines[2] == "ABC"


def test_render_table_over_cap_column_pads_short_cells_to_cap() -> None:
    # When one cell forces the column to the cap, shorter cells in that
    # column are left-justified out to the cap width.
    over = "a" * 25
    lines = output.render_table(
        ["NAME", "MODE"], [[over, "admin"], ["short", "agent (x)"]], max_col_width=20
    )
    # Column width is capped at 20; "short" is padded to 20 before the gap.
    assert lines[3].startswith("short" + " " * 15 + "  ")
