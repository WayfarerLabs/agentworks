"""Tests for named consoles (DB + orchestration)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.db import ConsoleRow, Database, _parse_shells
from agentworks.errors import (
    AlreadyExistsError,
    ConnectivityError,
    NotFoundError,
    ValidationError,
)
from agentworks.sessions.multi_console import (
    SHELL_INDEX_OPTION,
    SessionSpec,
    _validate_cwd,
    add_sessions,
    add_shell,
    create_console,
    default_shells,
    delete_console,
    delete_console_record,
    describe_console,
    list_consoles,
    parse_session_spec,
    remove_sessions,
    reorder_sessions,
    tmux_session_name,
)
from tests.conftest import _FakeResult, _FakeTarget

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput


# -- Helpers ---------------------------------------------------------------


def _seed_vm(db: Database, vm_name: str = "vm1", *, with_tailscale: bool = False) -> None:
    """Insert a VM and a workspace. No tailscale host -> live-sync skips."""
    db._conn.execute(
        "INSERT INTO vms (name, platform, admin_username) VALUES (?, 'lima', 'admin')",
        (vm_name,),
    )
    if with_tailscale:
        db._conn.execute(
            "UPDATE vms SET tailscale_host = ? WHERE name = ?",
            (f"100.64.0.{hash(vm_name) % 250}", vm_name),
        )
    db._conn.execute(
        "INSERT INTO workspaces (name, vm_name, workspace_path, linux_group) "
        "VALUES (?, ?, ?, ?)",
        (f"ws-{vm_name}", vm_name, f"/home/me/{vm_name}", f"ws-ws-{vm_name}"),
    )
    db._conn.commit()


def _seed_sessions(db: Database, names: list[str], *, workspace_name: str = "ws-vm1") -> None:
    for n in names:
        db._conn.execute(
            "INSERT INTO sessions (name, workspace_name, template, mode) "
            "VALUES (?, ?, 'default', 'admin')",
            (n, workspace_name),
        )
    db._conn.commit()


class _StubNamedConsoleConfig:
    tmux_layout: str = "tiled"


class _StubConfig:
    """A no-op Config stand-in.

    Tests that don't install the ``fake_target`` fixture also use VMs seeded
    with ``with_tailscale=False`` so ``_live_target`` returns None up front
    and the SSH layer is never entered. If you set ``with_tailscale=True``
    without monkey-patching ``admin_exec_target`` you will hit an
    AttributeError on this stub -- prefer the ``fake_target`` fixture.

    ``named_console`` provides only what multi_console reads from Config;
    extend here as new fields are added to NamedConsoleConfig.
    """

    named_console = _StubNamedConsoleConfig()


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
        "foo+",          # trailing plus
        "foo+x",         # non-numeric
        "foo++2",        # double plus
        "+5",            # empty name
        "FOO",           # uppercase
        "foo+-1",        # negative
        "foo+1+2",       # multiple plus
        "",              # empty
        "a.b",           # contains dot -- still rejected by the loose validator
        "a/b",           # contains slash
        "a b",           # contains space
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
        "",          # empty
        "/etc",      # absolute
        "/",         # absolute root
        "..",        # parent
        "../etc",    # parent escape
        "src/../etc",  # mid-path parent
        "a/..",      # trailing parent
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
            "INSERT INTO console_sessions (console_name, session_name, position, shells) "
            "VALUES ('con', 'b', 0, '[]')",
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
    db._conn.execute(
        "INSERT INTO vms (name, platform, admin_username) VALUES ('vm2', 'wsl', 'admin')"
    )
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
        "sess-a-coder", "ws-vm1", "default", SessionMode.AGENT,
        agent_name="coder", socket_path="/sock-a",
    )
    db.insert_session(
        "sess-b-helper", "ws-2", "default", SessionMode.AGENT,
        agent_name="helper", socket_path="/sock-b",
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
        ("empty", 0), ("far", 1), ("mixed", 3), ("single", 1),
    ]

    # Multi-value session filter still requires SAME session to satisfy combined predicates.
    # agent IN (coder, helper) AND workspace = ws-2: only sess-b-helper qualifies (in ws-2),
    # so only `mixed` matches.
    results = db.list_consoles_with_counts(workspace_name="ws-2", agent_name=["coder", "helper"])
    assert [(c.name, n) for c, n in results] == [("mixed", 3)]

    # Single-element list behaves identically to a bare string.
    assert db.list_consoles_with_counts(vm_name=["vm1"]) == db.list_consoles_with_counts(
        vm_name="vm1"
    )


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
        "{}",                                  # dict, not list
        '[null]',                              # null entry
        '[{"cwd": null}]',                     # missing admin
        '[{"admin": false}]',                  # missing cwd
        '[{"cwd": null, "admin": false, "extra": 1}]',  # extra key
        '[{"cwd": 1, "admin": false}]',        # cwd not str
        '[{"cwd": null, "admin": "yes"}]',     # admin not bool
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


# -- Orchestration: create_console -----------------------------------------


def test_create_console_explicit_specs(db: Database, captured_output: CapturedOutput) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["alpha", "beta", "gamma"])
    create_console(
        db,
        name="backend",
        vm_name="vm1",
        session_specs=["beta+2", "alpha", "gamma+1"],
    )
    members = db.list_console_sessions("backend")
    assert [(m.session_name, len(m.shells)) for m in members] == [
        ("beta", 2),
        ("alpha", 0),
        ("gamma", 1),
    ]


def test_running_session_names_raises_on_unreachable(
    db: Database, fake_target: _FakeTarget
) -> None:
    """If sessions exist with valid pid+boot_id but the probe returns nothing,
    treat that as a transport failure and raise instead of silently reporting
    'no running sessions'."""
    from agentworks.sessions.multi_console import running_session_names

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    db._conn.execute(
        "UPDATE sessions SET pid = 100, boot_id = 'b' WHERE name = 'alpha'"
    )
    db._conn.commit()
    # Probe returns empty stdout (simulates transport failure caught by check=False).
    fake_target.run = lambda command, **kwargs: _FakeResult(returncode=255, stdout="")  # type: ignore[assignment]

    with pytest.raises(ConnectivityError, match="could not determine running"):
        running_session_names(db, _StubConfig(), "vm1")


def test_running_session_names_uses_live_status_check(
    db: Database, fake_target: _FakeTarget
) -> None:
    """running_session_names SSH-probes via batch_check_all_sessions and
    returns only sessions whose live tmux state is OK."""
    from agentworks.sessions.multi_console import running_session_names

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha", "beta", "gamma"])
    # Give each session a PID so it's eligible for the batch check.
    db._conn.execute(
        "UPDATE sessions SET pid = 100, boot_id = 'b' WHERE name = 'alpha'"
    )
    db._conn.execute(
        "UPDATE sessions SET pid = 200, boot_id = 'b' WHERE name = 'beta'"
    )
    db._conn.execute(
        "UPDATE sessions SET pid = 300, boot_id = 'b' WHERE name = 'gamma'"
    )
    db._conn.commit()

    # batch_check_all_sessions emits one compound shell command per VM. We
    # reply with status lines for alpha (alive) + beta (alive); gamma's line
    # claims the session is gone.
    def stub_run(command: str, **kwargs: object) -> _FakeResult:
        fake_target.commands.append(command)
        if "has-session -t alpha" in command and "has-session -t beta" in command:
            return _FakeResult(
                returncode=0,
                stdout="S:alpha:0\nS:beta:0\nS:gamma:1\n",
            )
        return _FakeResult()

    fake_target.run = stub_run  # type: ignore[assignment]

    names = running_session_names(db, _StubConfig(), "vm1")
    assert names == ["alpha", "beta"]


def test_infer_vm_from_session_specs(db: Database) -> None:
    from agentworks.sessions.multi_console import infer_vm_from_session_specs

    _seed_vm(db, "vm1")
    _seed_vm(db, "vm2")
    _seed_sessions(db, ["a", "b"], workspace_name="ws-vm1")
    _seed_sessions(db, ["c"], workspace_name="ws-vm2")

    # Empty list -> None (caller falls back to prompt).
    assert infer_vm_from_session_specs(db, []) is None

    # Single VM -> resolved.
    assert infer_vm_from_session_specs(db, ["a"]) == "vm1"
    assert infer_vm_from_session_specs(db, ["a+2", "b"]) == "vm1"

    # Spans multiple VMs -> ValidationError (user must disambiguate with --vm).
    with pytest.raises(ValidationError, match="span multiple VMs"):
        infer_vm_from_session_specs(db, ["a", "c"])

    # All-unknown sessions -> None (defer error to create_console).
    assert infer_vm_from_session_specs(db, ["ghost", "fantom"]) is None


def test_create_console_fill_all_appends_alphabetically(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["gamma", "alpha", "beta"])
    create_console(
        db,
        name="everything",
        vm_name="vm1",
        session_specs=["gamma+5"],
        fill_all=True,
    )
    members = db.list_console_sessions("everything")
    assert [(m.session_name, len(m.shells)) for m in members] == [
        ("gamma", 5),
        ("alpha", 0),
        ("beta", 0),
    ]


def test_create_console_rejects_empty_without_all(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    with pytest.raises(ValidationError, match="specify at least one session"):
        create_console(db, name="empty", vm_name="vm1", session_specs=[])


def test_create_console_rejects_empty_fill_all(db: Database) -> None:
    _seed_vm(db)  # no sessions seeded
    with pytest.raises(ValidationError, match="VM 'vm1' has no sessions"):
        create_console(db, name="empty", vm_name="vm1", session_specs=[], fill_all=True)


def test_create_console_rejects_duplicate_name(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    with pytest.raises(AlreadyExistsError, match="already exists"):
        create_console(db, name="con", vm_name="vm1", session_specs=["a"])


def test_create_console_rejects_unknown_vm(db: Database) -> None:
    with pytest.raises(NotFoundError, match="not found"):
        create_console(db, name="con", vm_name="ghost", session_specs=["a"])


def test_create_console_rejects_unknown_session(db: Database) -> None:
    _seed_vm(db)
    with pytest.raises(NotFoundError, match="not found"):
        create_console(db, name="con", vm_name="vm1", session_specs=["ghost"])


def test_create_console_rejects_cross_vm_session(db: Database) -> None:
    _seed_vm(db, "vm1")
    _seed_vm(db, "vm2")
    _seed_sessions(db, ["a"], workspace_name="ws-vm2")
    with pytest.raises(ValidationError, match="is not on VM 'vm1'"):
        create_console(db, name="con", vm_name="vm1", session_specs=["a"])


def test_create_console_rejects_dup_in_args(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    with pytest.raises(ValidationError, match="listed more than once"):
        create_console(db, name="con", vm_name="vm1", session_specs=["a", "a+1"])


def test_create_console_rolls_back_on_failure(db: Database) -> None:
    """All-or-nothing: pre-existing console name is caught up front, no orphan rows."""
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    db.insert_console("con", "vm1")
    with pytest.raises(AlreadyExistsError, match="already exists"):
        create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    assert db.list_console_sessions("con") == []


# -- Orchestration: add_sessions / remove_sessions / add_shell -------------


def test_add_sessions(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    add_sessions(db, _StubConfig(), console_name="con", session_specs=["b+1", "c"])
    members = db.list_console_sessions("con")
    assert [(m.session_name, len(m.shells)) for m in members] == [
        ("a", 0),
        ("b", 1),
        ("c", 0),
    ]


def test_add_sessions_rejects_duplicate(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    with pytest.raises(AlreadyExistsError, match="already a member"):
        add_sessions(db, _StubConfig(), console_name="con", session_specs=["a"])


def test_remove_sessions(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])
    remove_sessions(db, _StubConfig(), console_name="con", session_names=["a"])
    members = db.list_console_sessions("con")
    assert [m.session_name for m in members] == ["b"]


def test_remove_sessions_rejects_non_member(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    with pytest.raises(NotFoundError, match="not a member"):
        remove_sessions(db, _StubConfig(), console_name="con", session_names=["b"])


def test_reorder_sessions_bumps_listed_to_front(db: Database) -> None:
    """Listed sessions land in their argument order at the front; unlisted
    members keep their current relative order behind them."""
    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c", "d", "e"])
    create_console(
        db, name="con", vm_name="vm1", session_specs=["a", "b", "c", "d", "e"]
    )

    reorder_sessions(
        db, _StubConfig(), console_name="con", session_names=["d", "b"]
    )

    members = db.list_console_sessions("con")
    # d, b first (argument order), then a, c, e (original relative order).
    assert [m.session_name for m in members] == ["d", "b", "a", "c", "e"]


def test_reorder_sessions_noop_when_already_in_requested_order(
    db: Database, captured_output: CapturedOutput
) -> None:
    """Asking to bump the sessions that are already at the front in that
    order is a clean no-op: no position writes, informational message."""
    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c"])

    reorder_sessions(
        db, _StubConfig(), console_name="con", session_names=["a", "b"]
    )

    members = db.list_console_sessions("con")
    assert [m.session_name for m in members] == ["a", "b", "c"]
    assert any(
        "already in the requested order" in m for m in captured_output.info
    )


def test_reorder_sessions_rejects_non_member(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    with pytest.raises(NotFoundError, match="not a member"):
        reorder_sessions(
            db, _StubConfig(), console_name="con", session_names=["b"]
        )


def test_reorder_sessions_rejects_duplicates(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])
    with pytest.raises(ValidationError, match="listed more than once"):
        reorder_sessions(
            db, _StubConfig(), console_name="con", session_names=["a", "a"]
        )


def test_reorder_sessions_rejects_missing_console(db: Database) -> None:
    _seed_vm(db)
    with pytest.raises(NotFoundError, match="console 'nope' not found"):
        reorder_sessions(
            db, _StubConfig(), console_name="nope", session_names=["a"]
        )


def test_reorder_sessions_rejects_empty_input(db: Database) -> None:
    """No-op-shaped input ('please reorder, but I gave you no sessions') is
    almost certainly a typo. Match create_console's stance on this."""
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])
    with pytest.raises(ValidationError, match="no sessions specified"):
        reorder_sessions(
            db, _StubConfig(), console_name="con", session_names=[]
        )


def test_add_shell_appends_entry(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+1"])
    add_shell(db, _StubConfig(), console_name="con", session_name="a", cwd="src", admin=True)
    member = db.get_console_session("con", "a")
    assert member is not None
    assert member.shells == [
        {"cwd": None, "admin": False},
        {"cwd": "src", "admin": True},
    ]


def test_add_shell_rejects_non_member(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    with pytest.raises(NotFoundError, match="not a member"):
        add_shell(db, _StubConfig(), console_name="con", session_name="b")


def test_add_shell_rejects_bad_cwd(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    with pytest.raises(ValidationError):
        add_shell(db, _StubConfig(), console_name="con", session_name="a", cwd="/etc")


def test_delete_console_record(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    delete_console_record(db, name="con")
    assert db.get_console("con") is None
    assert db._conn.execute("SELECT * FROM console_sessions").fetchall() == []


def test_delete_console_db_only_when_vm_unreachable(
    db: Database,
    captured_output: CapturedOutput,
) -> None:
    """delete_console removes the DB row even when the VM has no tailscale host
    (so _live_target returns None and tmux teardown is a no-op)."""
    _seed_vm(db, with_tailscale=False)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    delete_console(db, _StubConfig(), name="con", yes=True)
    assert db.get_console("con") is None


# -- describe_console / list_consoles output -------------------------------


def test_describe_console_uses_iteration_index(
    db: Database, captured_output: CapturedOutput
) -> None:
    """After a remove, members keep their position gap in the DB but describe
    renders 0..N-1 line numbers."""
    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c"])
    remove_sessions(db, _StubConfig(), console_name="con", session_names=["a"])
    describe_console(db, name="con")
    member_lines = [m for m in captured_output.info if m.lstrip().startswith("[")]
    assert member_lines == [
        "  [0] b  (no extra shells)",
        "  [1] c  (no extra shells)",
    ]


def test_list_consoles_renders_counts(
    db: Database, captured_output: CapturedOutput
) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])
    list_consoles(db)
    rows = [m for m in captured_output.info if m.startswith("con")]
    assert any("vm1" in r and r.endswith("2") for r in rows)


# -- Tmux orchestration (FakeTarget-mocked) --------------------------------


def test_attach_loop_wrapper_format() -> None:
    """Lock the wrapper shape: entry banner + forever attach loop with
    exit-status notice on session-end."""
    from agentworks.sessions.multi_console import _attach_loop_wrapper

    wrapper = _attach_loop_wrapper("backend", None)
    # Unset TMUX so console -> session nesting is allowed.
    assert "unset TMUX" in wrapper
    # Forever loop with no break/timeout, no "press enter to close" prompt.
    assert "while true" in wrapper
    assert "break" not in wrapper
    assert "Press enter" not in wrapper
    # Entry banner names the session.
    assert "Waiting for session backend to come up" in wrapper
    # Exit notice distinguishes clean vs non-zero attach status; the post-exit
    # banner tells the user we're waiting for a restart so the pane isn't silent.
    assert "Session backend exited cleanly" in wrapper
    assert "exited (status $rc)" in wrapper
    assert "Waiting for session to restart" in wrapper
    # Silent poll with 2s back-off.
    assert "sleep 2" in wrapper
    assert "sleep 1" not in wrapper

    # Socketed wrapper threads -S through both has-session and attach.
    wrapper_sock = _attach_loop_wrapper("a", "/tmp/a.sock")
    assert "tmux -S /tmp/a.sock has-session" in wrapper_sock
    assert "tmux -S /tmp/a.sock attach" in wrapper_sock


def test_attach_console_builds_initial_tmux(
    db: Database, fake_target: _FakeTarget
) -> None:
    """First attach: kill any existing session, create with a placeholder
    window, add one new-window per member in DB order, then drop the
    placeholder. Two shells on the first member -> two split-windows + tiled."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha", "beta"])
    create_console(db, name="con", vm_name="vm1", session_specs=["alpha+2", "beta"])

    # Simulate console not existing yet so build path runs.
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    # list-windows must report real windows so the placeholder gets killed.
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0, stdout="_PLACEHOLDER\nalpha\nbeta\n"
    )

    with pytest.raises(SystemExit):
        attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    cmds = fake_target.commands
    assert any("has-session -t aw-console-con" in c for c in cmds)
    assert any("kill-session -t aw-console-con" in c for c in cmds)
    assert any("new-session -d -s aw-console-con -n _PLACEHOLDER" in c for c in cmds)
    # No admin-shell window: named consoles only contain the curated sessions.
    assert not any("--admin--" in c for c in cmds)
    assert not any("admin-shell" in c for c in cmds)
    new_window_indexes = [i for i, c in enumerate(cmds) if "new-window -t aw-console-con" in c]
    assert len(new_window_indexes) == 2, cmds
    assert "alpha" in cmds[new_window_indexes[0]]
    assert "beta" in cmds[new_window_indexes[1]]
    split_cmds = [c for c in cmds if "split-window -t aw-console-con" in c]
    assert len(split_cmds) == 2, cmds  # two shells on alpha, none on beta
    assert any("select-layout -t aw-console-con:alpha tiled" in c for c in cmds)
    # Placeholder gets killed once real windows are in.
    assert any("kill-window -t aw-console-con:_PLACEHOLDER" in c for c in cmds)


def test_attach_console_placeholder_name_cannot_collide_with_session(
    db: Database, fake_target: _FakeTarget
) -> None:
    """A user-created session literally named 'placeholder' must not be
    accidentally killed when we drop the build placeholder. The placeholder
    uses '--' (forbidden by validate_name) so collisions are impossible."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["placeholder", "real"])
    create_console(db, name="con", vm_name="vm1", session_specs=["placeholder", "real"])
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0, stdout="_PLACEHOLDER\nplaceholder\nreal\n"
    )

    with pytest.raises(SystemExit):
        attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    kill_windows = [c for c in fake_target.commands if "kill-window" in c]
    # We kill exactly the build placeholder, never the user's window.
    assert any("_PLACEHOLDER" in c for c in kill_windows)
    assert not any(
        "kill-window -t aw-console-con:placeholder" in c and "_PLACEHOLDER" not in c
        for c in fake_target.commands
    )


def test_attach_console_warns_when_list_windows_fails(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """If list-windows fails (SSH drop etc.), the user gets a warning so the
    persisting placeholder isn't a silent surprise."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    create_console(db, name="con", vm_name="vm1", session_specs=["alpha"])
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=255, stderr="transport failure"
    )

    with pytest.raises(SystemExit):
        attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    assert any("could not list windows" in w for w in captured_output.warnings)
    # No kill-window for the placeholder since we couldn't confirm cleanup.
    assert not any("kill-window -t aw-console-con:_PLACEHOLDER" in c for c in fake_target.commands)


def test_create_console_with_admin_shell_persists_flag(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console(
        db,
        name="con",
        vm_name="vm1",
        session_specs=["a"],
        add_admin_shell=True,
    )
    console = db.get_console("con")
    assert console is not None
    assert console.admin_shell is True


def test_create_console_admin_shell_only_allowed(db: Database) -> None:
    """A console with admin_shell=True and no sessions is allowed (top-level shell only)."""
    _seed_vm(db)
    create_console(
        db,
        name="shell-only",
        vm_name="vm1",
        session_specs=[],
        add_admin_shell=True,
    )
    console = db.get_console("shell-only")
    assert console is not None
    assert console.admin_shell is True
    assert db.list_console_sessions("shell-only") == []


def test_attach_console_builds_admin_shell_window_without_placeholder(
    db: Database, fake_target: _FakeTarget
) -> None:
    """When admin_shell is set, window 0 is the admin-shell -- no placeholder."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    create_console(
        db,
        name="con",
        vm_name="vm1",
        session_specs=["alpha"],
        add_admin_shell=True,
    )
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)

    with pytest.raises(SystemExit):
        attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    cmds = fake_target.commands
    new_sessions = [c for c in cmds if "new-session -d -s aw-console-con" in c]
    assert len(new_sessions) == 1
    # Window 0 is the --admin-- window, running sudo su --login <admin> -- pin
    # the shape so quoting regressions in the bootstrap fail loudly.
    assert "-n --admin--" in new_sessions[0]
    assert "sudo su --login" in new_sessions[0]
    assert "admin" in new_sessions[0]  # the admin username from _seed_vm
    assert not any("_PLACEHOLDER" in c for c in cmds)
    assert not any("list-windows" in c for c in cmds)
    new_windows = [c for c in cmds if "new-window -t aw-console-con" in c]
    assert len(new_windows) == 1 and "alpha" in new_windows[0]


def test_describe_console_shows_admin_shell_state(
    db: Database, captured_output: CapturedOutput
) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console(db, name="plain", vm_name="vm1", session_specs=["a"])
    create_console(
        db, name="with-shell", vm_name="vm1", session_specs=["a"], add_admin_shell=True
    )

    captured_output.info.clear()
    describe_console(db, name="plain")
    assert any("Admin shell: no" in m for m in captured_output.info)

    captured_output.info.clear()
    describe_console(db, name="with-shell")
    assert any("Admin shell: yes" in m for m in captured_output.info)


def test_attach_console_keeps_placeholder_when_all_members_fail(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """If every new-window call fails, the placeholder stays so the tmux
    session survives for the user to investigate."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    create_console(db, name="con", vm_name="vm1", session_specs=["alpha"])
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    # new-window for alpha fails -> only placeholder ends up in list-windows.
    fake_target.responses["new-window -t aw-console-con"] = _FakeResult(
        returncode=1, stderr="simulated failure"
    )
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0, stdout="_PLACEHOLDER\n"
    )

    with pytest.raises(SystemExit):
        attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    assert not any("kill-window -t aw-console-con:_PLACEHOLDER" in c for c in fake_target.commands)
    assert any("placeholder kept" in w for w in captured_output.warnings)


def test_attach_console_announces_build_path(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """First attach prints a 'Building...' status."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0, stdout="_PLACEHOLDER\na\n"
    )

    captured_output.info.clear()
    with pytest.raises(SystemExit):
        attach_console(db, _StubConfig(), name="con", allow_nesting=True)
    assert any("Building console 'con' on first attach" in m for m in captured_output.info)


def test_attach_console_announces_attach_path(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """Subsequent attach (tmux already running) prints an 'Attaching...' status,
    not a build status."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)

    captured_output.info.clear()
    with pytest.raises(SystemExit):
        attach_console(db, _StubConfig(), name="con", allow_nesting=True)
    assert any("Attaching to running console 'con'" in m for m in captured_output.info)
    assert not any("Building" in m or "Rebuilding" in m for m in captured_output.info)


def test_attach_console_announces_recreate_path(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """--recreate against an alive console prints a 'Rebuilding...' status."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0, stdout="_PLACEHOLDER\na\n"
    )

    captured_output.info.clear()
    with pytest.raises(SystemExit):
        attach_console(db, _StubConfig(), name="con", recreate=True, allow_nesting=True)
    assert any("Rebuilding console 'con' (--recreate)" in m for m in captured_output.info)


def test_attach_console_reuses_existing_tmux(
    db: Database, fake_target: _FakeTarget
) -> None:
    """Subsequent attach: console exists -> no rebuild commands fire."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    create_console(db, name="con", vm_name="vm1", session_specs=["alpha"])
    # Console exists.
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)

    with pytest.raises(SystemExit):
        attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    cmds = fake_target.commands
    assert not any("new-session" in c for c in cmds)
    assert not any("new-window" in c for c in cmds)


def test_attach_console_recreate_rebuilds_even_if_alive(
    db: Database, fake_target: _FakeTarget
) -> None:
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    create_console(db, name="con", vm_name="vm1", session_specs=["alpha"])
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0, stdout="_PLACEHOLDER\nalpha\n"
    )

    with pytest.raises(SystemExit):
        attach_console(db, _StubConfig(), name="con", recreate=True, allow_nesting=True)

    cmds = fake_target.commands
    assert any("kill-session -t aw-console-con" in c for c in cmds)
    assert any("new-session -d -s aw-console-con" in c for c in cmds)


def test_attach_console_iterates_in_position_order(
    db: Database, fake_target: _FakeTarget
) -> None:
    """Even when DB positions have gaps (after a remove), iteration uses
    ORDER BY position ASC, not insertion order or row order."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b", "c", "d"])
    # Force live-sync to short-circuit so the mutations below don't issue
    # spurious tmux commands that pollute the attach assertion.
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c"])
    remove_sessions(db, _StubConfig(), console_name="con", session_names=["b"])
    add_sessions(db, _StubConfig(), console_name="con", session_specs=["d"])
    # positions are now a=0, c=2, d=3.

    fake_target.commands.clear()
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0, stdout="_PLACEHOLDER\na\nc\nd\n"
    )
    with pytest.raises(SystemExit):
        attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    new_windows = [c for c in fake_target.commands if "new-window -t aw-console-con" in c]
    names = [c.split("-n ")[1].split()[0] for c in new_windows]
    assert names == ["a", "c", "d"]


def test_attach_console_skips_missing_session_with_warning(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """Cascade should keep this from happening normally, but if a member row
    survives without its session, we warn and continue."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha", "ghost"])
    create_console(db, name="con", vm_name="vm1", session_specs=["alpha", "ghost"])
    # Delete the session directly so console_sessions still has a 'ghost' row.
    # ON DELETE CASCADE would normally clear it; bypass via raw SQL to simulate
    # an inconsistency.
    db._conn.execute("PRAGMA foreign_keys = OFF")
    db._conn.execute("DELETE FROM sessions WHERE name = 'ghost'")
    db._conn.execute("PRAGMA foreign_keys = ON")
    db._conn.commit()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0, stdout="_PLACEHOLDER\nalpha\n"
    )

    with pytest.raises(SystemExit):
        attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    assert any("ghost" in w and "no longer exists" in w for w in captured_output.warnings)
    new_windows = [c for c in fake_target.commands if "new-window -t aw-console-con" in c]
    # Only the surviving session gets a window.
    assert len(new_windows) == 1
    assert "alpha" in new_windows[0]


def test_attach_console_skips_window_when_agent_missing(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """If an agent-mode session's agent row is gone (FK violation under
    foreign_keys=OFF, or post-migration inconsistency), _session_linux_user
    raises NotFoundError. _add_session_window catches it, warns, and continues
    instead of aborting the whole console build."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    # Create an agent-mode session pointing at an agent row, then delete the
    # agent row directly to simulate the inconsistency.
    db._conn.execute(
        "INSERT INTO agents (name, vm_name, linux_user) VALUES ('bot', 'vm1', 'bot-user')"
    )
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, agent_name, socket_path) "
        "VALUES ('s', 'ws-vm1', 'default', 'agent', 'bot', '/tmp/s.sock')"
    )
    db._conn.commit()
    create_console(db, name="con", vm_name="vm1", session_specs=["s+1"])
    db._conn.execute("PRAGMA foreign_keys = OFF")
    db._conn.execute("DELETE FROM agents WHERE name = 'bot'")
    db._conn.execute("PRAGMA foreign_keys = ON")
    db._conn.commit()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0, stdout="_PLACEHOLDER\ns\n"
    )

    with pytest.raises(SystemExit):
        attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    assert any("agent for session 's' is missing" in w for w in captured_output.warnings)
    # The window itself was created (new-window happened before the agent check);
    # only the split-window calls for the shell panes are skipped.
    new_windows = [c for c in fake_target.commands if "new-window -t aw-console-con" in c]
    assert len(new_windows) == 1
    splits = [c for c in fake_target.commands if "split-window -t aw-console-con:s" in c]
    assert splits == []


def test_add_session_live_sync_skipped_when_console_absent(
    db: Database, fake_target: _FakeTarget
) -> None:
    """If the console's tmux session isn't alive, no new-window command runs."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)
    add_sessions(db, _StubConfig(), console_name="con", session_specs=["b"])

    assert not any("new-window" in c for c in fake_target.commands)


def test_add_session_live_sync_adds_window_when_alive(
    db: Database, fake_target: _FakeTarget
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    add_sessions(db, _StubConfig(), console_name="con", session_specs=["b+1"])

    new_window = [c for c in fake_target.commands if "new-window -t aw-console-con" in c]
    assert len(new_window) == 1
    assert "-n b" in new_window[0]
    splits = [c for c in fake_target.commands if "split-window -t aw-console-con:b" in c]
    assert len(splits) == 1


def test_remove_session_live_sync_kills_window(
    db: Database, fake_target: _FakeTarget
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    remove_sessions(db, _StubConfig(), console_name="con", session_names=["b"])

    kill_windows = [c for c in fake_target.commands if "kill-window -t aw-console-con:b" in c]
    assert len(kill_windows) == 1


def test_reorder_sessions_live_sync_swaps_windows_no_admin_shell(
    db: Database, fake_target: _FakeTarget
) -> None:
    """With no admin-shell window, the desired session order maps onto
    every live window index. The helper issues one swap-window per
    out-of-place slot, tracking indices in memory so the second iteration
    sees the new layout without another list-windows call."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b", "c"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0, stdout="0|a\n1|b\n2|c\n"
    )

    reorder_sessions(
        db, _StubConfig(), console_name="con", session_names=["c", "a"]
    )

    # Desired order: [c, a, b]. Starting layout [a, b, c]:
    # - i=0 wants c at idx 0; c is at 2 -> swap 2 <-> 0 -> [c, b, a]
    # - i=1 wants a at idx 1; a is at 2 (after the swap, our tracker knows
    #   this without re-listing) -> swap 2 <-> 1 -> [c, a, b]
    swaps = [c for c in fake_target.commands if "swap-window" in c]
    assert swaps == [
        "tmux swap-window -s aw-console-con:2 -t aw-console-con:0",
        "tmux swap-window -s aw-console-con:2 -t aw-console-con:1",
    ]


def test_reorder_sessions_live_sync_holds_admin_shell_fixed(
    db: Database, fake_target: _FakeTarget
) -> None:
    """Permutable slots are derived positively from the session set, so the
    --admin-- window (whose name is not in the desired list) is excluded."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b", "c"])
    # Build the console with admin_shell=True so the live layout will have
    # '--admin--' at index 0 and sessions at 1+.
    db.insert_console("con", "vm1", admin_shell=True)
    for n in ["a", "b", "c"]:
        db.add_console_session("con", n, [])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0, stdout="0|--admin--\n1|a\n2|b\n3|c\n"
    )

    reorder_sessions(
        db, _StubConfig(), console_name="con", session_names=["c"]
    )

    # Desired order: [c, a, b]. Session slots = [1, 2, 3] ('--admin--' at
    # 0 is not in the session set, so it isn't a slot). Starting [--admin--,
    # a, b, c]:
    #   - i=0 wants c at idx 1; c is at 3 -> swap 3<->1 -> [..., c, b, a]
    #   - i=1 wants a at idx 2; a is now at 3 -> swap 3<->2 -> [..., c, a, b]
    # The second swap is the unavoidable cost of placing one displaced
    # window: bumping c to the front pushed a out of position.
    swaps = [c for c in fake_target.commands if "swap-window" in c]
    assert swaps == [
        "tmux swap-window -s aw-console-con:3 -t aw-console-con:1",
        "tmux swap-window -s aw-console-con:3 -t aw-console-con:2",
    ]
    # --admin-- window itself was never moved.
    assert not any(
        "swap-window" in c and "--admin--" in c for c in fake_target.commands
    )


def test_reorder_sessions_live_sync_ignores_stray_window(
    db: Database, fake_target: _FakeTarget
) -> None:
    """A window with no matching session row (operator-created via raw
    `tmux new-window`, leftover from a rename, etc.) is not a permutable
    slot. The reorder operates only on windows whose names are in the
    session set; the stray stays put."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b", "c"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    # Operator opened an extra window named 'scratch' at index 2; sessions
    # live at 0, 1, 3.
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0, stdout="0|a\n1|b\n2|scratch\n3|c\n"
    )

    reorder_sessions(
        db, _StubConfig(), console_name="con", session_names=["c"]
    )

    # Session slots = [0, 1, 3] (scratch at 2 is excluded). Desired order
    # [c, a, b]:
    #   - i=0 wants c at slot 0 (idx 0); c is at 3 -> swap 3<->0 -> [c, b, scratch, a]
    #   - i=1 wants a at slot 1 (idx 1); a is now at 3 -> swap 3<->1 -> [c, a, scratch, b]
    # scratch is never touched; it remains at index 2.
    swaps = [c for c in fake_target.commands if "swap-window" in c]
    assert swaps == [
        "tmux swap-window -s aw-console-con:3 -t aw-console-con:0",
        "tmux swap-window -s aw-console-con:3 -t aw-console-con:1",
    ]
    assert not any(
        "swap-window" in c and "scratch" in c for c in fake_target.commands
    )


def test_reorder_sessions_live_sync_skipped_when_console_absent(
    db: Database, fake_target: _FakeTarget
) -> None:
    """If the console's tmux session isn't alive, no swap-window calls run.
    DB still updates."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)

    reorder_sessions(
        db, _StubConfig(), console_name="con", session_names=["b"]
    )

    assert not any("swap-window" in c for c in fake_target.commands)
    # DB still reflects the new order.
    members = db.list_console_sessions("con")
    assert [m.session_name for m in members] == ["b", "a"]


def test_reorder_sessions_live_sync_compacts_when_window_missing(
    db: Database, fake_target: _FakeTarget
) -> None:
    """If the operator killed a session window manually, the surviving
    windows compact toward the front instead of getting stranded at
    later slots. Without this, desired = [c, a, b] with live = [a, b]
    (c missing) would land 'a' at slot 1 and produce [b, a] -- wrong."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b", "c"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    # 'c' window is missing -- operator hit Ctrl-B & by mistake, or it
    # exited before the wrapper-loop could catch the restart.
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0, stdout="0|a\n1|b\n"
    )

    reorder_sessions(
        db, _StubConfig(), console_name="con", session_names=["c"]
    )

    # Desired order: [c, a, b]. present_desired = [a, b] (c filtered out).
    # session_slots = [0, 1]. Map a->0 (already there, skip), b->1 (already
    # there, skip). No swaps needed; layout stays [a, b].
    swaps = [c for c in fake_target.commands if "swap-window" in c]
    assert swaps == []
    # DB still reflects the new order (DB doesn't care about tmux state).
    members = db.list_console_sessions("con")
    assert [m.session_name for m in members] == ["c", "a", "b"]


def test_reorder_sessions_live_sync_bails_on_duplicate_window_names(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """If two windows share a name that's in the session set, we can't
    disambiguate which one to swap. Warn with a --recreate hint and skip
    tmux work; DB is already updated."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    # Two windows both named 'a' (operator renamed window 2 by accident).
    fake_target.responses["list-windows -t aw-console-con"] = _FakeResult(
        returncode=0, stdout="0|a\n1|b\n2|a\n"
    )

    reorder_sessions(
        db, _StubConfig(), console_name="con", session_names=["b"]
    )

    swaps = [c for c in fake_target.commands if "swap-window" in c]
    assert swaps == []
    assert any(
        "duplicate window name" in w and "--recreate" in w
        for w in captured_output.warnings
    )
    members = db.list_console_sessions("con")
    assert [m.session_name for m in members] == ["b", "a"]


def test_list_consoles_for_session_returns_members(db: Database) -> None:
    """Snapshot of which consoles list a given session as a member, before
    the FK cascade fires on session delete."""
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    db.insert_console("alpha", "vm1")
    db.insert_console("beta", "vm1")
    db.insert_console("gamma", "vm1")
    db.add_console_session("alpha", "a", [])
    db.add_console_session("alpha", "b", [])
    db.add_console_session("beta", "a", [])
    # gamma has no members.

    assert [c.name for c in db.list_consoles_for_session("a")] == ["alpha", "beta"]
    assert [c.name for c in db.list_consoles_for_session("b")] == ["alpha"]
    assert db.list_consoles_for_session("nope") == []


def test_kill_session_windows_kills_live_only(
    db: Database, fake_target: _FakeTarget
) -> None:
    """Pairs are grouped by console; kill-window runs only where the console's
    tmux session is alive."""
    from agentworks.sessions.multi_console import kill_session_windows

    fake_target.responses["has-session -t aw-console-alive"] = _FakeResult(returncode=0)
    fake_target.responses["has-session -t aw-console-dead"] = _FakeResult(returncode=1)

    kill_session_windows(
        fake_target,  # type: ignore[arg-type]
        pairs=[("alive", "s"), ("dead", "s")],
    )

    kill_windows = [c for c in fake_target.commands if "kill-window" in c]
    assert kill_windows == ["tmux kill-window -t aw-console-alive:s"]


def test_kill_session_windows_empty_is_noop(
    fake_target: _FakeTarget,
) -> None:
    """No pairs -> no SSH probes, no kill-window calls."""
    from agentworks.sessions.multi_console import kill_session_windows

    kill_session_windows(
        fake_target,  # type: ignore[arg-type]
        pairs=[],
    )
    assert fake_target.commands == []


def test_kill_session_windows_groups_by_console(
    fake_target: _FakeTarget,
) -> None:
    """Multiple sessions in one console -> single has-session probe, one
    kill-window per session."""
    from agentworks.sessions.multi_console import kill_session_windows

    fake_target.responses["has-session -t aw-console-c"] = _FakeResult(returncode=0)

    kill_session_windows(
        fake_target,  # type: ignore[arg-type]
        pairs=[("c", "a"), ("c", "b"), ("c", "d")],
    )

    has_session = [c for c in fake_target.commands if "has-session" in c]
    kill_windows = [c for c in fake_target.commands if "kill-window" in c]
    assert len(has_session) == 1
    assert kill_windows == [
        "tmux kill-window -t aw-console-c:a",
        "tmux kill-window -t aw-console-c:b",
        "tmux kill-window -t aw-console-c:d",
    ]


def test_kill_session_windows_transport_failure_warns(
    fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """A raised non-Agentworks exception (transport surprise) is swallowed
    with a warning that names the affected consoles."""
    from agentworks.sessions.multi_console import kill_session_windows

    def boom(command: str, **kwargs: object) -> _FakeResult:
        raise RuntimeError("ssh blew up")

    fake_target.run = boom  # type: ignore[assignment]

    kill_session_windows(
        fake_target,  # type: ignore[arg-type]
        pairs=[("alpha", "s"), ("beta", "s")],
    )

    assert any(
        "live console window cleanup failed" in w
        and "alpha" in w
        and "beta" in w
        for w in captured_output.warnings
    )


def test_kill_session_windows_agentworks_error_propagates(
    fake_target: _FakeTarget,
) -> None:
    """AgentworksError is not swallowed by the helper -- callers see it."""
    from agentworks.sessions.multi_console import kill_session_windows

    def boom(command: str, **kwargs: object) -> _FakeResult:
        raise ConnectivityError("vm unreachable", entity_kind="vm", entity_name="vm1")

    fake_target.run = boom  # type: ignore[assignment]

    with pytest.raises(ConnectivityError):
        kill_session_windows(
            fake_target,  # type: ignore[arg-type]
            pairs=[("alpha", "s")],
        )


# -- Integration: delete paths invoke kill_session_windows correctly -------


def test_delete_session_kills_console_windows(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """``manager.delete_session`` must snapshot console memberships *before*
    the DB delete (FK cascade clears the join) and then dispatch to
    ``kill_session_windows`` with one pair per member console."""
    from agentworks.db import PID_STOPPED
    from agentworks.sessions import manager as manager_mod

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["s", "other"])
    # Mark 's' STOPPED so check_session_status short-circuits with no SSH.
    db.update_session_pid("s", PID_STOPPED)
    create_console(db, name="alpha", vm_name="vm1", session_specs=["s", "other"])
    create_console(db, name="beta", vm_name="vm1", session_specs=["s"])
    create_console(db, name="gamma", vm_name="vm1", session_specs=["other"])

    monkeypatch.setattr(manager_mod, "_regenerate_tmuxinator", lambda *a, **k: None)

    captured: list[list[tuple[str, str]]] = []

    def spy(target: object, *, pairs: list[tuple[str, str]]) -> None:
        captured.append(pairs)

    monkeypatch.setattr(
        "agentworks.sessions.multi_console.kill_session_windows", spy
    )

    manager_mod.delete_session(db, _StubConfig(), name="s", yes=True)

    # The DB row is gone, and only the consoles that listed 's' get kills.
    assert db.get_session("s") is None
    assert len(captured) == 1
    assert sorted(captured[0]) == [("alpha", "s"), ("beta", "s")]


def test_delete_session_skips_kill_when_no_member_consoles(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """No console membership -> no kill_session_windows call. Guards against
    a future regression that unconditionally invokes the helper."""
    from agentworks.db import PID_STOPPED
    from agentworks.sessions import manager as manager_mod

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["lonely"])
    db.update_session_pid("lonely", PID_STOPPED)

    monkeypatch.setattr(manager_mod, "_regenerate_tmuxinator", lambda *a, **k: None)

    called = False

    def spy(target: object, *, pairs: list[tuple[str, str]]) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(
        "agentworks.sessions.multi_console.kill_session_windows", spy
    )

    manager_mod.delete_session(db, _StubConfig(), name="lonely", yes=True)

    assert called is False


def test_delete_workspace_kills_console_windows(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
    tmp_path: Path,
) -> None:
    """``delete_workspace --force`` must clean up windows for every deleted
    session across every console that listed them."""
    from agentworks.db import PID_STOPPED
    from agentworks.workspaces import manager as ws_manager

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["s1", "s2"])
    db.update_session_pid("s1", PID_STOPPED)
    db.update_session_pid("s2", PID_STOPPED)
    create_console(db, name="con1", vm_name="vm1", session_specs=["s1", "s2"])
    create_console(db, name="con2", vm_name="vm1", session_specs=["s1"])

    # delete_workspace shells out to delete_vm_workspace + tmuxinator regen;
    # stub both so we don't need a live VM filesystem.
    monkeypatch.setattr(
        "agentworks.workspaces.backends.vm.delete_vm_workspace",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "agentworks.agents.manager.revoke_workspace_grants",
        lambda *a, **k: None,
    )

    captured: list[list[tuple[str, str]]] = []

    def spy(target: object, *, pairs: list[tuple[str, str]]) -> None:
        captured.append(pairs)

    monkeypatch.setattr(
        "agentworks.sessions.multi_console.kill_session_windows", spy
    )

    # delete_workspace touches config.paths.vscode_workspaces to remove the
    # .code-workspace file; point it at a tmp dir so the unlink is a no-op.
    cfg = _StubConfig()
    cfg.paths = type("P", (), {"vscode_workspaces": tmp_path})()  # type: ignore[attr-defined]
    ws_manager.delete_workspace(db, cfg, "ws-vm1", force=True, yes=True)

    assert db.get_workspace("ws-vm1") is None
    assert db.get_session("s1") is None
    assert db.get_session("s2") is None
    assert len(captured) == 1
    assert sorted(captured[0]) == [
        ("con1", "s1"),
        ("con1", "s2"),
        ("con2", "s1"),
    ]


def test_delete_agent_kills_console_windows(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """``delete_agent --force`` runs the same cleanup over its agent's
    sessions."""
    from agentworks.agents import manager as agent_manager
    from agentworks.db import PID_STOPPED

    _seed_vm(db, with_tailscale=True)
    db._conn.execute(
        "INSERT INTO agents (name, vm_name, linux_user) VALUES ('bot', 'vm1', 'bot-user')"
    )
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, agent_name, socket_path, pid) "
        "VALUES ('s1', 'ws-vm1', 'default', 'agent', 'bot', '/tmp/s1.sock', ?), "
        "('s2', 'ws-vm1', 'default', 'agent', 'bot', '/tmp/s2.sock', ?)",
        (PID_STOPPED, PID_STOPPED),
    )
    db._conn.commit()
    create_console(db, name="con", vm_name="vm1", session_specs=["s1", "s2"])

    monkeypatch.setattr(
        "agentworks.agents.manager._remove_from_workspace_group",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "agentworks.agents.manager._delete_agent_on_vm",
        lambda *a, **k: None,
    )

    captured: list[list[tuple[str, str]]] = []

    def spy(target: object, *, pairs: list[tuple[str, str]]) -> None:
        captured.append(pairs)

    monkeypatch.setattr(
        "agentworks.sessions.multi_console.kill_session_windows", spy
    )

    agent_manager.delete_agent(db, _StubConfig(), name="bot", force=True, yes=True)

    assert db.get_agent("bot") is None
    assert len(captured) == 1
    assert sorted(captured[0]) == [("con", "s1"), ("con", "s2")]


def test_add_shell_live_sync_splits_pane_and_tiles(
    db: Database, fake_target: _FakeTarget
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    add_shell(db, _StubConfig(), console_name="con", session_name="a", cwd="src", admin=True)

    splits = [c for c in fake_target.commands if "split-window -t aw-console-con:a" in c]
    assert len(splits) == 1
    # Pane cwd reflects the relative path joined under the workspace root.
    assert "/home/me/vm1/src" in splits[0]
    layouts = [c for c in fake_target.commands if "select-layout -t aw-console-con:a tiled" in c]
    assert len(layouts) == 1


def test_delete_console_live_kills_tmux_session(
    db: Database, fake_target: _FakeTarget
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    delete_console(db, _StubConfig(), name="con", yes=True)

    kill_session = [c for c in fake_target.commands if "kill-session -t aw-console-con" in c]
    assert len(kill_session) == 1
    assert db.get_console("con") is None


def test_split_shell_pane_agent_branch_uses_sudo(
    db: Database, fake_target: _FakeTarget
) -> None:
    """Agent-user shells bootstrap via `sudo --login -u <user> bash -c '...'`;
    admin-user shells skip the sudo wrapper since the console is already admin."""
    # Build an agent + agent-mode session manually so we can exercise the
    # session_user != admin_user branch of _split_shell_pane.
    _seed_vm(db, with_tailscale=True)
    db._conn.execute(
        "INSERT INTO agents (name, vm_name, linux_user) VALUES ('bot', 'vm1', 'bot-user')",
    )
    db._conn.execute(
        "INSERT INTO sessions (name, workspace_name, template, mode, agent_name, socket_path) "
        "VALUES ('s', 'ws-vm1', 'default', 'agent', 'bot', '/tmp/s.sock')",
    )
    db._conn.commit()
    create_console(db, name="con", vm_name="vm1", session_specs=["s"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    add_shell(db, _StubConfig(), console_name="con", session_name="s")  # agent, workspace root

    splits = [c for c in fake_target.commands if "split-window -t aw-console-con:s" in c]
    assert len(splits) == 1
    assert "sudo --login -u bot-user" in splits[0]
    assert 'exec "$SHELL" -l' in splits[0]


def test_split_shell_pane_admin_branch_no_sudo(
    db: Database, fake_target: _FakeTarget
) -> None:
    """Admin shell on an admin-mode session: no sudo, just cd + login shell."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    add_shell(db, _StubConfig(), console_name="con", session_name="a")

    splits = [c for c in fake_target.commands if "split-window -t aw-console-con:a" in c]
    assert len(splits) == 1
    assert "sudo --login" not in splits[0]
    assert 'exec "$SHELL" -l' in splits[0]


# -- Pane tagging ----------------------------------------------------------


def test_split_shell_pane_tags_new_pane_with_config_index(
    db: Database, fake_target: _FakeTarget
) -> None:
    """After split-window emits the new pane id, _split_shell_pane sets
    @agentworks-shell-index so restore-session can identify which configured
    shell a given live pane corresponds to."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    # Simulate tmux split-window -P emitting a pane id.
    fake_target.responses["split-window -t aw-console-con:a"] = _FakeResult(
        stdout="%7\n"
    )

    add_shell(db, _StubConfig(), console_name="con", session_name="a")

    set_options = [
        c for c in fake_target.commands
        if "set-option -p" in c and SHELL_INDEX_OPTION in c
    ]
    assert len(set_options) == 1
    # The first shell added is config index 0 (cs.shells was empty).
    assert f"-t %7 {SHELL_INDEX_OPTION} 0" in set_options[0]


def test_split_shell_pane_warns_when_split_returns_no_pane_id(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """If split-window's stdout is empty (older tmux / weird transport), the
    tag step is skipped and the operator gets a warning that the pane is
    untagged. The pane is still live; restore-session just won't be able to
    repair this window without `attach --recreate`."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    # Default _FakeResult has empty stdout, so no pane_id to tag.

    add_shell(db, _StubConfig(), console_name="con", session_name="a")

    set_options = [c for c in fake_target.commands if "set-option -p" in c]
    assert set_options == []
    # The recovery hint includes the actual console name so it can be
    # copy/pasted verbatim.
    assert any(
        "couldn't capture its id" in w
        and "untagged" in w
        and "attach con --recreate" in w
        for w in captured_output.warnings
    )


def test_split_shell_pane_warns_when_set_option_fails(
    db: Database, fake_target: _FakeTarget, captured_output: CapturedOutput
) -> None:
    """If tmux split-window succeeded and emitted a pane id but the subsequent
    set-option fails (tmux version/flags mismatch, target gone, etc.), the
    pane is live but untagged. _split_shell_pane must surface this so the
    operator gets a loud signal instead of restore-session breaking later."""
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])

    fake_target.commands.clear()
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=0)
    fake_target.responses["split-window -t aw-console-con:a"] = _FakeResult(
        stdout="%7\n"
    )
    # set-option fails non-zero.
    fake_target.responses["set-option -p"] = _FakeResult(
        returncode=1, stderr="bad target"
    )

    add_shell(db, _StubConfig(), console_name="con", session_name="a")

    assert any(
        "tagging failed" in w and "attach con --recreate" in w
        for w in captured_output.warnings
    )


