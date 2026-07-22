"""Unit tests for pure helpers in ``agentworks.output``."""

from __future__ import annotations

import concurrent.futures
import contextlib
from typing import TYPE_CHECKING

import pytest

from agentworks import output
from agentworks.output import Role

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput


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


def test_truncate_returns_fitting_text_unchanged() -> None:
    # Text at or under the width is returned as-is (no ellipsis).
    assert output.truncate("abcde", 5) == "abcde"
    assert output.truncate("abc", 5) == "abc"


def test_truncate_overflow_is_exactly_width_with_ellipsis() -> None:
    # An overflowing string becomes text[: width - 3] + "..." and is
    # exactly ``width`` characters long.
    result = output.truncate("abcdefghij", 6)
    assert result == "abc..."
    assert len(result) == 6


def test_truncate_degenerate_widths_never_overflow() -> None:
    # width <= 3 cannot fit the "..." marker; hard-truncate instead of
    # producing a negative slice (which would over-long the cell).
    assert output.truncate("abcde", 3) == "abc"
    assert output.truncate("abcde", 2) == "ab"
    assert output.truncate("abcde", 1) == "a"
    assert output.truncate("abcde", 0) == ""
    for width in range(0, 6):
        assert len(output.truncate("abcde", width)) <= width


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
    lines = output.render_table(["NAME", "MODE"], [[over, "admin"], ["short", "agent (x)"]], max_col_width=20)
    # Column width is capped at 20; "short" is padded to 20 before the gap.
    assert lines[3].startswith("short" + " " * 15 + "  ")


# -- Section state model -----------------------------------------------------


def test_section_pushes_and_restores_level() -> None:
    assert output._current_level() == 0
    with output.section(None):
        assert output._current_level() == 1
    assert output._current_level() == 0


def test_nested_sections_increment_and_restore_level() -> None:
    with output.section(None):
        assert output._current_level() == 1
        with output.section(None):
            assert output._current_level() == 2
        assert output._current_level() == 1
    assert output._current_level() == 0


def test_section_restores_level_on_exception() -> None:
    # A raise mid-section must not strand the ambient level.
    with contextlib.suppress(RuntimeError), output.section(None):
        assert output._current_level() == 1
        raise RuntimeError("boom")
    assert output._current_level() == 0


def test_headerless_section_pushes_without_header(
    captured_output: CapturedOutput,
) -> None:
    with output.section():
        output.info("inner")
    assert all(role is not Role.HEADER for role, _, _ in captured_output.lines)
    assert (Role.BODY, 1, "inner") in captured_output.lines


# -- Role + level capture ----------------------------------------------------


def test_no_section_emits_at_column_zero(captured_output: CapturedOutput) -> None:
    # Backward compat: with no section open every primitive reports level 0.
    output.info("a")
    output.detail("b")
    output.warn("c")
    assert (Role.BODY, 0, "a") in captured_output.lines
    assert (Role.DETAIL, 0, "b") in captured_output.lines
    assert (Role.WARNING, 0, "c") in captured_output.lines


def test_primitives_capture_role_and_ambient_level(
    captured_output: CapturedOutput,
) -> None:
    with output.section("Preflight"):
        output.info("step")
        output.detail("sub")
        output.warn("careful")
    assert (Role.HEADER, 0, "Preflight") in captured_output.lines
    assert (Role.BODY, 1, "step") in captured_output.lines
    # detail(indent=1) at level 1 -> level + indent - 1 = 1.
    assert (Role.DETAIL, 1, "sub") in captured_output.lines
    assert (Role.WARNING, 1, "careful") in captured_output.lines


def test_detail_indent_param_maps_to_relative_level(
    captured_output: CapturedOutput,
) -> None:
    # The deprecated indent= shim: at level 0, indent=2 -> level 0 + 2 - 1 = 1.
    output.detail("x", indent=2)
    assert (Role.DETAIL, 1, "x") in captured_output.lines


def test_result_always_reports_level_zero(captured_output: CapturedOutput) -> None:
    with output.section("Provisioning"), output.section(None):
        assert output._current_level() == 2
        output.result("done")
    assert (Role.RESULT, 0, "done") in captured_output.lines


def test_warn_from_thread_pool_worker_is_captured(
    captured_output: CapturedOutput,
) -> None:
    # Guards the level-only / global-handler decision: a bare worker
    # thread reads the ContextVar default (level 0) but must still reach
    # the installed (module-global) handler.
    def worker() -> None:
        output.warn("from worker")

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(worker).result()

    assert "from worker" in captured_output.warnings


# -- Default handler rendering (byte-identity + level ownership) --------------


def test_default_handler_body_byte_identical_at_level_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    output._DefaultHandler().emit(Role.BODY, "hello", 0)
    assert capsys.readouterr().out == "hello\n"


def test_default_handler_body_indents_with_level(
    capsys: pytest.CaptureFixture[str],
) -> None:
    output._DefaultHandler().emit(Role.BODY, "deep", 2)
    assert capsys.readouterr().out == "    deep\n"


def test_default_handler_detail_matches_legacy_indent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # detail(x) today == 2 spaces; detail(x, indent=2) == 4 spaces.
    handler = output._DefaultHandler()
    handler.emit(Role.DETAIL, "one", 0)  # detail(x) -> rendered level 0
    handler.emit(Role.DETAIL, "two", 1)  # detail(x, indent=2) -> rendered level 1
    assert capsys.readouterr().out == "  one\n    two\n"


def test_default_handler_warn_byte_identical_at_level_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    output._DefaultHandler().emit(Role.WARNING, "careful", 0)
    assert capsys.readouterr().err == "Warning: careful\n"


def test_default_handler_warn_indents_with_level(
    capsys: pytest.CaptureFixture[str],
) -> None:
    output._DefaultHandler().emit(Role.WARNING, "careful", 1)
    assert capsys.readouterr().err == "  Warning: careful\n"


def test_default_handler_phase_header_byte_identical(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # phase() today == blank line then "=== t ===".
    output._DefaultHandler().emit(Role.HEADER, "Preflight", 0)
    assert capsys.readouterr().out == "\n=== Preflight ===\n"


def test_default_handler_header_decoration_by_level(
    capsys: pytest.CaptureFixture[str],
) -> None:
    handler = output._DefaultHandler()
    handler.emit(Role.HEADER, "a", 1)
    assert capsys.readouterr().out == "\n  --- a ---\n"
    handler.emit(Role.HEADER, "b", 2)  # level 2+: plain, no blank line
    assert capsys.readouterr().out == "    b\n"


def test_default_handler_result_renders_at_column_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    output._DefaultHandler().emit(Role.RESULT, "done", 3)
    assert capsys.readouterr().out == "done\n"


def test_default_handler_prompt_byte_identical_at_level_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda text="": prompts.append(text) or "")
    output._DefaultHandler().prompt("Name", 0)
    assert prompts == ["Name: "]


def test_default_handler_prompt_indents_label_with_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda text="": prompts.append(text) or "")
    output._DefaultHandler().prompt("Name", 2)
    assert prompts == ["    Name: "]
