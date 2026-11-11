"""Dedicated serial teardown preserves prepared logical and runtime identities."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest

from agentworks.db import SessionMode
from agentworks.errors import BrokenStateError, ConnectivityError, StateError
from agentworks.sessions.manager import _teardown as teardown_mod
from agentworks.sessions.manager._teardown import DedicatedTeardownPlan
from agentworks.sessions.tmux import ProbeStatus

from .test_session_batch_stop_parallelism import BOOT_ID, _seed_session, _Target

if TYPE_CHECKING:
    from agentworks.db import Database, SessionRow
    from agentworks.transports import Transport


@pytest.mark.parametrize("upgraded", [False, True])
@pytest.mark.parametrize("change", ["run", "row", "runtime"])
def test_synchronous_completion_preserves_replaced_prepared_identity(
    db: Database, monkeypatch: pytest.MonkeyPatch, upgraded: bool, change: str
) -> None:
    from agentworks.sessions import tmux as tmux_mod

    socket_path = "/run/agentworks/admin-tmux-sockets/agentworks/session.sock"
    session = _seed_session(db, "session", socket_path=socket_path, pid=101, start_ticks=None if upgraded else 201)
    monkeypatch.setattr(tmux_mod, "require_socket_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(tmux_mod, "_matches_launch_attempt", lambda **kwargs: True)
    monkeypatch.setattr(
        tmux_mod,
        "capture_tmux_server_fingerprint",
        lambda **kwargs: tmux_mod.FingerprintProbe(
            ProbeStatus.PRESENT, tmux_mod.TmuxServerFingerprint(pid=101, boot_id=BOOT_ID, start_ticks=201)
        ),
    )
    changed_rows: list[SessionRow] = []

    def remote_success(plan: DedicatedTeardownPlan) -> None:
        before = db.get_session(session.name)
        assert before is not None and before.tmux_server_start_ticks == 201
        if change == "run":
            db.set_session_run_id(session.name, str(uuid4()))
        elif change == "row":
            db.delete_session(session.name)
            db.insert_session(session.name, "work", "default", SessionMode.ADMIN, socket_path=socket_path)
        else:
            db.update_session_runtime(
                session.name, socket_path=socket_path, pid=102, boot_id=BOOT_ID, tmux_server_start_ticks=202
            )
        changed = db.get_session(session.name)
        assert changed is not None
        changed_rows.append(changed)

    monkeypatch.setattr(teardown_mod, "execute_dedicated_teardown", remote_success)
    with pytest.raises(StateError):
        teardown_mod.teardown_dedicated_session(
            session, target=cast("Transport", _Target("serial")), target_owns_session=True, db=db, force=False
        )
    assert db.get_session(session.name) == changed_rows[0]


@pytest.mark.parametrize("change", ["run", "row", "runtime"])
def test_incomplete_upgrade_refuses_replaced_identity_before_remote_execution(
    db: Database, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from agentworks.sessions import tmux as tmux_mod

    socket_path = "/run/agentworks/admin-tmux-sockets/agentworks/session.sock"
    session = _seed_session(db, "session", socket_path=socket_path, pid=101, start_ticks=None)
    monkeypatch.setattr(tmux_mod, "require_socket_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(tmux_mod, "_matches_launch_attempt", lambda **kwargs: True)
    monkeypatch.setattr(
        tmux_mod,
        "capture_tmux_server_fingerprint",
        lambda **kwargs: tmux_mod.FingerprintProbe(
            ProbeStatus.PRESENT, tmux_mod.TmuxServerFingerprint(pid=101, boot_id=BOOT_ID, start_ticks=201)
        ),
    )
    read = db.get_session
    changed_rows: list[SessionRow] = []
    replacement_started = False

    def replace_on_refetch(name: str) -> SessionRow | None:
        nonlocal replacement_started
        if not replacement_started:
            replacement_started = True
            if change == "run":
                db.set_session_run_id(name, str(uuid4()))
            elif change == "row":
                db.delete_session(name)
                db.insert_session(name, "work", "default", SessionMode.ADMIN, socket_path=socket_path)
            else:
                db.update_session_runtime(
                    name, socket_path=socket_path, pid=102, boot_id=BOOT_ID, tmux_server_start_ticks=202
                )
            changed = read(name)
            assert changed is not None
            changed_rows.append(changed)
        return read(name)

    monkeypatch.setattr(db, "get_session", replace_on_refetch)
    monkeypatch.setattr(
        teardown_mod, "execute_dedicated_teardown", lambda plan: pytest.fail("replacement authorized teardown")
    )
    with pytest.raises(StateError):
        teardown_mod.teardown_dedicated_session(
            session, target=cast("Transport", _Target("serial")), target_owns_session=True, db=db, force=False
        )
    assert read(session.name) == changed_rows[0]


@pytest.mark.parametrize("change", ["run", "row", "runtime"])
def test_incomplete_upgrade_preserves_changes_during_native_ownership_probe(
    db: Database, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from agentworks.sessions import tmux as tmux_mod

    socket_path = "/run/agentworks/admin-tmux-sockets/agentworks/session.sock"
    session = _seed_session(db, "session", socket_path=socket_path, pid=101, start_ticks=None)
    monkeypatch.setattr(tmux_mod, "require_socket_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        tmux_mod,
        "capture_tmux_server_fingerprint",
        lambda **kwargs: tmux_mod.FingerprintProbe(
            ProbeStatus.PRESENT, tmux_mod.TmuxServerFingerprint(pid=101, boot_id=BOOT_ID, start_ticks=201)
        ),
    )
    changed_rows: list[SessionRow] = []

    def ownership_proof(**kwargs: object) -> bool:
        if change == "run":
            db.set_session_run_id(session.name, str(uuid4()))
        elif change == "row":
            db.delete_session(session.name)
            db.insert_session(session.name, "work", "default", SessionMode.ADMIN, socket_path=socket_path)
        else:
            db.update_session_runtime(
                session.name, socket_path=socket_path, pid=102, boot_id=BOOT_ID, tmux_server_start_ticks=202
            )
        changed = db.get_session(session.name)
        assert changed is not None
        changed_rows.append(changed)
        return True

    monkeypatch.setattr(tmux_mod, "_matches_launch_attempt", ownership_proof)
    monkeypatch.setattr(
        teardown_mod, "execute_dedicated_teardown", lambda plan: pytest.fail("replacement authorized teardown")
    )
    with pytest.raises(StateError):
        teardown_mod.teardown_dedicated_session(
            session, target=cast("Transport", _Target("serial")), target_owns_session=True, db=db, force=False
        )
    assert db.get_session(session.name) == changed_rows[0]


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("mode", ["serial", "batch"])
def test_unreadable_runtime_recovery_preserves_state_in_both_lanes(
    db: Database, monkeypatch: pytest.MonkeyPatch, force: bool, mode: str
) -> None:
    from agentworks.sessions import tmux as tmux_mod

    socket_path = "/run/agentworks/admin-tmux-sockets/agentworks/session.sock"
    session = _seed_session(db, "session", socket_path=socket_path, pid=101, start_ticks=201)
    target = cast("Transport", _Target("recovery"))
    monkeypatch.setattr(tmux_mod, "require_socket_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        tmux_mod, "capture_tmux_server_fingerprint", lambda **kwargs: tmux_mod.FingerprintProbe(ProbeStatus.ABSENT)
    )

    def unknown(plan: DedicatedTeardownPlan) -> bool:
        raise ConnectivityError("presence unavailable")

    monkeypatch.setattr(teardown_mod, "_prove_plan_runtime_absent", unknown)
    monkeypatch.setattr(teardown_mod, "_remove_stale_socket", lambda **kwargs: pytest.fail("unproven cleanup"))
    if mode == "serial":
        with pytest.raises(ConnectivityError if force else BrokenStateError):
            teardown_mod.teardown_dedicated_session(
                session, target=target, target_owns_session=True, db=db, force=force
            )
    else:
        plan = teardown_mod.prepare_dedicated_teardown(
            db, session, vm_name="box", target=target, target_owns_session=True, force=force
        )
        failures = teardown_mod.execute_concurrent_dedicated_teardowns([plan], db=db)
        assert len(failures) == 1 and failures[0][0] == session.name
    assert db.get_session(session.name) == session
