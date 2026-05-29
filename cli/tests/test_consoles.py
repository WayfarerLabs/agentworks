"""Tests for named consoles (DB + orchestration)."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import pytest

from agentworks import output
from agentworks.db import ConsoleRow, Database, _parse_shells
from agentworks.sessions.multi_console import (
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
    tmux_session_name,
)

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput


# -- Helpers ---------------------------------------------------------------


def _seed_vm(db: Database, vm_name: str = "vm1", *, with_tailscale: bool = False) -> None:
    """Insert a VM and a workspace. No tailscale host -> live-sync skips."""
    db._conn.execute(
        "INSERT INTO vms (name, platform, admin_username) VALUES (?, 'wsl', 'admin')",
        (vm_name,),
    )
    if with_tailscale:
        db._conn.execute(
            "UPDATE vms SET tailscale_host = ? WHERE name = ?",
            (f"100.64.0.{hash(vm_name) % 250}", vm_name),
        )
    db._conn.execute(
        "INSERT INTO workspaces (name, type, vm_name, workspace_path) "
        "VALUES (?, 'vm', ?, ?)",
        (f"ws-{vm_name}", vm_name, f"/home/me/{vm_name}"),
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


class _StubConfig:
    """A no-op Config stand-in.

    Tests that don't install the ``fake_target`` fixture also use VMs seeded
    with ``with_tailscale=False`` so ``_live_target`` returns None up front
    and the SSH layer is never entered. If you set ``with_tailscale=True``
    without monkey-patching ``admin_exec_target`` you will hit an
    AttributeError on this stub -- prefer the ``fake_target`` fixture.
    """


# -- parse_session_spec ----------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("foo", SessionSpec(name="foo", shells=0)),
        ("foo+0", SessionSpec(name="foo", shells=0)),
        ("foo+3", SessionSpec(name="foo", shells=3)),
        ("a-b_c+12", SessionSpec(name="a-b_c", shells=12)),
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
    ],
)
def test_parse_session_spec_rejects_bad_input(bad: str) -> None:
    with pytest.raises(output.ValidationError):
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
    with pytest.raises(output.ValidationError):
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
    with pytest.raises(output.ConsoleError, match="specify at least one session"):
        create_console(db, name="empty", vm_name="vm1", session_specs=[])


def test_create_console_rejects_empty_fill_all(db: Database) -> None:
    _seed_vm(db)  # no sessions seeded
    with pytest.raises(output.ConsoleError, match="VM 'vm1' has no sessions"):
        create_console(db, name="empty", vm_name="vm1", session_specs=[], fill_all=True)


def test_create_console_rejects_duplicate_name(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    with pytest.raises(output.ConsoleError, match="already exists"):
        create_console(db, name="con", vm_name="vm1", session_specs=["a"])


def test_create_console_rejects_unknown_vm(db: Database) -> None:
    with pytest.raises(output.VMError, match="not found"):
        create_console(db, name="con", vm_name="ghost", session_specs=["a"])


def test_create_console_rejects_unknown_session(db: Database) -> None:
    _seed_vm(db)
    with pytest.raises(output.SessionError, match="not found"):
        create_console(db, name="con", vm_name="vm1", session_specs=["ghost"])


def test_create_console_rejects_cross_vm_session(db: Database) -> None:
    _seed_vm(db, "vm1")
    _seed_vm(db, "vm2")
    _seed_sessions(db, ["a"], workspace_name="ws-vm2")
    with pytest.raises(output.ConsoleError, match="is not on VM 'vm1'"):
        create_console(db, name="con", vm_name="vm1", session_specs=["a"])


def test_create_console_rejects_dup_in_args(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    with pytest.raises(output.ConsoleError, match="listed more than once"):
        create_console(db, name="con", vm_name="vm1", session_specs=["a", "a+1"])


def test_create_console_rolls_back_on_failure(db: Database) -> None:
    """All-or-nothing: pre-existing console name is caught up front, no orphan rows."""
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    db.insert_console("con", "vm1")
    with pytest.raises(output.ConsoleError, match="already exists"):
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
    with pytest.raises(output.ConsoleError, match="already a member"):
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
    with pytest.raises(output.ConsoleError, match="not a member"):
        remove_sessions(db, _StubConfig(), console_name="con", session_names=["b"])


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
    with pytest.raises(output.ConsoleError, match="not a member"):
        add_shell(db, _StubConfig(), console_name="con", session_name="b")


def test_add_shell_rejects_bad_cwd(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    with pytest.raises(output.ValidationError):
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


class _FakeResult:
    """Minimal stand-in for ssh.SSHResult."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class _FakeTarget:
    """Captures the commands run against it. Supports a per-test override map
    that lets us simulate (e.g.) `has-session` returning nonzero on first probe.
    """

    def __init__(self, responses: dict[str, _FakeResult] | None = None) -> None:
        self.commands: list[str] = []
        # Substring -> response. First matching substring wins; default = ok.
        self.responses = responses or {}

    def run(self, command: str, **kwargs: object) -> _FakeResult:
        self.commands.append(command)
        for needle, response in self.responses.items():
            if needle in command:
                return response
        return _FakeResult()


@pytest.fixture
def fake_target(monkeypatch: pytest.MonkeyPatch) -> _FakeTarget:
    """Install a FakeTarget for the SSH layer and stub VM-running checks."""
    target = _FakeTarget()
    monkeypatch.setattr(
        "agentworks.ssh.admin_exec_target",
        lambda vm, config, **kwargs: target,
    )
    monkeypatch.setattr(
        "agentworks.workspaces.manager._ensure_vm_running",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "agentworks.ssh.interactive",
        lambda target, command: 0,
    )
    return target


def test_attach_loop_wrapper_format() -> None:
    """Lock the wrapper shape so issue #51's retry budget can't silently regress."""
    from agentworks.sessions.multi_console import _attach_loop_wrapper

    wrapper = _attach_loop_wrapper("backend", None)
    # Retry-budget keywords -- if the retry loop is removed, these fail.
    assert "attempts=0" in wrapper
    assert "attempts + 1" in wrapper
    assert "-ge 20" in wrapper
    # Unset TMUX so console -> session nesting is allowed.
    assert "unset TMUX" in wrapper
    # Raw (not shell-quoted) name in the human-facing echo.
    assert "echo 'Session backend has ended" in wrapper

    # Socketed wrapper threads -S through both has-session and attach.
    wrapper_sock = _attach_loop_wrapper("a", "/tmp/a.sock")
    assert "tmux -S /tmp/a.sock has-session" in wrapper_sock
    assert "tmux -S /tmp/a.sock attach" in wrapper_sock


def test_attach_console_builds_initial_tmux(
    db: Database, fake_target: _FakeTarget
) -> None:
    """First attach: kill any existing session, create with admin-shell window,
    then one new-window per member in DB order. Two shells -> two split-windows
    + a tiled select-layout."""
    from agentworks.sessions.multi_console import attach_console

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha", "beta"])
    create_console(db, name="con", vm_name="vm1", session_specs=["alpha+2", "beta"])

    # Simulate console not existing yet so build path runs.
    fake_target.responses["has-session -t aw-console-con"] = _FakeResult(returncode=1)

    with pytest.raises(SystemExit):
        attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    cmds = fake_target.commands
    # Sequence: existence probe, kill-session (rebuild), new-session, set
    # admin window, new-window alpha, two splits, tiled, new-window beta.
    assert any("has-session -t aw-console-con" in c for c in cmds)
    assert any("kill-session -t aw-console-con" in c for c in cmds)
    assert any("new-session -d -s aw-console-con -n admin-shell" in c for c in cmds)
    new_window_indexes = [i for i, c in enumerate(cmds) if "new-window -t aw-console-con" in c]
    assert len(new_window_indexes) == 2, cmds
    assert "alpha" in cmds[new_window_indexes[0]]
    assert "beta" in cmds[new_window_indexes[1]]
    split_cmds = [c for c in cmds if "split-window -t aw-console-con" in c]
    assert len(split_cmds) == 2, cmds  # two shells on alpha, none on beta
    assert any("select-layout -t aw-console-con:alpha tiled" in c for c in cmds)


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

    with pytest.raises(SystemExit):
        attach_console(db, _StubConfig(), name="con", allow_nesting=True)

    assert any("ghost" in w and "no longer exists" in w for w in captured_output.warnings)
    new_windows = [c for c in fake_target.commands if "new-window -t aw-console-con" in c]
    # Only the surviving session gets a window.
    assert len(new_windows) == 1
    assert "alpha" in new_windows[0]


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
