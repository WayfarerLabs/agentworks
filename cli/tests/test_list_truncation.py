"""Display truncation for the human-readable ``list`` tables.

Long resource names must not misalign or balloon the ``vm`` / ``agent`` /
``workspace`` / ``console`` list tables: each renderer caps its NAME cell with
``output.truncate`` at a per-view width. These tests seed a pathologically long
name (bypassing ``validate_name`` via a direct DB insert, the way a legacy /
manually-inserted row would) and assert the table row truncates with an
ellipsis while a short sibling row stays column-aligned with it.

CRITICAL carve-out: ``--names-only`` (the shell-completion feed) must emit the
FULL untruncated name. Every view is checked for that too, so a truncation
change can never silently corrupt completion candidates.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.db import Database

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# A name longer than every per-view NAME cap (vm 42, agent 28, workspace 29,
# console 50), so it truncates in all four tables.
_LONG = "z" * 70
_SHORT = "aa"

_GET_DB_TARGETS = (
    "agentworks.cli.commands.vm.get_db",
    "agentworks.cli.commands.workspace.get_db",
    "agentworks.cli.commands.agent.get_db",
    "agentworks.cli.commands.console.get_db",
)


def _lines(stdout: str) -> list[str]:
    plain = _ANSI_RE.sub("", stdout)
    return [line for line in plain.splitlines() if line.strip()]


def _invoke(db: Database, argv: list[str]) -> tuple[int, list[str]]:
    runner = CliRunner()
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("agentworks.config.load_config", return_value=object()))
        for target in _GET_DB_TARGETS:
            stack.enter_context(patch(target, return_value=db))
        result = runner.invoke(app, argv)
    return result.exit_code, _lines(result.stdout)


def _row_for(lines: list[str], needle: str) -> str:
    """The single rendered line whose NAME cell starts with ``needle``."""
    matches = [line for line in lines if line.lstrip().startswith(needle)]
    assert len(matches) == 1, f"expected exactly one row starting with {needle!r}, got {matches}"
    return matches[0]


def _assert_truncated_and_aligned(lines: list[str], *, second_col: str, cap: int, truncated_prefix_len: int) -> None:
    """Assert the long row truncated with an ellipsis and both data rows keep
    the second column at the same offset (the table stayed aligned)."""
    expected = _LONG[:truncated_prefix_len] + "..."
    long_row = _row_for(lines, "z")
    short_row = _row_for(lines, _SHORT)

    assert "..." in long_row, f"long name should be ellipsized: {long_row!r}"
    assert expected in long_row, f"expected truncated {expected!r} in {long_row!r}"
    assert _LONG not in long_row, "full untruncated name must not appear in the table"
    assert len(expected) <= cap

    # Both data rows pad NAME to the same dynamic width, so the second column
    # begins at the same character offset in each: proof the table is aligned.
    assert long_row.index(second_col) == short_row.index(second_col), (
        f"second column {second_col!r} misaligned: long@{long_row.index(second_col)} "
        f"short@{short_row.index(second_col)}"
    )


def _seed_vm_rows(db: Database) -> None:
    db.insert_vm(_LONG, site="lima", hostname="h1")
    db.insert_vm(_SHORT, site="lima", hostname="h2")


# ---------------------------------------------------------------------------
# vm list (NAME cap 42, truncate keeps 39 + "...")
# ---------------------------------------------------------------------------


def test_vm_list_truncates_long_name_and_stays_aligned(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    _seed_vm_rows(db)
    code, lines = _invoke(db, ["vm", "list"])
    assert code == 0, lines
    _assert_truncated_and_aligned(lines, second_col="lima", cap=42, truncated_prefix_len=39)
    db.close()


def test_vm_list_names_only_emits_full_name(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    _seed_vm_rows(db)
    code, lines = _invoke(db, ["vm", "list", "--names-only"])
    assert code == 0, lines
    assert _LONG in lines, "names-only must emit the full untruncated name for completion"
    db.close()


# ---------------------------------------------------------------------------
# agent list (NAME cap 28, truncate keeps 25 + "...")
# ---------------------------------------------------------------------------


def _seed_agent_rows(db: Database) -> None:
    db.insert_vm("vm1", site="lima", hostname="h")
    db.insert_agent(_LONG, "vm1", "agt-x")
    db.insert_agent(_SHORT, "vm1", "agt-y")


def test_agent_list_truncates_long_name_and_stays_aligned(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    _seed_agent_rows(db)
    code, lines = _invoke(db, ["agent", "list"])
    assert code == 0, lines
    _assert_truncated_and_aligned(lines, second_col="vm1", cap=28, truncated_prefix_len=25)
    db.close()


def test_agent_list_names_only_emits_full_name(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    _seed_agent_rows(db)
    code, lines = _invoke(db, ["agent", "list", "--names-only"])
    assert code == 0, lines
    assert _LONG in lines
    db.close()


# ---------------------------------------------------------------------------
# workspace list (NAME cap 29, truncate keeps 26 + "...")
# ---------------------------------------------------------------------------


def _seed_workspace_rows(db: Database) -> None:
    db.insert_vm("vm1", site="lima", hostname="h")
    db.insert_workspace(_LONG, workspace_path="/tmp/a", vm_name="vm1", linux_group="ws-a")
    db.insert_workspace(_SHORT, workspace_path="/tmp/b", vm_name="vm1", linux_group="ws-b")


def test_workspace_list_truncates_long_name_and_stays_aligned(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    _seed_workspace_rows(db)
    code, lines = _invoke(db, ["workspace", "list"])
    assert code == 0, lines
    _assert_truncated_and_aligned(lines, second_col="vm1", cap=29, truncated_prefix_len=26)
    db.close()


def test_workspace_list_names_only_emits_full_name(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    _seed_workspace_rows(db)
    code, lines = _invoke(db, ["workspace", "list", "--names-only"])
    assert code == 0, lines
    assert _LONG in lines
    db.close()


# ---------------------------------------------------------------------------
# console list (NAME cap 50, truncate keeps 47 + "...")
# ---------------------------------------------------------------------------


def _seed_console_rows(db: Database) -> None:
    db.insert_vm("vm1", site="lima", hostname="h")
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES (?, 'vm1')", (_LONG,))
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES (?, 'vm1')", (_SHORT,))
    db._conn.commit()


def test_console_list_truncates_long_name_and_stays_aligned(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    _seed_console_rows(db)
    code, lines = _invoke(db, ["console", "list"])
    assert code == 0, lines
    _assert_truncated_and_aligned(lines, second_col="vm1", cap=50, truncated_prefix_len=47)
    db.close()


def test_console_list_names_only_emits_full_name(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    _seed_console_rows(db)
    code, lines = _invoke(db, ["console", "list", "--names-only"])
    assert code == 0, lines
    assert _LONG in lines
    db.close()
