"""Tests for the ``--names-only`` flag on every `list` command.

Pins the completion contract documented in
``.rulesync/rules/cli-conventions.md``: every ``<resource> list``
emits one name per line under ``--names-only``, the order matches
the table's row order, and filters compose (``--names-only`` is a
presentation switch, not a filter).

Shell completion (``cli/agentworks/completions/{bash,zsh,powershell}.py``)
shells out via this flag rather than parsing the human-readable table,
so a regression here silently breaks tab-completion across three shells.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.db import Database

# Typer renders errors through rich, which inserts ANSI escapes. Tests
# that check command success via exit_code don't need this, but a few
# assertions inspect stdout lines, so strip color defensively. See the
# session-agent-filter tests for the same pattern.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _names(stdout: str) -> list[str]:
    """Parse the stdout of ``... list --names-only`` into a name list."""
    plain = _ANSI_RE.sub("", stdout)
    return [line for line in plain.splitlines() if line.strip()]


def _seed(tmp_path: Path) -> Database:
    """Two VMs, two workspaces, two agents, two sessions, two consoles.

    Two-of-each so the order-matching assertion has something to check
    AND so filters (``--vm vm1``) actually narrow the set.
    """
    db = Database(tmp_path / "test.db")
    db.insert_vm("vm-a", site="lima", hostname="lima--vm-a")
    db.insert_vm("vm-b", site="lima", hostname="lima--vm-b")
    db.insert_workspace("ws-a", workspace_path="/tmp/ws-a", vm_name="vm-a", linux_group="ws-ws-a")
    db.insert_workspace("ws-b", workspace_path="/tmp/ws-b", vm_name="vm-b", linux_group="ws-ws-b")
    db.insert_agent("agent-a", "vm-a", "agt-agent-a")
    db.insert_agent("agent-b", "vm-b", "agt-agent-b")
    from agentworks.db import SessionMode

    db.insert_session("sess-a", "ws-a", template="default", mode=SessionMode.ADMIN)
    db.insert_session("sess-b", "ws-b", template="default", mode=SessionMode.ADMIN)
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('con-a', 'vm-a')")
    db._conn.execute("INSERT INTO consoles (name, vm_name) VALUES ('con-b', 'vm-b')")
    db._conn.commit()
    return db


# ``get_db`` is imported by-name into each command module, so a naive
# ``patch("agentworks.cli._helpers.get_db", ...)`` would miss those
# already-bound references. Patch the symbol inside each list-command
# module instead.
_GET_DB_TARGETS = (
    "agentworks.cli.commands.vm.get_db",
    "agentworks.cli.commands.workspace.get_db",
    "agentworks.cli.commands.agent.get_db",
    "agentworks.cli.commands.session.get_db",
    "agentworks.cli.commands.console.get_db",
)


def _invoke(db: Database, argv: list[str]) -> tuple[int, list[str]]:
    """Run ``agw <argv>`` with our seeded db and a stub config."""
    runner = CliRunner()
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch("agentworks.config.load_config", return_value=object()),
        )
        for target in _GET_DB_TARGETS:
            stack.enter_context(patch(target, return_value=db))
        result = runner.invoke(app, argv)
    return result.exit_code, _names(result.stdout)


# ---------------------------------------------------------------------------
# Each list command emits one name per line under --names-only.
# ---------------------------------------------------------------------------


def test_vm_list_names_only_emits_one_per_line(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    code, names = _invoke(db, ["vm", "list", "--names-only"])
    assert code == 0, names
    assert names == ["vm-a", "vm-b"]


def test_workspace_list_names_only_emits_one_per_line(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    code, names = _invoke(db, ["workspace", "list", "--names-only"])
    assert code == 0, names
    assert names == ["ws-a", "ws-b"]


def test_agent_list_names_only_emits_one_per_line(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    code, names = _invoke(db, ["agent", "list", "--names-only"])
    assert code == 0, names
    assert names == ["agent-a", "agent-b"]


def test_session_list_names_only_emits_one_per_line(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    code, names = _invoke(db, ["session", "list", "--names-only"])
    assert code == 0, names
    assert set(names) == {"sess-a", "sess-b"}


def test_console_list_names_only_emits_one_per_line(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    code, names = _invoke(db, ["console", "list", "--names-only"])
    assert code == 0, names
    assert names == ["con-a", "con-b"]


# ---------------------------------------------------------------------------
# Filters compose with --names-only (it's a presentation switch, not a filter).
# ---------------------------------------------------------------------------


def test_workspace_list_names_only_respects_vm_filter(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    code, names = _invoke(db, ["workspace", "list", "--vm", "vm-a", "--names-only"])
    assert code == 0, names
    assert names == ["ws-a"]


def test_agent_list_names_only_respects_vm_filter(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    code, names = _invoke(db, ["agent", "list", "--vm", "vm-b", "--names-only"])
    assert code == 0, names
    assert names == ["agent-b"]


def test_session_list_names_only_respects_vm_filter(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    code, names = _invoke(db, ["session", "list", "--vm", "vm-a", "--names-only"])
    assert code == 0, names
    assert names == ["sess-a"]


def test_console_list_names_only_respects_vm_filter(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    code, names = _invoke(db, ["console", "list", "--vm", "vm-b", "--names-only"])
    assert code == 0, names
    assert names == ["con-b"]


# ---------------------------------------------------------------------------
# Empty result: --names-only prints NOTHING (no friendly "No X" message).
# Otherwise the friendly string would land in the completion candidate set
# whenever the resource list is empty, polluting tab-completion.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subcommand",
    [
        ["vm", "list", "--names-only"],
        ["workspace", "list", "--names-only"],
        ["agent", "list", "--names-only"],
        ["session", "list", "--names-only"],
        ["console", "list", "--names-only"],
    ],
)
def test_list_names_only_empty_db_prints_nothing(
    tmp_path: Path,
    subcommand: list[str],
) -> None:
    db = Database(tmp_path / "test.db")
    code, names = _invoke(db, subcommand)
    assert code == 0, names
    assert names == [], (
        f"empty {subcommand[0]} list under --names-only should produce "
        f"no output (would otherwise pollute completion); got: {names}"
    )
