"""Finite stored-overlay migration preserves context and success boundaries."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

import pytest

from agentworks.db import DesiredOverlayRecord, VersionedPayload
from agentworks.db.instance_state import JsonObject
from agentworks.errors import StateError
from agentworks.instance_specs import InstanceOverlay, UnsupportedStoredOverlayError, decode_stored_overlay
from agentworks.legacy_claude import LegacyClaudeContextRequired, checkpoint_conversion
from agentworks.schema import CapabilityBlock


def stored(raw: JsonObject, *, vm: bool = False, version: int | None = None) -> DesiredOverlayRecord:
    return DesiredOverlayRecord(
        "vm" if vm else "agent",
        "fixture",
        VersionedPayload(version or (2 if vm else 1), {"vm": {"cpus": 4}, "admin": raw} if vm else raw),
        "2026-09-07",
    )


def base() -> list[CapabilityBlock]:
    return [
        CapabilityBlock.of("codex"),
        CapabilityBlock.of(
            "claude-code",
            marketplaces=["inherited"],
            plugins=["base@inherited"],
            settings={"source": "settings.json", "strategy": "merge-preserve"},
        ),
        CapabilityBlock.of("shell"),
    ]


@pytest.mark.parametrize("vm", [False, True])
@pytest.mark.parametrize(
    "legacy",
    [
        {"claude_marketplaces": ["new", "inherited", "new"]},
        {"claude_plugins": ["new@inherited", "base@inherited"]},
        {"claude_marketplaces": [], "claude_plugins": []},
    ],
)
def test_conversion_retains_full_context_and_other_components(vm, legacy):
    original = stored({"shell": "zsh", **legacy}, vm=vm)
    initial = deepcopy(original.payload.value)
    attachments = base()
    decoded = decode_stored_overlay(original, legacy_user_base=attachments)
    component = cast("dict[str, Any]", decoded.payload.value["admin"] if vm else decoded.payload.value)
    entries = component["harness_integrations"]
    assert [item["name"] for item in entries] == ["codex", "claude-code", "shell"]
    assert entries[1]["settings"] == attachments[1].config["settings"]
    assert entries[1]["marketplaces"] == (["inherited", "new"] if legacy.get("claude_marketplaces") else ["inherited"])
    assert entries[1]["plugins"] == (
        ["base@inherited", "new@inherited"] if legacy.get("claude_plugins") else ["base@inherited"]
    )
    assert component["shell"] == "zsh"
    if vm:
        assert decoded.payload.value["vm"] == {"cpus": 4}
    assert original.payload.value == initial
    assert attachments[1].config["marketplaces"] == ["inherited"]


@pytest.mark.parametrize("raw", [{"claude_plugins": []}, {"claude_plugins": None}])
def test_absent_legacy_agent_values_do_not_enable_an_empty_claude_attachment(raw):
    decoded = decode_stored_overlay(stored(raw), legacy_user_base=[])
    assert decoded.payload.value == {}


@pytest.mark.parametrize(
    "raw",
    [
        {"claude_plugins": "private-input"},
        {"claude_plugins": [False]},
        {"claude_plugins": ["private-input"], "harness_integrations": []},
    ],
)
def test_invalid_and_ambiguous_legacy_fields_refuse_without_values(raw):
    original = stored(raw)
    with pytest.raises(StateError) as caught:
        decode_stored_overlay(original, legacy_user_base=base())
    assert "private-input" not in str(caught.value)
    assert original.payload.value == raw


def test_admin_null_is_invalid_but_agent_null_is_an_absent_old_field():
    with pytest.raises(StateError):
        decode_stored_overlay(stored({"claude_plugins": None}, vm=True), legacy_user_base=[])


@pytest.mark.parametrize("vm", [False, True])
def test_record_only_decode_reports_context_needed_after_validating_remaining_fields(vm):
    with pytest.raises(LegacyClaudeContextRequired):
        decode_stored_overlay(stored({"claude_plugins": []}, vm=vm))
    with pytest.raises(UnsupportedStoredOverlayError):
        decode_stored_overlay(stored({"claude_plugins": [], "future_field": "private"}, vm=vm))
    with pytest.raises(StateError) as caught:
        decode_stored_overlay(stored({"claude_plugins": [], "shell": 3}, vm=vm))
    assert not isinstance(caught.value, LegacyClaudeContextRequired)


@pytest.mark.parametrize("vm", [False, True])
def test_future_versions_never_enter_the_legacy_adapter(vm):
    with pytest.raises(UnsupportedStoredOverlayError):
        decode_stored_overlay(stored({"claude_plugins": []}, vm=vm, version=99), legacy_user_base=base())


def test_conversion_checkpoint_refuses_a_changed_stored_overlay(db):
    db.insert_vm("box", site="proxmox", hostname="box")
    db.insert_agent("fixture", "box", "agt-fixture")
    db.instance_state.put_desired_overlay("agent", "fixture", VersionedPayload(1, {"claude_plugins": []}))
    original = db.instance_state.get_desired_overlay("agent", "fixture")
    assert original is not None
    decoded = decode_stored_overlay(original, legacy_user_base=[])
    assert isinstance(decoded, InstanceOverlay)
    replacement = VersionedPayload(1, {"shell": "zsh"})
    db.instance_state.put_desired_overlay("agent", "fixture", replacement)
    with db.transaction(), pytest.raises(StateError):
        checkpoint_conversion(db, original, decoded.payload)
    assert db.instance_state.get_desired_overlay("agent", "fixture").payload == replacement


def test_record_only_doctor_marks_migration_pending_without_source_values(db):
    from agentworks.doctor import HealthGroup, InstanceStateHealthFactType, Status
    from agentworks.doctor_state import _report_instance_state

    db.insert_vm("box", site="proxmox", hostname="box")
    db.insert_agent("fixture", "box", "agt-fixture")
    payload = VersionedPayload(1, {"claude_plugins": ["private-source@fixture"]})
    db.instance_state.put_desired_overlay("agent", "fixture", payload)
    original = db.instance_state.get_desired_overlay("agent", "fixture")
    group = HealthGroup("fixture")
    _report_instance_state(group, None, db.list_vms(), db.instance_state.inspect_owner_state("agent", "fixture"))
    pending = [
        item
        for item in group.checks
        if item.instance_state and item.instance_state.fact_type is InstanceStateHealthFactType.MIGRATION_PENDING
    ]
    assert len(pending) == 1
    assert pending[0].status is Status.INFO
    assert "private-source" not in str(group)
    assert db.instance_state.get_desired_overlay("agent", "fixture") == original
