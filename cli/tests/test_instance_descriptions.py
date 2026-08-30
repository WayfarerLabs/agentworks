"""Resolved declarations and lifecycle evidence on live describe DTOs."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.db import AppliedStateKey, SessionMode, VersionedPayload
from agentworks.errors import ConfigError, StateError, ValidationError

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database
    from agentworks.instance_description import InstanceComparison, InstanceStateDescription, LifecycleEvidence
    from agentworks.vms.applied_state import SSHAppliedState


_FINGERPRINT = f"SHA256:{'A' * 43}"
_OTHER_FINGERPRINT = f"SHA256:{'E' * 43}"
_HARDWARE_REQUEST = "hardware-request"


def _seed_vm(
    db: Database,
    *,
    template: str | None = None,
    admin_template: str | None = None,
    cpus: int | None = 4,
    memory: int | None = 8,
    disk: int | None = 50,
    swap: int | None = 4,
) -> None:
    db.insert_vm(
        "box",
        site="proxmox",
        hostname="box",
        template=template,
        admin_template=admin_template,
        cpus=cpus,
        memory_gib=memory,
        disk_gib=disk,
        swap_gib=swap,
    )


def _vm_state(db: Database, config: Config) -> tuple[InstanceStateDescription, SSHAppliedState | None]:
    from agentworks.instance_description import load_instance_description_registry
    from agentworks.vms.manager.inspect import _vm_instance_state

    with db.snapshot():
        vm = db.get_vm("box")
        assert vm is not None
        registry = load_instance_description_registry(db, config, "vm", "box")
        inspection = db.instance_state.inspect_owner_state("vm", "box")
        return _vm_instance_state(registry, vm, inspection)


def _evidence_key(key: AppliedStateKey) -> str:
    return _HARDWARE_REQUEST if key is AppliedStateKey.HARDWARE_PROVENANCE else key.value


def _fact(state: InstanceStateDescription, key: AppliedStateKey) -> LifecycleEvidence:
    return next(fact for fact in state.lifecycle_evidence if fact.key == _evidence_key(key))


def _comparison(state: InstanceStateDescription, key: AppliedStateKey) -> InstanceComparison | None:
    return next((item for item in state.comparisons if item.key == _evidence_key(key)), None)


def _insert_raw_record(
    db: Database,
    *,
    record_type: str,
    record_key: str,
    payload_version: int = 1,
    value_json: str = "{}",
    operation: str | None = None,
    ignore_checks: bool = False,
) -> None:
    if ignore_checks:
        db._conn.execute("PRAGMA ignore_check_constraints = ON")  # noqa: SLF001
    try:
        db._conn.execute(  # noqa: SLF001
            "INSERT INTO instance_records "
            "(instance_kind, instance_name, record_type, record_key, payload_version, "
            "value_json, recorded_at, operation) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "vm",
                "box",
                record_type,
                record_key,
                payload_version,
                value_json,
                "2026-08-29T12:00:00Z",
                operation,
            ),
        )
        db._conn.commit()  # noqa: SLF001
    finally:
        if ignore_checks:
            db._conn.execute("PRAGMA ignore_check_constraints = OFF")  # noqa: SLF001


def test_workspace_and_agent_descriptions_include_current_final_layers(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    from agentworks.agents.manager import agent_description
    from agentworks.instance_specs import parse_instance_spec
    from agentworks.workspaces.manager import workspace_description

    _seed_vm(db)
    db.insert_workspace("work", "/srv/work", "box", "ws-work")
    db.insert_agent("dev", "box", "agt-dev")
    workspace_overlay = parse_instance_spec("workspace", '{"tmuxinator":false}')
    agent_overlay = parse_instance_spec("agent", '{"shell":"/bin/fish"}')
    db.instance_state.put_desired_overlay("workspace", "work", workspace_overlay.payload)
    db.instance_state.put_desired_overlay("agent", "dev", agent_overlay.payload)

    config = make_config()
    workspace = workspace_description(db, config, "work")
    agent = agent_description(db, config, name="dev")

    workspace_slot = workspace.instance_state.declarations[0]
    assert workspace_slot.instance_spec.status == "present"
    assert workspace_slot.current.status == "resolved"
    assert workspace_slot.current.spec["tmuxinator"] is False
    assert workspace.instance_state.lifecycle_evidence == ()

    agent_slot = agent.instance_state.declarations[0]
    assert agent_slot.instance_spec.status == "present"
    assert agent_slot.current.status == "resolved"
    assert agent_slot.current.spec["shell"] == "/bin/fish"
    assert agent.instance_state.lifecycle_evidence == ()


def test_workspace_and_agent_descriptions_retain_database_facts_when_registry_is_unavailable(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    from agentworks.agents.manager import agent_description
    from agentworks.agents.manager.inspect import agent_description_data
    from agentworks.instance_description import InstanceStateIssueCode
    from agentworks.instance_specs import parse_instance_spec, parse_vm_instance_specs
    from agentworks.workspaces.manager import workspace_description
    from agentworks.workspaces.manager.create import workspace_description_data

    _seed_vm(db)
    db.insert_vm("bad-box", "proxmox", "bad-box")
    invalid_admin = parse_vm_instance_specs(None, '{"mise_install_before":"operator-value-marker"}')
    assert invalid_admin is not None
    db.instance_state.put_desired_overlay("vm", "bad-box", invalid_admin.payload)

    db.insert_workspace("work", "/srv/work", "box", "ws-work")
    db.insert_agent("dev", "box", "agt-dev")
    db.insert_agent_grant("dev", "work", "explicit")
    db.insert_session(
        "run",
        "work",
        "default",
        SessionMode.AGENT,
        agent_name="dev",
        socket_path="/tmp/run.sock",
    )
    workspace_overlay = parse_instance_spec("workspace", '{"tmuxinator":false}')
    agent_overlay = parse_instance_spec("agent", '{"shell":"/bin/fish"}')
    db.instance_state.put_desired_overlay("workspace", "work", workspace_overlay.payload)
    db.instance_state.put_desired_overlay("agent", "dev", agent_overlay.payload)

    config = make_config()
    workspace = workspace_description(db, config, "work")
    agent = agent_description(db, config, name="dev")

    assert workspace.workspace.name == "work"
    assert [session.name for session in workspace.sessions] == ["run"]
    assert [item.name for item in workspace.agents] == ["dev"]
    assert agent.name == "dev"
    assert agent.explicit_grants == ("work",)
    assert [session.name for session in agent.sessions] == ["run"]
    assert "operator-value-marker" not in json.dumps(workspace_description_data(workspace))
    assert "operator-value-marker" not in json.dumps(agent_description_data(agent))

    for state, expected_spec in (
        (workspace.instance_state, {"tmuxinator": False}),
        (agent.instance_state, {"shell": "/bin/fish"}),
    ):
        slot = state.declarations[0]
        assert slot.instance_spec.status == "present"
        assert slot.instance_spec.spec == expected_spec
        assert slot.current.status == "unresolved"
        assert slot.current.reason == "registry-unavailable"
        assert [(issue.code, issue.slot) for issue in state.issues] == [
            (InstanceStateIssueCode.REGISTRY_UNAVAILABLE, slot.name)
        ]


@pytest.mark.parametrize("error", [ConfigError("marker"), ValidationError("marker")])
def test_tolerant_registry_loader_degrades_only_expected_operator_data_errors(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    from unittest.mock import Mock

    from agentworks import bootstrap
    from agentworks.instance_description import (
        load_instance_description_registry,
        load_tolerant_instance_description_registry,
    )

    loader = Mock(side_effect=error)
    monkeypatch.setattr(bootstrap, "load_request_registry", loader)
    config = make_config()

    assert load_tolerant_instance_description_registry(db, config, "workspace", "work") is None
    with pytest.raises(type(error)) as raised:
        load_instance_description_registry(db, config, "vm", "box")
    assert raised.value is error


def test_tolerant_registry_loader_preserves_focused_owner_state_fallback(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from agentworks import bootstrap
    from agentworks.instance_description import load_tolerant_instance_description_registry
    from agentworks.resources import Registry

    fallback = Registry.empty()
    loader = Mock(
        side_effect=[
            StateError("marker", entity_kind="workspace", entity_name="work"),
            fallback,
        ]
    )
    monkeypatch.setattr(bootstrap, "load_request_registry", loader)

    registry = load_tolerant_instance_description_registry(db, make_config(), "workspace", "work")

    assert registry is fallback
    assert loader.call_count == 2


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("marker"),
        AssertionError("marker"),
        StateError("marker"),
        StateError("marker", entity_kind="database"),
    ],
)
def test_tolerant_registry_loader_propagates_unexpected_failures(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    from unittest.mock import Mock

    from agentworks import bootstrap
    from agentworks.instance_description import load_tolerant_instance_description_registry

    monkeypatch.setattr(bootstrap, "load_request_registry", Mock(side_effect=error))

    with pytest.raises(type(error)) as raised:
        load_tolerant_instance_description_registry(db, make_config(), "workspace", "work")
    assert raised.value is error


def test_workspace_description_retains_unavailable_stored_spec(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    from agentworks.workspaces.manager import workspace_description

    _seed_vm(db)
    db.insert_workspace("work", "/srv/work", "box", "ws-work")
    db.instance_state.put_desired_overlay("workspace", "work", VersionedPayload(9, {"tmuxinator": False}))

    description = workspace_description(db, make_config(), "work")

    slot = description.instance_state.declarations[0]
    assert slot.instance_spec.status == "unavailable"
    assert slot.instance_spec.reason == "unsupported-version"
    assert slot.current.status == "unresolved"
    assert slot.current.reason == "instance-spec-unavailable"
    assert description.instance_state.lifecycle_evidence == ()


def test_vm_and_admin_instance_specs_resolve_independently(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    from agentworks.instance_specs import parse_vm_instance_specs

    _seed_vm(db)
    overlays = parse_vm_instance_specs('{"cpus":6}', '{"shell":"zsh"}')
    assert overlays is not None
    db.instance_state.put_desired_overlay("vm", "box", overlays.payload)

    state, _ = _vm_state(db, make_config())

    assert [slot.name for slot in state.declarations] == ["vm", "admin"]
    vm_slot, admin_slot = state.declarations
    assert vm_slot.instance_spec.status == "present"
    assert vm_slot.current.status == "resolved"
    assert vm_slot.current.spec["cpus"] == 6
    assert admin_slot.instance_spec.status == "present"
    assert admin_slot.current.status == "resolved"
    assert admin_slot.current.spec["shell"] == "zsh"


def test_missing_selected_vm_template_leaves_admin_and_lifecycle_evidence_visible(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    from agentworks.vms.applied_state import encode_hardware_provenance

    _seed_vm(db, template="removed")
    db.instance_state.replace_applied_slices(
        "vm",
        "box",
        "vm-create",
        {AppliedStateKey.HARDWARE_PROVENANCE: encode_hardware_provenance()},
    )

    state, _ = _vm_state(db, make_config())

    vm_slot, admin_slot = state.declarations
    assert vm_slot.current.status == "unresolved"
    assert vm_slot.current.reason == "missing-selection"
    assert admin_slot.current.status == "resolved"
    assert _fact(state, AppliedStateKey.HARDWARE_PROVENANCE).status == "recorded"
    assert _comparison(state, AppliedStateKey.HARDWARE_PROVENANCE) is None


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (VersionedPayload(9, {"future": True}), "unsupported-version"),
        (VersionedPayload(2, {"admin": {}, "vm": 7}), "malformed"),
    ],
)
def test_unavailable_vm_desired_state_retains_applied_siblings(
    db: Database,
    make_config,  # noqa: ANN001
    payload: VersionedPayload,
    reason: str,
) -> None:
    from agentworks.vms.applied_state import encode_hardware_provenance

    _seed_vm(db)
    db.instance_state.put_desired_overlay("vm", "box", payload)
    db.instance_state.replace_applied_slices(
        "vm",
        "box",
        "vm-create",
        {AppliedStateKey.HARDWARE_PROVENANCE: encode_hardware_provenance()},
    )

    state, _ = _vm_state(db, make_config())

    assert [slot.instance_spec.reason for slot in state.declarations] == [reason, reason]
    assert [slot.current.status for slot in state.declarations] == ["unresolved", "unresolved"]
    assert _fact(state, AppliedStateKey.HARDWARE_PROVENANCE).status == "recorded"
    assert _fact(state, AppliedStateKey.SSH_IDENTITY).status == "not-recorded"


def test_malformed_desired_envelope_retains_applied_siblings(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    from agentworks.vms.applied_state import encode_hardware_provenance

    _seed_vm(db)
    _insert_raw_record(
        db,
        record_type="desired-overlay",
        record_key="spec",
        value_json="not-json",
    )
    db.instance_state.replace_applied_slices(
        "vm",
        "box",
        "vm-create",
        {AppliedStateKey.HARDWARE_PROVENANCE: encode_hardware_provenance()},
    )

    state, _ = _vm_state(db, make_config())

    assert [slot.instance_spec.reason for slot in state.declarations] == ["malformed", "malformed"]
    assert _fact(state, AppliedStateKey.HARDWARE_PROVENANCE).status == "recorded"
    assert any(issue.code.value == "instance-spec-malformed" for issue in state.issues)


def test_damaged_desired_record_key_is_unavailable_not_absent(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    _seed_vm(db)
    _insert_raw_record(
        db,
        record_type="desired-overlay",
        record_key="unsafe\nkey",
        ignore_checks=True,
    )

    state, _ = _vm_state(db, make_config())

    for declaration in state.declarations:
        assert declaration.instance_spec.status == "unavailable"
        assert declaration.instance_spec.reason == "malformed"
        assert declaration.current.status == "unresolved"
        assert declaration.current.reason == "instance-spec-unavailable"
    assert any(issue.code.value == "instance-spec-malformed" for issue in state.issues)


def test_damaged_desired_sibling_does_not_hide_valid_canonical_record(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    from agentworks.instance_specs import parse_vm_instance_specs

    _seed_vm(db)
    overlays = parse_vm_instance_specs('{"cpus":6}', None)
    assert overlays is not None
    db.instance_state.put_desired_overlay("vm", "box", overlays.payload)
    _insert_raw_record(
        db,
        record_type="desired-overlay",
        record_key="unsafe\nkey",
        ignore_checks=True,
    )

    state, _ = _vm_state(db, make_config())

    vm_slot = state.declarations[0]
    assert vm_slot.instance_spec.status == "present"
    assert vm_slot.current.status == "resolved"
    assert vm_slot.current.spec["cpus"] == 6
    assert any(issue.code.value == "instance-spec-malformed" for issue in state.issues)


@pytest.mark.parametrize(
    ("row_values", "marker", "expected_status", "expected_comparison", "difference_fields"),
    [
        ((4, 8, 50, 4), False, "not-recorded", "not-recorded", ()),
        ((4, 8, 50, 4), True, "recorded", "match", ()),
        ((6, 16, 50, 4), True, "recorded", "drift", ("cpus", "memory")),
        ((None, 8, 50, 4), True, "unavailable", None, ()),
    ],
)
def test_hardware_fact_matrix(
    db: Database,
    make_config,  # noqa: ANN001
    row_values: tuple[int | None, int | None, int | None, int | None],
    marker: bool,
    expected_status: str,
    expected_comparison: str | None,
    difference_fields: tuple[str, ...],
) -> None:
    from agentworks.vms.applied_state import encode_hardware_provenance

    _seed_vm(db, cpus=row_values[0], memory=row_values[1], disk=row_values[2], swap=row_values[3])
    if marker:
        db.instance_state.replace_applied_slices(
            "vm",
            "box",
            "vm-create",
            {AppliedStateKey.HARDWARE_PROVENANCE: encode_hardware_provenance()},
        )

    state, _ = _vm_state(db, make_config())

    assert _fact(state, AppliedStateKey.HARDWARE_PROVENANCE).status == expected_status
    comparison = _comparison(state, AppliedStateKey.HARDWARE_PROVENANCE)
    assert (None if comparison is None else comparison.state) == expected_comparison
    assert (() if comparison is None else tuple(item.field for item in comparison.differences)) == difference_fields


def test_hardware_request_match_does_not_claim_observed_provider_hardware_matches(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    from agentworks.instance_specs import parse_vm_instance_specs
    from agentworks.vms.applied_state import encode_hardware_provenance
    from agentworks.vms.manager.inspect import (
        VMDescription,
        VMDetailFacts,
        VMLiveResources,
        vm_description_data,
    )

    _seed_vm(db, cpus=6, memory=16)
    overlays = parse_vm_instance_specs('{"cpus":6,"memory":16}', None)
    assert overlays is not None
    db.instance_state.put_desired_overlay("vm", "box", overlays.payload)
    db.instance_state.replace_applied_slices(
        "vm",
        "box",
        "vm-create",
        {AppliedStateKey.HARDWARE_PROVENANCE: encode_hardware_provenance()},
    )
    state, _ = _vm_state(db, make_config())
    vm = db.get_vm("box")
    assert vm is not None
    description = VMDescription(
        VMDetailFacts.from_row(vm),
        None,
        None,
        None,
        None,
        None,
        "unset",
        VMLiveResources(
            "8",
            "0.1",
            "32 GiB",
            "1 GiB",
            "3%",
            "4 GiB",
            "0 B",
            "0%",
            "50 GiB",
            "5 GiB",
            "10%",
        ),
        (),
        (),
        (),
        (),
        (),
        state,
    )

    projected = vm_description_data(description)["vm"]
    assert isinstance(projected, dict)
    assert projected["provisioned_resources"] == {
        "cpus": 6,
        "memory_gib": 16,
        "disk_gib": 50,
        "swap_gib": 4,
    }
    assert projected["live_resources"] == {
        "cpus": "8",
        "load_average": "0.1",
        "memory_total": "32 GiB",
        "memory_used": "1 GiB",
        "memory_percent": "3%",
        "swap_total": "4 GiB",
        "swap_used": "0 B",
        "swap_percent": "0%",
        "disk_total": "50 GiB",
        "disk_used": "5 GiB",
        "disk_percent": "10%",
    }
    instance_state = projected["instance_state"]
    assert isinstance(instance_state, dict)
    evidence = instance_state["lifecycle_evidence"]
    assert isinstance(evidence, list)
    hardware = evidence[0]
    assert isinstance(hardware, dict)
    assert hardware["key"] == "hardware-request"
    comparisons = instance_state["comparisons"]
    assert isinstance(comparisons, list)
    assert comparisons[0] == {"key": "hardware-request", "state": "match"}


def test_hardware_marker_rejects_non_integer_database_value(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    from agentworks.vms.applied_state import encode_hardware_provenance

    _seed_vm(db)
    db._conn.execute("UPDATE vms SET cpus = 'damaged' WHERE name = 'box'")  # noqa: SLF001
    db._conn.commit()  # noqa: SLF001
    db.instance_state.replace_applied_slices(
        "vm",
        "box",
        "vm-create",
        {AppliedStateKey.HARDWARE_PROVENANCE: encode_hardware_provenance()},
    )

    state, _ = _vm_state(db, make_config())

    assert _fact(state, AppliedStateKey.HARDWARE_PROVENANCE).status == "unavailable"
    assert _comparison(state, AppliedStateKey.HARDWARE_PROVENANCE) is None
    assert any(issue.code.value == "lifecycle-evidence-unavailable" for issue in state.issues)


@pytest.mark.parametrize(
    ("key", "payload", "issue_code"),
    [
        (AppliedStateKey.HARDWARE_PROVENANCE, VersionedPayload(2, {}), "applied-record-unsupported"),
        (
            AppliedStateKey.SSH_IDENTITY,
            VersionedPayload(1, {"private_key_ref": "/key", "status": "verified"}),
            "applied-record-malformed",
        ),
    ],
)
def test_known_unreadable_applied_record_retains_its_sibling(
    db: Database,
    make_config,  # noqa: ANN001
    key: AppliedStateKey,
    payload: VersionedPayload,
    issue_code: str,
) -> None:
    from agentworks.ssh_identity import VerifiedSSHIdentity
    from agentworks.vms.applied_state import encode_hardware_provenance, encode_ssh_identity

    _seed_vm(db)
    sibling_key = (
        AppliedStateKey.SSH_IDENTITY
        if key is AppliedStateKey.HARDWARE_PROVENANCE
        else AppliedStateKey.HARDWARE_PROVENANCE
    )
    sibling_payload = (
        encode_ssh_identity("/key", VerifiedSSHIdentity(_FINGERPRINT))
        if sibling_key is AppliedStateKey.SSH_IDENTITY
        else encode_hardware_provenance()
    )
    db.instance_state.replace_applied_slices(
        "vm",
        "box",
        "vm-create",
        {key: payload, sibling_key: sibling_payload},
    )

    state, _ = _vm_state(db, make_config())

    assert _fact(state, key).status == "unavailable"
    assert _fact(state, sibling_key).status == "recorded"
    assert any(issue.code.value == issue_code and issue.record_key == key.value for issue in state.issues)


def test_future_record_type_and_applied_key_are_value_free_unconsumed_facts(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    _seed_vm(db)
    _insert_raw_record(
        db,
        record_type="applied-state",
        record_key="future-fact",
        operation="future-operation",
    )
    _insert_raw_record(db, record_type="future-state", record_key="future-key")

    state, _ = _vm_state(db, make_config())

    assert {(item.record_type, item.record_key) for item in state.unconsumed_records} == {
        ("applied-state", "future-fact"),
        ("future-state", "future-key"),
    }
    assert [fact.status for fact in state.lifecycle_evidence] == ["not-recorded", "not-recorded"]


def test_ssh_not_recorded_is_an_explicit_comparison(
    db: Database,
    make_config,  # noqa: ANN001
) -> None:
    _seed_vm(db)

    state, applied = _vm_state(db, make_config())

    assert applied is None
    assert _fact(state, AppliedStateKey.SSH_IDENTITY).status == "not-recorded"
    comparison = _comparison(state, AppliedStateKey.SSH_IDENTITY)
    assert comparison is not None
    assert comparison.state == "not-recorded"


@pytest.mark.parametrize(
    ("current_fingerprint", "expected"),
    [(_FINGERPRINT, "match"), (_OTHER_FINGERPRINT, "drift")],
)
def test_verified_ssh_comparison_matrix(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    current_fingerprint: str,
    expected: str,
) -> None:
    from agentworks.ssh_identity import VerifiedSSHIdentity
    from agentworks.vms.applied_state import encode_ssh_identity
    from agentworks.vms.manager.inspect import _add_current_ssh_comparison

    _seed_vm(db)
    db.instance_state.replace_applied_slices(
        "vm",
        "box",
        "vm-create",
        {AppliedStateKey.SSH_IDENTITY: encode_ssh_identity("/key", VerifiedSSHIdentity(_FINGERPRINT))},
    )
    config = make_config()
    state, applied = _vm_state(db, config)
    assert applied is not None
    monkeypatch.setattr(
        "agentworks.ssh_identity.read_private_ssh_identity",
        lambda path: VerifiedSSHIdentity(current_fingerprint),
    )

    compared = _add_current_ssh_comparison(state, "box", config, applied)

    comparison = _comparison(compared, AppliedStateKey.SSH_IDENTITY)
    assert comparison is not None
    assert comparison.state == expected


def test_unverifiable_applied_ssh_identity_never_reads_current_key(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.ssh_identity import UnverifiableSSHIdentity
    from agentworks.vms.applied_state import encode_ssh_identity
    from agentworks.vms.manager.inspect import _add_current_ssh_comparison

    _seed_vm(db)
    db.instance_state.replace_applied_slices(
        "vm",
        "box",
        "vm-create",
        {AppliedStateKey.SSH_IDENTITY: encode_ssh_identity("/legacy-key", UnverifiableSSHIdentity())},
    )
    config = make_config()
    state, applied = _vm_state(db, config)
    assert applied is not None
    monkeypatch.setattr(
        "agentworks.ssh_identity.read_private_ssh_identity",
        lambda path: pytest.fail(f"unverifiable evidence must not read {path}"),
    )

    compared = _add_current_ssh_comparison(state, "box", config, applied)

    comparison = _comparison(compared, AppliedStateKey.SSH_IDENTITY)
    assert comparison is not None
    assert comparison.state == "unverifiable"


def test_unavailable_current_ssh_identity_omits_comparison(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.ssh_identity import SSHIdentityReadError, VerifiedSSHIdentity
    from agentworks.vms.applied_state import encode_ssh_identity
    from agentworks.vms.manager.inspect import _add_current_ssh_comparison

    _seed_vm(db)
    db.instance_state.replace_applied_slices(
        "vm",
        "box",
        "vm-create",
        {AppliedStateKey.SSH_IDENTITY: encode_ssh_identity("/key", VerifiedSSHIdentity(_FINGERPRINT))},
    )
    config = make_config()
    state, applied = _vm_state(db, config)
    assert applied is not None

    def unavailable(path: Path) -> None:
        raise SSHIdentityReadError("unavailable", "test identity is unavailable")

    monkeypatch.setattr("agentworks.ssh_identity.read_private_ssh_identity", unavailable)

    compared = _add_current_ssh_comparison(state, "box", config, applied)

    assert _comparison(compared, AppliedStateKey.SSH_IDENTITY) is None
    assert any(issue.code.value == "current-identity-unavailable" for issue in compared.issues)


def test_instance_state_json_projects_literal_lifecycle_evidence_contract() -> None:
    from agentworks.instance_description import (
        ComparisonDifference,
        DeclarationSlot,
        InstanceComparison,
        InstanceSpec,
        InstanceStateDescription,
        LifecycleEvidence,
        instance_state_data,
    )
    from agentworks.resources.access import ResourceIdentity
    from agentworks.resources.resolved_spec import ResolvedSpec

    state = InstanceStateDescription(
        declarations=(
            DeclarationSlot(
                "vm",
                ResourceIdentity("vm-template", "default"),
                InstanceSpec("absent"),
                ResolvedSpec({"cpus": 8}, ()),
            ),
        ),
        lifecycle_evidence=(
            LifecycleEvidence(
                "hardware-request",
                "recorded",
                recorded_at="2026-08-30T12:00:00Z",
                operation="vm-create",
                value={"cpus": 6},
            ),
        ),
        comparisons=(
            InstanceComparison(
                "hardware-request",
                "drift",
                (ComparisonDifference("cpus", 6, 8),),
            ),
        ),
    )

    assert instance_state_data(state) == {
        "declarations": {
            "vm": {
                "selection": {"kind": "vm-template", "name": "default"},
                "instance_spec": {"status": "absent"},
                "current": {
                    "status": "resolved",
                    "spec": {"cpus": 8},
                    "provenance": [],
                },
            },
        },
        "lifecycle_evidence": [
            {
                "key": "hardware-request",
                "status": "recorded",
                "recorded_at": "2026-08-30T12:00:00Z",
                "operation": "vm-create",
                "value": {"cpus": 6},
            },
        ],
        "comparisons": [
            {
                "key": "hardware-request",
                "state": "drift",
                "differences": [{"field": "cpus", "recorded": 6, "current": 8}],
            },
        ],
        "unconsumed_records": [],
        "issues": [],
    }


def test_live_instance_human_facts_are_terminal_safe(
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.instance_description import (
        ComparisonDifference,
        DeclarationSlot,
        InstanceComparison,
        InstanceSpec,
        InstanceStateDescription,
        InstanceStateIssue,
        InstanceStateIssueCode,
        LifecycleEvidence,
        UnconsumedRecord,
        render_instance_state,
    )
    from agentworks.resources.access import ResourceIdentity
    from agentworks.resources.resolved_spec import (
        ResolvedPathProvenance,
        ResolvedSpec,
        ResolvedValueSource,
    )

    unsafe = "line\ncontrol\x1b\u202e"
    declaration = DeclarationSlot(
        name="slot",
        selection=ResourceIdentity(f"kind-{unsafe}", f"name-{unsafe}"),
        instance_spec=InstanceSpec(
            "present",
            recorded_at=f"time-{unsafe}",
            spec={f"key-{unsafe}": f"value-{unsafe}"},
        ),
        current=ResolvedSpec(
            spec={f"current-{unsafe}": f"resolved-{unsafe}"},
            provenance=(
                ResolvedPathProvenance(
                    (f"path-{unsafe}",),
                    (ResolvedValueSource("declared", f"source-kind-{unsafe}", f"source-name-{unsafe}"),),
                ),
            ),
        ),
    )
    state = InstanceStateDescription(
        declarations=(
            DeclarationSlot(
                f"slot-{unsafe}",
                declaration.selection,
                declaration.instance_spec,
                declaration.current,
            ),
        ),
        lifecycle_evidence=(
            LifecycleEvidence(
                f"fact-{unsafe}",
                "recorded",
                recorded_at=f"recorded-{unsafe}",
                operation=f"operation-{unsafe}",
                value={f"fact-key-{unsafe}": f"fact-value-{unsafe}"},
            ),
        ),
        comparisons=(
            InstanceComparison(
                f"comparison-{unsafe}",
                "drift",
                (ComparisonDifference(f"field-{unsafe}", f"recorded-{unsafe}", f"current-{unsafe}"),),
            ),
        ),
        unconsumed_records=(UnconsumedRecord(f"type-{unsafe}", f"record-{unsafe}", 9, f"future-time-{unsafe}"),),
        issues=(InstanceStateIssue(InstanceStateIssueCode.APPLIED_RECORD_MALFORMED, slot=f"issue-{unsafe}"),),
    )

    render_instance_state(state)

    unsafe_categories = {"Cc", "Cf", "Cs", "Zl", "Zp"}
    assert all(
        not any(unicodedata.category(character) in unsafe_categories for character in message)
        for message in captured_output.detail
    )


def test_live_instance_structured_values_render_as_ascii_json(
    captured_output,  # noqa: ANN001
) -> None:
    from agentworks.instance_description import _render_json_object

    value = {"ordinary-unicode": "snowman \u2603", "unsafe": "line\ncontrol\x1b\u202e"}

    _render_json_object(value)

    assert all(line.isascii() for line in captured_output.detail)
    assert json.loads("\n".join(captured_output.detail)) == value
