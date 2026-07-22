"""Tests for session-spec parsing, `_validate_cwd`, and the console DB layer.

Split out of `test_consoles.py` (see `.claude/rules/code-style.md` on file-size
targets). Shared seed helpers and stub Config classes live in
`tests/_consoles_support.py`; sibling shards cover the manager-level
orchestration and tmux-facing behavior.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from agentworks.db import ConsoleRow, Database, _parse_shells
from agentworks.errors import ValidationError
from agentworks.sessions.multi_console import (
    SessionSpec,
    _validate_cwd,
    default_shells,
    parse_session_spec,
    tmux_session_name,
)
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry  # noqa: F401

# -- parse_session_spec ----------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("foo", SessionSpec(name="foo", shells=0)),
        ("foo+0", SessionSpec(name="foo", shells=0)),
        ("foo+3", SessionSpec(name="foo", shells=3)),
        ("a-b_c+12", SessionSpec(name="a-b_c", shells=12)),
        # Legacy <workspace>--<agent> names from before validate_name banned
        # consecutive hyphens. parse_session_spec is the path that builds
        # console specs (console create / console add-sessions), so it uses
        # the loose validator. Other reference paths (session delete, attach,
        # stop, logs) don't go through parse_session_spec at all -- they hit
        # db.get_session() directly, which has never validated names.
        ("myws--bot", SessionSpec(name="myws--bot", shells=0)),
        ("myws--bot+2", SessionSpec(name="myws--bot", shells=2)),
    ],
)
def test_parse_session_spec_ok(spec: str, expected: SessionSpec) -> None:
    assert parse_session_spec(spec) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "foo+",  # trailing plus
        "foo+x",  # non-numeric
        "foo++2",  # double plus
        "+5",  # empty name
        "FOO",  # uppercase
        "foo+-1",  # negative
        "foo+1+2",  # multiple plus
        "",  # empty
        "a.b",  # contains dot -- still rejected by the loose validator
        "a/b",  # contains slash
        "a b",  # contains space
    ],
)
def test_parse_session_spec_rejects_bad_input(bad: str) -> None:
    with pytest.raises(ValidationError):
        parse_session_spec(bad)


def test_default_shells() -> None:
    assert default_shells(0) == []
    assert default_shells(3) == [
        {"cwd": None, "admin": False},
        {"cwd": None, "admin": False},
        {"cwd": None, "admin": False},
    ]


def test_tmux_session_name_prefix() -> None:
    assert tmux_session_name("foo") == "aw-console-foo"


# -- _validate_cwd ---------------------------------------------------------


@pytest.mark.parametrize("cwd", [None, "src", "src/api", "a/b/c"])
def test_validate_cwd_accepts_relative(cwd: str | None) -> None:
    _validate_cwd(cwd)


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        "/etc",  # absolute
        "/",  # absolute root
        "..",  # parent
        "../etc",  # parent escape
        "src/../etc",  # mid-path parent
        "a/..",  # trailing parent
    ],
)
def test_validate_cwd_rejects_escapes(bad: str) -> None:
    with pytest.raises(ValidationError):
        _validate_cwd(bad)


# -- DB layer --------------------------------------------------------------


def test_console_crud(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])

    console = db.insert_console("con", "vm1")
    assert isinstance(console, ConsoleRow)
    assert console.name == "con"
    assert console.vm_name == "vm1"

    assert db.get_console("con") == console
    assert db.list_consoles() == [console]
    assert db.list_consoles(vm_name="vm1") == [console]
    assert db.list_consoles(vm_name="other") == []

    cs1 = db.add_console_session("con", "a", [{"cwd": None, "admin": False}])
    cs2 = db.add_console_session("con", "b", [])
    assert cs1.position == 0
    assert cs2.position == 1

    members = db.list_console_sessions("con")
    assert [m.session_name for m in members] == ["a", "b"]
    assert members[0].shells == [{"cwd": None, "admin": False}]


def test_position_auto_increments_atomically(db: Database) -> None:
    """The position column uses INSERT...SELECT MAX+1, not a read-then-insert."""
    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c"])
    db.insert_console("con", "vm1")

    db.add_console_session("con", "a", [])
    db.add_console_session("con", "b", [])
    db.add_console_session("con", "c", [])

    positions = [m.position for m in db.list_console_sessions("con")]
    assert positions == [0, 1, 2]


def test_remove_leaves_position_gap(db: Database) -> None:
    """Removing a session does not renumber positions; new adds get max+1."""
    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c", "d"])
    db.insert_console("con", "vm1")
    for n in ["a", "b", "c"]:
        db.add_console_session("con", n, [])

    db.remove_console_session("con", "b")
    db.add_console_session("con", "d", [])

    members = db.list_console_sessions("con")
    assert [(m.session_name, m.position) for m in members] == [
        ("a", 0),
        ("c", 2),
        ("d", 3),
    ]


def test_unique_position_constraint(db: Database) -> None:
    """The UNIQUE (console_name, position) constraint protects against duplicate positions."""
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    db.insert_console("con", "vm1")
    db.add_console_session("con", "a", [])
    # Manually insert with a colliding position (bypasses the auto-increment SQL)
    with pytest.raises(sqlite3.IntegrityError):
        db._conn.execute(
            "INSERT INTO console_sessions (console_name, session_name, position, shells) VALUES ('con', 'b', 0, '[]')",
        )


def test_duplicate_membership_rejected(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    db.insert_console("con", "vm1")
    db.add_console_session("con", "a", [])
    with pytest.raises(sqlite3.IntegrityError):
        db.add_console_session("con", "a", [])


def test_shells_json_roundtrip(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    db.insert_console("con", "vm1")
    payload = [
        {"cwd": None, "admin": False},
        {"cwd": "src/api", "admin": False},
        {"cwd": None, "admin": True},
    ]
    db.add_console_session("con", "a", payload)
    fetched = db.get_console_session("con", "a")
    assert fetched is not None
    assert fetched.shells == payload


def test_update_console_shells(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    db.insert_console("con", "vm1")
    db.add_console_session("con", "a", [])
    db.update_console_shells("con", "a", [{"cwd": None, "admin": True}])
    fetched = db.get_console_session("con", "a")
    assert fetched is not None
    assert fetched.shells == [{"cwd": None, "admin": True}]


def test_list_consoles_with_counts(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c"])
    db.insert_console("empty", "vm1")
    db.insert_console("full", "vm1")
    db.add_console_session("full", "a", [])
    db.add_console_session("full", "b", [])

    results = db.list_consoles_with_counts()
    assert [(c.name, n) for c, n in results] == [("empty", 0), ("full", 2)]


def test_list_consoles_with_counts_workspace_and_agent_filters(db: Database) -> None:
    """Filters use 'any session matches' semantics; total count is preserved."""
    _seed_vm(db, "vm1")
    # Second workspace on the same VM, plus a second VM for the vm filter.
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws-2', 'vm1', '/home/me/ws-2', 'ws-ws-2')"
    )
    db._conn.execute("INSERT INTO vms (name, site, hostname, admin_username) VALUES ('vm2', 'wsl', 'h', 'admin')")
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES ('ws-other', 'vm2', '/home/me/ws-other', 'ws-ws-other')"
    )
    db.insert_agent("coder", "vm1", "agt--coder")
    db.insert_agent("helper", "vm1", "agt--helper")
    db._conn.commit()

    # Sessions:
    #   sess-a-coder   -> ws-vm1 (default seed), agent=coder
    #   sess-b-helper  -> ws-2,                  agent=helper
    #   sess-c-admin   -> ws-vm1,                admin
    #   sess-other     -> ws-other on vm2,       admin
    from agentworks.db import SessionMode

    db.insert_session(
        "sess-a-coder",
        "ws-vm1",
        "default",
        SessionMode.AGENT,
        agent_name="coder",
        socket_path="/sock-a",
    )
    db.insert_session(
        "sess-b-helper",
        "ws-2",
        "default",
        SessionMode.AGENT,
        agent_name="helper",
        socket_path="/sock-b",
    )
    db.insert_session("sess-c-admin", "ws-vm1", "default", SessionMode.ADMIN)
    db.insert_session("sess-other", "ws-other", "default", SessionMode.ADMIN)

    # Consoles:
    #   mixed    on vm1 -> sess-a-coder, sess-b-helper, sess-c-admin (3 sessions, spans 2 workspaces, 2 agents)
    #   single   on vm1 -> sess-c-admin only (1 session, admin)
    #   far      on vm2 -> sess-other (1 session)
    #   empty    on vm1 -> no sessions
    db.insert_console("mixed", "vm1")
    db.add_console_session("mixed", "sess-a-coder", [])
    db.add_console_session("mixed", "sess-b-helper", [])
    db.add_console_session("mixed", "sess-c-admin", [])
    db.insert_console("single", "vm1")
    db.add_console_session("single", "sess-c-admin", [])
    db.insert_console("far", "vm2")
    db.add_console_session("far", "sess-other", [])
    db.insert_console("empty", "vm1")

    # workspace=ws-vm1 matches both consoles that include any ws-vm1 session.
    # The count returned is total sessions in each console, not the matching count.
    results = db.list_consoles_with_counts(workspace_name="ws-vm1")
    assert [(c.name, n) for c, n in results] == [("mixed", 3), ("single", 1)]

    # workspace=ws-2 matches only `mixed`.
    results = db.list_consoles_with_counts(workspace_name="ws-2")
    assert [(c.name, n) for c, n in results] == [("mixed", 3)]

    # workspace=ws-other matches only `far` (on vm2).
    results = db.list_consoles_with_counts(workspace_name="ws-other")
    assert [(c.name, n) for c, n in results] == [("far", 1)]

    # agent=coder matches consoles with at least one coder session.
    results = db.list_consoles_with_counts(agent_name="coder")
    assert [(c.name, n) for c, n in results] == [("mixed", 3)]

    # agent=helper matches consoles with at least one helper session.
    results = db.list_consoles_with_counts(agent_name="helper")
    assert [(c.name, n) for c, n in results] == [("mixed", 3)]

    # AND composition: vm=vm1 + workspace=ws-other -> empty (ws-other is on vm2).
    assert db.list_consoles_with_counts(vm_name="vm1", workspace_name="ws-other") == []

    # Session-level filters require the SAME session to match all predicates.
    # workspace=ws-vm1 + agent=coder -> matches `mixed` because sess-a-coder
    # is in ws-vm1 AND run by coder (one session satisfies both).
    results = db.list_consoles_with_counts(workspace_name="ws-vm1", agent_name="coder")
    assert [(c.name, n) for c, n in results] == [("mixed", 3)]

    # workspace=ws-vm1 + agent=helper -> empty. `mixed` contains both ws-vm1
    # sessions (sess-a-coder, sess-c-admin) and helper sessions (sess-b-helper),
    # but no single session is both in ws-vm1 AND run by helper. The combined
    # predicate avoids the surprise of consoles matching via unrelated sessions.
    assert db.list_consoles_with_counts(workspace_name="ws-vm1", agent_name="helper") == []

    # Multi-value (list) on workspace: OR within the filter. ws-vm1 OR ws-2 match
    # `mixed` (one of each) and `single` (ws-vm1 only).
    results = db.list_consoles_with_counts(workspace_name=["ws-vm1", "ws-2"])
    assert [(c.name, n) for c, n in results] == [("mixed", 3), ("single", 1)]

    # Multi-value on vm_name (console-level filter): vm1 OR vm2 returns every console.
    results = db.list_consoles_with_counts(vm_name=["vm1", "vm2"])
    assert [(c.name, n) for c, n in results] == [
        ("empty", 0),
        ("far", 1),
        ("mixed", 3),
        ("single", 1),
    ]

    # Multi-value session filter still requires SAME session to satisfy combined predicates.
    # agent IN (coder, helper) AND workspace = ws-2: only sess-b-helper qualifies (in ws-2),
    # so only `mixed` matches.
    results = db.list_consoles_with_counts(workspace_name="ws-2", agent_name=["coder", "helper"])
    assert [(c.name, n) for c, n in results] == [("mixed", 3)]

    # Single-element list behaves identically to a bare string.
    assert db.list_consoles_with_counts(vm_name=["vm1"]) == db.list_consoles_with_counts(vm_name="vm1")


def test_cascade_session_delete_removes_membership(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    db.insert_console("con", "vm1")
    db.add_console_session("con", "a", [])
    db.add_console_session("con", "b", [])
    db.delete_session("a")
    members = db.list_console_sessions("con")
    assert [m.session_name for m in members] == ["b"]


def test_cascade_console_delete_removes_memberships(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    db.insert_console("con", "vm1")
    db.add_console_session("con", "a", [])
    db.add_console_session("con", "b", [])
    db.delete_console("con")
    # Console gone, memberships gone
    assert db.get_console("con") is None
    rows = db._conn.execute("SELECT * FROM console_sessions").fetchall()
    assert rows == []


def test_cascade_vm_delete_removes_consoles(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    db.insert_console("con", "vm1")
    db.add_console_session("con", "a", [])
    db.delete_vm("vm1")
    assert db.list_consoles() == []
    rows = db._conn.execute("SELECT * FROM console_sessions").fetchall()
    assert rows == []


def test_reorder_console_sessions_rewrites_positions(db: Database) -> None:
    """Full-list reorder rewrites positions atomically without tripping
    UNIQUE(console_name, position)."""
    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c", "d"])
    db.insert_console("con", "vm1")
    for n in ["a", "b", "c", "d"]:
        db.add_console_session("con", n, [])

    db.reorder_console_sessions("con", ["c", "a", "d", "b"])

    members = db.list_console_sessions("con")
    assert [m.session_name for m in members] == ["c", "a", "d", "b"]
    assert [m.position for m in members] == [0, 1, 2, 3]


def test_reorder_console_sessions_rejects_wrong_member_set(db: Database) -> None:
    """The DB primitive expects the full current membership, no extras /
    no missing -- guards against manager-layer bugs that would otherwise
    leave the table in a half-reordered state."""
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    db.insert_console("con", "vm1")
    db.add_console_session("con", "a", [])
    db.add_console_session("con", "b", [])

    with pytest.raises(ValueError, match="full member list"):
        db.reorder_console_sessions("con", ["a"])  # missing b
    with pytest.raises(ValueError, match="full member list"):
        db.reorder_console_sessions("con", ["a", "b", "c"])  # c not a member
    with pytest.raises(ValueError, match="full member list"):
        db.reorder_console_sessions("con", ["a", "a"])  # duplicate a, missing b


# -- Transaction safety ----------------------------------------------------


def test_transaction_rollback_on_failure(db: Database) -> None:
    """A failure mid-transaction rolls back partial console_sessions inserts."""
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    db.insert_console("con", "vm1")

    with pytest.raises(sqlite3.IntegrityError), db.transaction():
        db.add_console_session("con", "a", [])
        db.add_console_session("con", "b", [])
        db.add_console_session("con", "b", [])  # PK violation

    # Neither 'a' nor 'b' should be present after rollback.
    assert db.list_console_sessions("con") == []


def test_transaction_nested(db: Database) -> None:
    """Nested transaction blocks defer to the outermost; no premature commit."""
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    db.insert_console("con", "vm1")

    try:
        with db.transaction():
            db.add_console_session("con", "a", [])
            with db.transaction():
                # Inner block adds nothing; outer raises after.
                pass
            raise RuntimeError("force rollback")
    except RuntimeError:
        pass

    # Outermost rollback fired -> 'a' not committed.
    assert db.list_console_sessions("con") == []


# -- _parse_shells validation ---------------------------------------------


def test_parse_shells_accepts_valid() -> None:
    raw = json.dumps([{"cwd": None, "admin": False}, {"cwd": "x", "admin": True}])
    assert _parse_shells(raw, "c", "s") == [
        {"cwd": None, "admin": False},
        {"cwd": "x", "admin": True},
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '"a string"',
        "{}",  # dict, not list
        "[null]",  # null entry
        '[{"cwd": null}]',  # missing admin
        '[{"admin": false}]',  # missing cwd
        '[{"cwd": null, "admin": false, "extra": 1}]',  # extra key
        '[{"cwd": 1, "admin": false}]',  # cwd not str
        '[{"cwd": null, "admin": "yes"}]',  # admin not bool
    ],
)
def test_parse_shells_rejects_bad_shapes(raw: str) -> None:
    with pytest.raises(ValueError):
        _parse_shells(raw, "c", "s")


def test_get_console_session_raises_on_corrupted_shells(db: Database) -> None:
    """Manually-corrupted JSON surfaces as a ValueError instead of silently drifting."""
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    db.insert_console("con", "vm1")
    db.add_console_session("con", "a", [])
    db._conn.execute(
        "UPDATE console_sessions SET shells = ? WHERE console_name = ? AND session_name = ?",
        ('[{"cwd": null}]', "con", "a"),
    )
    db._conn.commit()
    with pytest.raises(ValueError):
        db.get_console_session("con", "a")
