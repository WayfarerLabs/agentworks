"""Existing-session declaration replacement follows the locked launch lifecycle."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace

import pytest

from agentworks.db import Database, DesiredRuntimeState, SessionStatus, VersionedPayload, VMStatus
from agentworks.db.instance_state import JsonObject
from agentworks.errors import AgentworksError, StateError, UserAbort
from agentworks.instance_specs import get_instance_overlay, parse_instance_spec
from agentworks.native_locks import native_locks
from agentworks.native_locks.rows import vm_exclusive
from agentworks.plugins.claude.conversation_probe import ConversationProbe
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.sessions import manager
from tests.runtime_state.runtime_support import REFUSE, seed_session, seed_vm
from tests.runtime_state.runtime_support import runtime as runtime

type Runtime = tuple[dict[str, VMStatus], dict[str, SessionStatus], list[tuple[str, str]]]


SID = "939b1597-7c61-5ace-80f4-14617b7b4257"


def _seed(db: Database) -> None:
    seed_vm(db, "box")
    seed_session(db, "work", "box")


def _allow_restart(monkeypatch: pytest.MonkeyPatch, runtime: Runtime) -> None:
    monkeypatch.setattr(
        "agentworks.sessions.manager._runtime.observe_session_runtime",
        lambda session, **kwargs: runtime[1].get(session.name, SessionStatus.STOPPED),
    )
    monkeypatch.setattr(
        manager, "_teardown_session", lambda *args, **kwargs: runtime[1].update({"work": SessionStatus.STOPPED})
    )


def _put(db: Database, raw: str) -> None:
    overlay = parse_instance_spec("session", raw)
    with db.transaction():
        db.instance_state.put_desired_overlay("session", "work", overlay.payload)


def _value(db: Database) -> JsonObject | None:
    stored = db.instance_state.get_desired_overlay("session", "work")
    return None if stored is None else stored.payload.value


def test_start_replaces_and_restart_clears_spec_through_runtime(db, make_config, runtime, monkeypatch):
    _seed(db)
    config = make_config()
    _put(db, '{"env":{"OLD":"before"}}')
    manager.start_session(db, config, name="work", spec='{"env":{"NEW":"after"}}', interaction=REFUSE)
    assert _value(db) == {"env": {"NEW": {"value": "after"}}}
    assert ("session-start", "work") in runtime[2]
    _allow_restart(monkeypatch, runtime)
    manager.restart_session(db, config, name="work", spec="{}", yes=True, interaction=REFUSE)
    assert _value(db) is None


def test_omitted_spec_retains_stored_layer(db, make_config, runtime):
    _seed(db)
    _put(db, '{"env":{"RETAINED":"value"}}')
    prior = db.instance_state.get_desired_overlay("session", "work")
    manager.start_session(db, make_config(), name="work", interaction=REFUSE)
    assert db.instance_state.get_desired_overlay("session", "work") == prior


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " \t",
        "not-json",
        "[]",
        '{"inherits":["default"]}',
        '{"harness_integration":{"name":"shell","unknown":true}}',
        '{"artifacts":{"bundles":["not-declared"]}}',
    ],
)
def test_invalid_spec_preserves_prior_state_before_observation(db, make_config, runtime, monkeypatch, raw):
    _seed(db)
    _put(db, '{"env":{"RETAINED":"value"}}')
    before = db.instance_state.get_desired_overlay("session", "work")
    monkeypatch.setattr(manager, "observe_session_statuses", lambda *a, **k: pytest.fail("observed runtime"))
    with pytest.raises(AgentworksError):
        manager.restart_session(db, make_config(), name="work", spec=raw, yes=True, interaction=REFUSE)
    assert db.instance_state.get_desired_overlay("session", "work") == before
    assert runtime[2] == []


def test_disabled_integration_rejected_before_observation(db, make_config, runtime, monkeypatch):
    _seed(db)
    monkeypatch.setattr(manager, "observe_session_statuses", lambda *a, **k: pytest.fail("observed runtime"))
    with pytest.raises(AgentworksError):
        manager.restart_session(
            db,
            make_config(),
            name="work",
            yes=True,
            interaction=REFUSE,
            spec=json.dumps({"harness_integration": {"name": "claude-code", "force_uuid": SID}}),
        )
    assert _value(db) is None
    assert runtime[2] == []


def test_rejected_restart_consent_preserves_overlay(db, make_config, runtime, monkeypatch):
    _seed(db)
    _put(db, '{"env":{"RETAINED":"value"}}')
    before = db.instance_state.get_desired_overlay("session", "work")
    runtime[1]["work"] = SessionStatus.RUNNING
    monkeypatch.setattr("agentworks.output.confirm", lambda *a, **k: False)
    with pytest.raises(UserAbort):
        manager.restart_session(
            db, make_config(), name="work", spec='{"env":{"NEW":"after"}}', interaction=TtyInteractionPolicy.ALLOW
        )
    assert db.instance_state.get_desired_overlay("session", "work") == before
    assert runtime[2] == []


@pytest.mark.parametrize(
    "payload", [VersionedPayload(1, {"unknown": "private-value"}), VersionedPayload(99, {"opaque": "private-value"})]
)
def test_clear_supersedes_unsupported_selected_overlay(db, make_config, runtime, captured_output, payload):
    _seed(db)
    db.instance_state.put_desired_overlay("session", "work", payload)
    manager.start_session(db, make_config(), name="work", spec="{}", interaction=REFUSE)
    assert _value(db) is None
    assert ("session-start", "work") in runtime[2]
    rendered = repr(captured_output.lines)
    for key, value in payload.value.items():
        assert key not in rendered
        assert value not in rendered


def test_spec_hands_its_layer_to_the_batch_and_takes_no_lock_of_its_own(db, make_config, runtime, monkeypatch):
    _seed(db)
    entered = []

    def fail(*args, **kwargs):
        assert "held" not in kwargs
        # Nothing is held around the batch: the batch's own sets would refuse otherwise.
        with native_locks(db.path, vm_exclusive("box")):
            pass
        session = db.get_session("work")
        kwargs["declarations"]["work"].accept(session, SessionStatus.STOPPED)
        entered.append(session.name)
        raise StateError("fixture launch failure")

    monkeypatch.setattr("agentworks.runtime_state.run_session_batch", fail)
    with pytest.raises(StateError):
        manager.start_session(db, make_config(), name="work", spec='{"env":{"NEW":"after"}}', interaction=REFUSE)
    # The batch accepted the layer, which survives the later failure.
    assert _value(db) == {"env": {"NEW": {"value": "after"}}}
    assert entered == ["work"]


def test_session_replacement_during_consent_preserves_replacement_spec(db, make_config, runtime, monkeypatch):
    _seed(db)
    runtime[1]["work"] = SessionStatus.RUNNING
    config = make_config()
    before = db.get_session("work")

    def replace_session(*args, **kwargs):
        db.delete_session("work")
        seed_session(db, "work", "box")
        _put(db, '{"env":{"OTHER":"owner"}}')
        return True

    monkeypatch.setattr("agentworks.output.confirm", replace_session)
    with pytest.raises(StateError):
        manager.restart_session(
            db, config, name="work", spec='{"env":{"NEW":"after"}}', interaction=TtyInteractionPolicy.ALLOW
        )
    assert db.get_session("work").session_uuid != before.session_uuid
    assert _value(db) == {"env": {"OTHER": {"value": "owner"}}}
    assert runtime[2] == []


def test_session_replacement_before_intent_acceptance_refuses_before_the_layer_is_replaced(
    db, make_config, runtime, monkeypatch
):
    """The consent is asked with nothing locked; a session replaced before the
    batch accepts its intent is not the one it was about, so it refuses there."""
    import agentworks.runtime_state._families as families

    _seed(db)
    before = db.get_session("work")
    asked = []
    original = native_locks

    @contextmanager
    def replace_at_intent_lock(*args, **kwargs):
        with original(*args, **kwargs) as held:
            db.delete_session("work")
            seed_session(db, "work", "box")
            _put(db, '{"env":{"OTHER":"owner"}}')
            yield held

    monkeypatch.setattr(families, "native_locks", replace_at_intent_lock)
    monkeypatch.setattr(
        manager, "observe_session_statuses", lambda sessions, **k: {s.name: SessionStatus.RUNNING for s in sessions}
    )
    monkeypatch.setattr("agentworks.output.confirm", lambda *a, **k: asked.append(True) or True)
    with pytest.raises(StateError):
        manager.restart_session(
            db, make_config(), name="work", spec='{"env":{"NEW":"after"}}', interaction=TtyInteractionPolicy.ALLOW
        )
    assert asked == [True]
    assert db.get_session("work").session_uuid != before.session_uuid
    assert _value(db) == {"env": {"OTHER": {"value": "owner"}}}
    assert runtime[2] == []


def test_existing_session_can_adopt_and_remove_force_uuid(db, make_config, runtime, monkeypatch):
    _seed(db)
    config = make_config()
    config = replace(config, enabled_system_plugins=(*config.enabled_system_plugins, "claude"))
    monkeypatch.setattr(
        "agentworks.plugins.claude.harness_integration.probe_conversation", lambda *a, **k: ConversationProbe(True)
    )
    forced = {"harness_integration": {"name": "claude-code", "force_uuid": SID}}
    manager.start_session(db, config, name="work", spec=json.dumps(forced), interaction=REFUSE)
    assert db.get_session("work").harness_integration_state["claude-code"]["session_id"] == SID
    stored = get_instance_overlay(db, "session", "work")
    assert stored is not None
    assert stored.payload.value == forced
    _allow_restart(monkeypatch, runtime)
    manager.restart_session(db, config, name="work", spec="{}", yes=True, interaction=REFUSE)
    assert _value(db) is None


def test_new_secret_reference_is_validated_and_retained_on_resolution_failure(db, make_config, runtime):
    _seed(db)
    with pytest.raises(StateError):
        manager.start_session(
            db,
            make_config(),
            name="work",
            spec='{"env":{"NEW":{"secret":"missing-runtime-token"}}}',
            interaction=REFUSE,
        )
    assert _value(db) == {"env": {"NEW": {"secret": "missing-runtime-token"}}}
    assert not any(action == "session-start" for action, _ in runtime[2])


def test_start_on_running_session_saves_spec_without_replacing_runtime(db, make_config, runtime):
    _seed(db)
    runtime[1]["work"] = SessionStatus.RUNNING
    before = db.get_session("work")
    manager.start_session(db, make_config(), name="work", spec='{"env":{"NEW":"after"}}', interaction=REFUSE)
    assert _value(db) == {"env": {"NEW": {"value": "after"}}}
    assert db.get_session("work").pid == before.pid
    assert not any(action == "session-start" for action, _ in runtime[2])


@pytest.mark.parametrize("change", ["workspace-parent", "vm-incarnation", "runtime"])
def test_parent_or_runtime_change_during_consent_refuses_spec(db, make_config, runtime, monkeypatch, change):
    _seed(db)
    if change == "workspace-parent":
        seed_vm(db, "other")
    runtime[1]["work"] = SessionStatus.RUNNING
    config = make_config()

    def change_identity(*args, **kwargs):
        if change == "workspace-parent":
            db._conn.execute("UPDATE workspaces SET vm_name = 'other' WHERE name = 'ws-box'")
            db._conn.commit()
        elif change == "vm-incarnation":
            db._conn.execute("UPDATE vms SET vm_uuid = ? WHERE name = 'box'", (SID,))
            db._conn.commit()
        else:
            db.update_session_runtime("work", socket_path="/new/socket", pid=99, boot_id=SID, tmux_server_start_ticks=1)
        return True

    monkeypatch.setattr("agentworks.output.confirm", change_identity)
    with pytest.raises(StateError):
        manager.restart_session(
            db, config, name="work", spec='{"env":{"NEW":"after"}}', interaction=TtyInteractionPolicy.ALLOW
        )
    assert _value(db) is None
    assert runtime[2] == []


@pytest.mark.parametrize("operation", ["start", "restart"])
@pytest.mark.parametrize("replacement", [True, False])
def test_force_new_conflict_preserves_overlay_runtime_and_target(
    db, make_config, runtime, monkeypatch, operation, replacement
):
    _seed(db)
    config = make_config()
    config = replace(config, enabled_system_plugins=(*config.enabled_system_plugins, "claude"))
    forced = {"harness_integration": {"name": "claude-code", "force_uuid": SID}}
    _put(db, '{"env":{"OLD":"retained"}}' if replacement else json.dumps(forced))
    selected = db.get_session("work")
    db.set_session_desired_runtime_state(
        "work", DesiredRuntimeState.STOPPED, expected_session_uuid=selected.session_uuid
    )
    before = db.get_session("work")
    prior = db.instance_state.get_desired_overlay("session", "work")
    if replacement:
        monkeypatch.setattr(manager, "observe_session_statuses", lambda *a, **k: pytest.fail("observed runtime"))
    monkeypatch.setattr("agentworks.output.confirm", lambda *a, **k: pytest.fail("asked for restart consent"))
    options = {"yes": True} if operation == "restart" else {}
    with pytest.raises(StateError):
        getattr(manager, f"{operation}_session")(
            db,
            config,
            name="work",
            force_new=True,
            spec=json.dumps(forced) if replacement else None,
            interaction=REFUSE,
            **options,
        )
    assert db.get_session("work") == before
    assert db.instance_state.get_desired_overlay("session", "work") == prior
    assert runtime[2] == []


def test_restart_consent_is_asked_unlocked_and_a_status_change_before_the_lock_refuses(
    db, make_config, runtime, monkeypatch
):
    _seed(db)
    before = _value(db)
    # The consent sees it running; the batch, observing again before it accepts the layer, sees it stopped.
    statuses = iter([SessionStatus.RUNNING, SessionStatus.STOPPED])
    monkeypatch.setattr(
        manager, "observe_session_statuses", lambda sessions, **k: {s.name: next(statuses) for s in sessions}
    )
    asked = []

    def confirm(*args, **kwargs):
        with native_locks(db.path, vm_exclusive("box")):
            asked.append(True)
        return True

    monkeypatch.setattr("agentworks.output.confirm", confirm)
    with pytest.raises(StateError) as refused:
        manager.restart_session(
            db, make_config(), name="work", spec='{"env":{"NEW":"after"}}', interaction=TtyInteractionPolicy.ALLOW
        )
    assert refused.value.entity_name == "work"
    assert asked == [True]
    assert _value(db) == before
    assert runtime[2] == []
