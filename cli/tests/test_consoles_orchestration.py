"""Tests for the manager-level console orchestration functions.

Split out of `test_consoles.py` (see `.claude/rules/code-style.md` on file-size
targets). Covers `create_console`, `add_sessions`/`remove_sessions`/
`reorder_sessions`/`add_shell`, `delete_console_record`/`delete_console` (the
DB-only path), and the `describe_console`/`list_consoles` output rendering.
Shared seed helpers and stub Config classes live in
`tests/_consoles_support.py`.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.db import Database
from agentworks.errors import (
    AlreadyExistsError,
    ConnectivityError,
    NotFoundError,
    StateError,
    ValidationError,
)
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions.multi_console import (
    add_sessions,
    add_shell,
    delete_console,
    delete_console_record,
    describe_console,
    list_consoles,
    remove_sessions,
    reorder_sessions,
)
from agentworks.sessions.multi_console import (
    create_console as _real_create_console,
)
from tests._consoles_support import _seed_sessions, _seed_vm, _stub_build_registry, _StubConfig  # noqa: F401
from tests.conftest import _FakeResult, _FakeTarget
from tests.console_helpers import create_console_record as create_console

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput

BOOT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _refuse_live_target(*args: object, **kwargs: object) -> None:
    raise ConnectivityError("SSH identity unavailable")


def _validate_create(db: Database, **kwargs: object) -> None:
    _real_create_console(
        db,
        _StubConfig(),
        interaction=TtyInteractionPolicy.REFUSE,
        **kwargs,  # type: ignore[arg-type]
    )


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


def test_running_session_names_raises_on_unreachable(db: Database, fake_target: _FakeTarget) -> None:
    """If sessions exist with valid pid+boot_id but the probe returns nothing,
    treat that as a transport failure and raise instead of silently reporting
    'no running sessions'."""
    from agentworks.sessions.multi_console import running_session_names

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    db._conn.execute("UPDATE sessions SET pid = 100, boot_id = ? WHERE name = 'alpha'", (BOOT_ID,))
    db._conn.commit()
    # Probe returns empty stdout (simulates transport failure caught by check=False).
    fake_target.run = lambda command, **kwargs: _FakeResult(returncode=255, stdout="")  # type: ignore[assignment]

    with pytest.raises(ConnectivityError):
        running_session_names(db, _StubConfig(), "vm1")


@pytest.mark.parametrize("pid", [None, 100])
def test_running_session_names_rejects_legacy_row_before_connectivity_probe(
    db: Database,
    fake_target: _FakeTarget,
    pid: int | None,
) -> None:
    from agentworks.sessions.multi_console import running_session_names

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["legacy"])
    db._conn.execute(
        "UPDATE sessions SET socket_path = NULL, pid = ?, boot_id = ? WHERE name = 'legacy'",
        (pid, BOOT_ID),
    )
    db._conn.commit()

    with pytest.raises(StateError) as caught:
        running_session_names(db, _StubConfig(), "vm1")

    assert caught.value.entity_kind == "session"
    assert caught.value.entity_name == "legacy"
    assert caught.value.hint is not None
    assert fake_target.commands == []


def test_running_session_names_repairs_incomplete_live_identity(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.sessions.multi_console import running_session_names
    from agentworks.sessions.tmux import FingerprintProbe, ProbeStatus, TmuxServerFingerprint

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    monkeypatch.setattr(
        "agentworks.sessions.tmux.capture_tmux_server_fingerprint",
        lambda **kwargs: FingerprintProbe(
            ProbeStatus.PRESENT,
            TmuxServerFingerprint(pid=100, boot_id=BOOT_ID, start_ticks=77),
        ),
    )

    def stub_run(command: str, **kwargs: object) -> _FakeResult:
        fake_target.commands.append(command)
        boot_hex = BOOT_ID.encode().hex()
        return _FakeResult(returncode=0, stdout=f"S:alpha:0::0::0:{boot_hex}:0\n")

    fake_target.run = stub_run  # type: ignore[assignment]

    assert running_session_names(db, _StubConfig(), "vm1") == ["alpha"]
    repaired = db.get_session("alpha")
    assert repaired is not None
    assert (repaired.pid, repaired.boot_id, repaired.tmux_server_start_ticks) == (100, BOOT_ID, 77)


def test_running_session_names_refuses_unresolved_incomplete_identity(
    db: Database,
    fake_target: _FakeTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.sessions.multi_console import running_session_names
    from agentworks.sessions.tmux import FingerprintProbe, ProbeStatus

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha"])
    monkeypatch.setattr(
        "agentworks.sessions.tmux.capture_tmux_server_fingerprint",
        lambda **kwargs: FingerprintProbe(ProbeStatus.UNKNOWN),
    )

    with pytest.raises(ConnectivityError):
        running_session_names(db, _StubConfig(), "vm1")

    unresolved = db.get_session("alpha")
    assert unresolved is not None and unresolved.pid is None
    assert fake_target.commands == []


def test_running_session_names_uses_live_status_check(db: Database, fake_target: _FakeTarget) -> None:
    """running_session_names SSH-probes via batch_check_all_sessions and
    returns only sessions whose live tmux state is OK."""
    from agentworks.sessions.multi_console import running_session_names

    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["alpha", "beta", "gamma"])
    # Give each session a PID so it's eligible for the batch check.
    db._conn.execute("UPDATE sessions SET pid = 100, boot_id = ? WHERE name = 'alpha'", (BOOT_ID,))
    db._conn.execute("UPDATE sessions SET pid = 200, boot_id = ? WHERE name = 'beta'", (BOOT_ID,))
    db._conn.execute("UPDATE sessions SET pid = 300, boot_id = ? WHERE name = 'gamma'", (BOOT_ID,))
    db._conn.commit()

    # batch_check_all_sessions emits one compound shell command per VM. We
    # reply with status lines for alpha (alive) + beta (alive); gamma's line
    # claims the session is gone.
    def stub_run(command: str, **kwargs: object) -> _FakeResult:
        fake_target.commands.append(command)
        if "has-session -t =alpha" in command and "has-session -t =beta" in command:
            missing_session = b"can't find session: gamma".hex()
            missing_server = b"no server running on /gamma".hex()
            boot_hex = BOOT_ID.encode().hex()
            return _FakeResult(
                returncode=0,
                stdout=(
                    f"S:alpha:0::0::0:{boot_hex}:0\n"
                    f"S:beta:0::0::0:{boot_hex}:0\n"
                    f"S:gamma:1:{missing_session}:1:{missing_server}:0:{boot_hex}:1\n"
                ),
            )
        return _FakeResult()

    fake_target.run = stub_run  # type: ignore[assignment]

    names = running_session_names(db, _StubConfig(), "vm1")
    assert names == ["alpha", "beta"]


def test_console_create_all_running_reuses_one_loaded_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.cli.commands import console as console_commands
    from agentworks.sessions import multi_console

    database = object()
    config = object()
    load_calls: list[None] = []
    observed_configs: list[object] = []

    def load_config() -> object:
        load_calls.append(None)
        return config

    monkeypatch.setattr(console_commands, "get_db", lambda: database)
    monkeypatch.setattr(console_commands, "prompt_vm", lambda candidate, name: SimpleNamespace(name=name))
    monkeypatch.setattr("agentworks.config.load_config", load_config)
    monkeypatch.setattr(
        multi_console,
        "running_session_names",
        lambda candidate, loaded, vm_name: observed_configs.append(loaded) or [],
    )
    monkeypatch.setattr(
        multi_console,
        "create_console",
        lambda candidate, loaded, **kwargs: observed_configs.append(loaded),
    )

    console_commands.console_create(
        name="focused",
        sessions=[],
        vm="vm1",
        all_sessions=False,
        all_running=True,
        add_admin_shell=True,
    )

    assert load_calls == [None]
    assert observed_configs == [config, config]


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
        _validate_create(db, name="empty", vm_name="vm1", session_specs=[])


def test_create_console_rejects_empty_fill_all(db: Database) -> None:
    _seed_vm(db)  # no sessions seeded
    with pytest.raises(ValidationError, match="VM 'vm1' has no sessions"):
        _validate_create(db, name="empty", vm_name="vm1", session_specs=[], fill_all=True)


def test_create_console_rejects_duplicate_name(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    with pytest.raises(AlreadyExistsError, match="already exists"):
        _validate_create(db, name="con", vm_name="vm1", session_specs=["a"])


def test_create_console_rejects_unknown_vm(db: Database) -> None:
    with pytest.raises(NotFoundError, match="not found"):
        _validate_create(db, name="con", vm_name="ghost", session_specs=["a"])


def test_create_console_rejects_unknown_session(db: Database) -> None:
    _seed_vm(db)
    with pytest.raises(NotFoundError, match="not found"):
        _validate_create(db, name="con", vm_name="vm1", session_specs=["ghost"])


def test_create_console_rejects_cross_vm_session(db: Database) -> None:
    _seed_vm(db, "vm1")
    _seed_vm(db, "vm2")
    _seed_sessions(db, ["a"], workspace_name="ws-vm2")
    with pytest.raises(ValidationError, match="is not on VM 'vm1'"):
        _validate_create(db, name="con", vm_name="vm1", session_specs=["a"])


def test_create_console_rejects_dup_in_args(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    with pytest.raises(ValidationError, match="listed more than once"):
        _validate_create(db, name="con", vm_name="vm1", session_specs=["a", "a+1"])


def test_create_console_rolls_back_on_failure(db: Database) -> None:
    """All-or-nothing: pre-existing console name is caught up front, no orphan rows."""
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    db.insert_console("con", "vm1")
    with pytest.raises(AlreadyExistsError, match="already exists"):
        _validate_create(db, name="con", vm_name="vm1", session_specs=["a"])
    assert db.list_console_sessions("con") == []


# -- Orchestration: add_sessions / remove_sessions / add_shell -------------


def test_add_sessions(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    add_sessions(
        db, _StubConfig(), console_name="con", session_specs=["b+1", "c"], interaction=TtyInteractionPolicy.REFUSE
    )
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
        add_sessions(
            db, _StubConfig(), console_name="con", session_specs=["a"], interaction=TtyInteractionPolicy.REFUSE
        )


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
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c", "d", "e"])

    reorder_sessions(db, _StubConfig(), console_name="con", session_names=["d", "b"])

    members = db.list_console_sessions("con")
    # d, b first (argument order), then a, c, e (original relative order).
    assert [m.session_name for m in members] == ["d", "b", "a", "c", "e"]


def test_reorder_sessions_noop_when_already_in_requested_order(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Asking to bump the sessions that are already at the front in that
    order is a clean no-op: no position writes, informational message."""
    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c"])
    monkeypatch.setattr(
        "agentworks.sessions.multi_console._live_target",
        lambda *args, **kwargs: pytest.fail("a no-op reorder must not preflight live sync"),
    )

    reorder_sessions(db, _StubConfig(), console_name="con", session_names=["a", "b"])

    members = db.list_console_sessions("con")
    assert [m.session_name for m in members] == ["a", "b", "c"]
    assert any("already in the requested order" in m for m in captured_output.info)


def test_add_sessions_live_preflight_refusal_preserves_membership(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    monkeypatch.setattr(
        "agentworks.sessions.multi_console._live_target",
        _refuse_live_target,
    )
    monkeypatch.setattr("agentworks.sessions.multi_console._pane_secret_target", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        "agentworks.secrets.resolve_for_command",
        lambda *args, **kwargs: pytest.fail("live preflight refusal must precede secret resolution"),
    )

    with pytest.raises(ConnectivityError):
        add_sessions(
            db, _StubConfig(), console_name="con", session_specs=["b+1"], interaction=TtyInteractionPolicy.REFUSE
        )

    assert [member.session_name for member in db.list_console_sessions("con")] == ["a"]


def test_remove_sessions_live_preflight_refusal_preserves_membership(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])
    monkeypatch.setattr(
        "agentworks.sessions.multi_console._live_target",
        _refuse_live_target,
    )

    with pytest.raises(ConnectivityError):
        remove_sessions(db, _StubConfig(), console_name="con", session_names=["b"])

    assert [member.session_name for member in db.list_console_sessions("con")] == ["a", "b"]


def test_reorder_sessions_live_preflight_refusal_preserves_order(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])
    monkeypatch.setattr(
        "agentworks.sessions.multi_console._live_target",
        _refuse_live_target,
    )

    with pytest.raises(ConnectivityError):
        reorder_sessions(db, _StubConfig(), console_name="con", session_names=["b"])

    assert [member.session_name for member in db.list_console_sessions("con")] == ["a", "b"]


def test_add_shell_live_preflight_refusal_preserves_shells(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_vm(db, with_tailscale=True)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+1"])
    original_shells = db.get_console_session("con", "a")
    assert original_shells is not None
    monkeypatch.setattr(
        "agentworks.sessions.multi_console._live_target",
        _refuse_live_target,
    )
    monkeypatch.setattr("agentworks.sessions.multi_console._pane_secret_target", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        "agentworks.secrets.resolve_for_command",
        lambda *args, **kwargs: pytest.fail("live preflight refusal must precede secret resolution"),
    )

    with pytest.raises(ConnectivityError):
        add_shell(db, _StubConfig(), console_name="con", session_name="a", interaction=TtyInteractionPolicy.REFUSE)

    member = db.get_console_session("con", "a")
    assert member is not None
    assert member.shells == original_shells.shells


def test_reorder_sessions_rejects_non_member(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    with pytest.raises(NotFoundError, match="not a member"):
        reorder_sessions(db, _StubConfig(), console_name="con", session_names=["b"])


def test_reorder_sessions_rejects_duplicates(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])
    with pytest.raises(ValidationError, match="listed more than once"):
        reorder_sessions(db, _StubConfig(), console_name="con", session_names=["a", "a"])


def test_reorder_sessions_rejects_missing_console(db: Database) -> None:
    _seed_vm(db)
    with pytest.raises(NotFoundError, match="console 'nope' not found"):
        reorder_sessions(db, _StubConfig(), console_name="nope", session_names=["a"])


def test_reorder_sessions_rejects_empty_input(db: Database) -> None:
    """No-op-shaped input ('please reorder, but I gave you no sessions') is
    almost certainly a typo. Match create_console's stance on this."""
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])
    with pytest.raises(ValidationError, match="no sessions specified"):
        reorder_sessions(db, _StubConfig(), console_name="con", session_names=[])


def test_add_shell_appends_entry(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a+1"])
    add_shell(
        db,
        _StubConfig(),
        console_name="con",
        session_name="a",
        cwd="src",
        admin=True,
        interaction=TtyInteractionPolicy.REFUSE,
    )
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
        add_shell(db, _StubConfig(), console_name="con", session_name="b", interaction=TtyInteractionPolicy.REFUSE)


def test_add_shell_rejects_bad_cwd(db: Database) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a"])
    with pytest.raises(ValidationError):
        add_shell(
            db, _StubConfig(), console_name="con", session_name="a", cwd="/etc", interaction=TtyInteractionPolicy.REFUSE
        )


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


def test_describe_console_uses_iteration_index(db: Database, captured_output: CapturedOutput) -> None:
    """After a remove, members keep their position gap in the DB but describe
    renders 0..N-1 line numbers."""
    _seed_vm(db)
    _seed_sessions(db, ["a", "b", "c"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b", "c"])
    remove_sessions(db, _StubConfig(), console_name="con", session_names=["a"])
    describe_console(db, name="con")
    member_lines = [m for m in captured_output.info if m.lstrip().startswith("[")]
    assert member_lines == [
        "[0] b  (no extra shells)",
        "[1] c  (no extra shells)",
    ]


def test_list_consoles_renders_counts(db: Database, captured_output: CapturedOutput) -> None:
    _seed_vm(db)
    _seed_sessions(db, ["a", "b"])
    create_console(db, name="con", vm_name="vm1", session_specs=["a", "b"])
    list_consoles(db)
    rows = [m for m in captured_output.info if m.startswith("con")]
    assert any("vm1" in r and r.endswith("2") for r in rows)
