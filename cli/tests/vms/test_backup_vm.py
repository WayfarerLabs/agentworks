"""``backup_vm``'s precondition ordering: deterministic fatal checks
run before the composition root; ``gated_vm_boundary`` opens the gate
and runs the boundary resolve pass, which can prompt for site secrets,
and the operator must never answer a prompt for a backup the row
already sank.
"""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager, nullcontext
from json import loads
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.db import AppliedStateKey, ProvisioningStatus, VersionedPayload
from agentworks.errors import BackupError, StateError
from agentworks.execution.results import Completed, Result
from agentworks.instance_specs import parse_vm_instance_specs
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import backup as vm_backup
from agentworks.vms.manager import boundary
from tests.conftest import seed_vm_trust
from tests.vm_access_support import install_access_recorder

if TYPE_CHECKING:
    from agentworks.db import Database
    from tests.vm_access_support import AccessRecorder


@pytest.fixture(autouse=True)
def _backed_up_vm_has_a_host_key_record() -> None:
    seed_vm_trust("bvm")


def test_a_vm_without_a_host_key_record_is_refused_before_anything_is_written(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    from agentworks.execution import trust
    from agentworks.execution.errors import TrustMissing

    db.insert_vm("bvm", site="lima-local", hostname="bvm")
    db.update_vm_provisioning_status("bvm", ProvisioningStatus.COMPLETE)
    db.update_vm_tailscale("bvm", "100.64.0.9")
    trust.remove("bvm")
    install_access_recorder(monkeypatch)
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda config, **_kwargs: object())
    # The real gate's SSH precondition refuses; the gate itself must never open.
    monkeypatch.setattr(
        boundary, "_gated_vm_boundary", lambda *a, **k: pytest.fail("the gate opened for a VM with no record")
    )

    with pytest.raises(TrustMissing):
        vm_backup.backup_vm(db, SimpleNamespace(), "bvm", interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]


@pytest.mark.parametrize("status", [ProvisioningStatus.FAILED, ProvisioningStatus.IN_PROGRESS])
def test_a_vm_whose_provisioning_did_not_complete_is_refused_before_the_boundary(
    db: Database, monkeypatch: pytest.MonkeyPatch, status: ProvisioningStatus
) -> None:
    db.insert_vm("bvm", site="lima-local", hostname="bvm")
    db.update_vm_tailscale("bvm", "100.64.0.9")
    db.update_vm_provisioning_status("bvm", status)
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda *a, **k: pytest.fail("registry loaded"))
    monkeypatch.setattr(vm_backup, "gated_vm_boundary", lambda *a, **k: pytest.fail("the boundary opened"))

    with pytest.raises(StateError):
        vm_backup.backup_vm(db, SimpleNamespace(), "bvm", interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]


def test_missing_tailscale_fails_before_the_boundary(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    db.insert_vm("bvm", site="lima-local", hostname="bvm")  # no tailscale
    db.update_vm_provisioning_status("bvm", ProvisioningStatus.COMPLETE)

    def _no_boundary(*args: object, **kwargs: object) -> None:
        raise AssertionError("the boundary opened (and possibly prompted) before the guard")

    monkeypatch.setattr(vm_backup, "gated_vm_boundary", _no_boundary)

    with pytest.raises(StateError, match="no Tailscale address"):
        vm_backup.backup_vm(db, object(), "bvm", interaction=TtyInteractionPolicy.REFUSE)  # type: ignore[arg-type]


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_backup_directory_and_json_are_owner_only(tmp_path: Path) -> None:
    destination = tmp_path / "backup"
    vm_backup._create_backup_directory(destination, "bvm")  # noqa: SLF001
    vm_backup._write_json(destination / "instance-specs.json", {"environment": "plaintext"})  # noqa: SLF001

    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert stat.S_IMODE((destination / "instance-specs.json").stat().st_mode) == 0o600


def test_backup_directory_refuses_a_collision(tmp_path: Path) -> None:
    destination = tmp_path / "backup"
    destination.mkdir()

    with pytest.raises(BackupError):
        vm_backup._create_backup_directory(destination, "bvm")  # noqa: SLF001


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink protection")
def test_backup_json_refuses_existing_and_symlink_paths(tmp_path: Path) -> None:
    existing = tmp_path / "existing.json"
    existing.write_text("preserve", encoding="utf-8")
    with pytest.raises(FileExistsError):
        vm_backup._write_json(existing, {})  # noqa: SLF001
    assert existing.read_text(encoding="utf-8") == "preserve"

    target = tmp_path / "target.json"
    target.write_text("preserve target", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(FileExistsError):
        vm_backup._write_json(link, {})  # noqa: SLF001
    assert target.read_text(encoding="utf-8") == "preserve target"


def _install_metadata_only_backup_fakes(monkeypatch: pytest.MonkeyPatch) -> AccessRecorder:
    monkeypatch.setattr(vm_backup, "gated_vm_boundary", lambda *a, **k: nullcontext())
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda config, **_kwargs: object())
    return install_access_recorder(monkeypatch)


def _replace_ssh_payload_with_malformed_known_value(db: Database, vm_name: str) -> None:
    db.instance_state.replace_applied_slices(
        "vm",
        vm_name,
        "vm-create",
        {
            AppliedStateKey.SSH_IDENTITY: VersionedPayload(
                1,
                {
                    "fingerprint": f"SHA256:{'A' * 43}",
                    "private_key_ref": "/keys/operator",
                    "status": "verified",
                },
            )
        },
    )
    db._conn.execute(  # noqa: SLF001
        "UPDATE instance_records SET value_json = ? "
        "WHERE instance_kind = 'vm' AND instance_name = ? "
        "AND record_type = 'applied-state' AND record_key = 'ssh-identity'",
        ('{"status":"verified"}', vm_name),
    )
    db._conn.commit()  # noqa: SLF001


def test_backup_refuses_a_malformed_selected_applied_payload(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.insert_vm("bvm", site="lima-local", hostname="bvm")
    db.update_vm_provisioning_status("bvm", ProvisioningStatus.COMPLETE)
    db.update_vm_tailscale("bvm", "100.64.0.8")
    _replace_ssh_payload_with_malformed_known_value(db, "bvm")
    _install_metadata_only_backup_fakes(monkeypatch)
    config = SimpleNamespace(paths=SimpleNamespace(backups=tmp_path))

    with pytest.raises(StateError):
        vm_backup.backup_vm(
            db,
            config,  # type: ignore[arg-type]
            "bvm",
            interaction=TtyInteractionPolicy.REFUSE,
        )


def test_backup_refuses_an_unsupported_selected_applied_payload_version(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.insert_vm("bvm", site="lima-local", hostname="bvm")
    db.update_vm_provisioning_status("bvm", ProvisioningStatus.COMPLETE)
    db.update_vm_tailscale("bvm", "100.64.0.8")
    db.instance_state.replace_applied_slices(
        "vm",
        "bvm",
        "vm-create",
        {AppliedStateKey.HARDWARE_PROVENANCE: VersionedPayload(2, {})},
    )
    _install_metadata_only_backup_fakes(monkeypatch)
    config = SimpleNamespace(paths=SimpleNamespace(backups=tmp_path))

    with pytest.raises(StateError) as caught:
        vm_backup.backup_vm(
            db,
            config,  # type: ignore[arg-type]
            "bvm",
            interaction=TtyInteractionPolicy.REFUSE,
        )

    assert isinstance(caught.value, StateError)
    assert caught.value.entity_kind == "vm"
    assert caught.value.entity_name == "bvm"
    assert caught.value.hint is not None


def test_backup_does_not_decode_an_unrelated_malformed_applied_payload(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.insert_vm("bvm", site="lima-local", hostname="bvm")
    db.update_vm_provisioning_status("bvm", ProvisioningStatus.COMPLETE)
    db.update_vm_tailscale("bvm", "100.64.0.8")
    db.instance_state.replace_applied_slices(
        "vm",
        "bvm",
        "vm-create",
        {AppliedStateKey.HARDWARE_PROVENANCE: VersionedPayload(1, {})},
    )
    db.insert_vm("other", site="lima-local", hostname="other")
    _replace_ssh_payload_with_malformed_known_value(db, "other")
    _install_metadata_only_backup_fakes(monkeypatch)
    config = SimpleNamespace(paths=SimpleNamespace(backups=tmp_path))

    destination = vm_backup.backup_vm(
        db,
        config,  # type: ignore[arg-type]
        "bvm",
        interaction=TtyInteractionPolicy.REFUSE,
    )

    applied = loads((destination / "instance-applied-state.json").read_text(encoding="utf-8"))
    assert [(record["instance_name"], record["key"]) for record in applied] == [("bvm", "hardware-provenance")]


@pytest.mark.parametrize("legacy", [False, True], ids=("composite-v2", "legacy-flat-v1"))
@pytest.mark.skipif(os.name == "nt", reason="native Windows intentionally refuses overlay-bearing backups")
def test_vm_backup_exports_versioned_instance_specs(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    legacy: bool,
) -> None:
    db.insert_vm("bvm", site="lima-local", hostname="bvm")
    db.update_vm_provisioning_status("bvm", ProvisioningStatus.COMPLETE)
    db.update_vm_tailscale("bvm", "100.64.0.8")
    if legacy:
        payload = VersionedPayload(1, {"env": {"TOKEN": {"value": "plaintext"}}})
    else:
        overlays = parse_vm_instance_specs(
            '{"env":{"TOKEN":"plaintext"}}',
            '{"env":{"ADMIN_TOKEN":{"secret":"admin-token"}}}',
        )
        assert overlays is not None
        payload = overlays.payload
    db.instance_state.put_desired_overlay("vm", "bvm", payload)
    db.instance_state.replace_applied_slices(
        "vm",
        "bvm",
        "vm-create",
        {
            AppliedStateKey.HARDWARE_PROVENANCE: VersionedPayload(1, {}),
            AppliedStateKey.SSH_IDENTITY: VersionedPayload(
                1,
                {
                    "fingerprint": f"SHA256:{'A' * 43}",
                    "private_key_ref": "/keys/operator",
                    "status": "verified",
                },
            ),
        },
    )

    _install_metadata_only_backup_fakes(monkeypatch)
    config = SimpleNamespace(paths=SimpleNamespace(backups=tmp_path))

    destination = vm_backup.backup_vm(
        db,
        config,  # type: ignore[arg-type]
        "bvm",
        interaction=TtyInteractionPolicy.REFUSE,
    )

    manifest = loads((destination / "manifest.json").read_text(encoding="utf-8"))
    specs = loads((destination / "instance-specs.json").read_text(encoding="utf-8"))
    applied = loads((destination / "instance-applied-state.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 5
    assert manifest["instance_spec_count"] == 1
    assert manifest["applied_state_count"] == 2
    expected_value = (
        {"env": {"TOKEN": {"value": "plaintext"}}}
        if legacy
        else {
            "vm": {"env": {"TOKEN": {"value": "plaintext"}}},
            "admin": {"env": {"ADMIN_TOKEN": {"secret": "admin-token"}}},
        }
    )
    assert specs == [
        {
            "instance_kind": "vm",
            "instance_name": "bvm",
            "payload_version": 1 if legacy else 2,
            "value": expected_value,
            "recorded_at": specs[0]["recorded_at"],
        }
    ]
    assert applied == [
        {
            "instance_kind": "vm",
            "instance_name": "bvm",
            "key": "hardware-provenance",
            "payload_version": 1,
            "value": {},
            "operation": "vm-create",
            "recorded_at": applied[0]["recorded_at"],
        },
        {
            "instance_kind": "vm",
            "instance_name": "bvm",
            "key": "ssh-identity",
            "payload_version": 1,
            "value": {
                "fingerprint": f"SHA256:{'A' * 43}",
                "private_key_ref": "/keys/operator",
                "status": "verified",
            },
            "operation": "vm-create",
            "recorded_at": applied[1]["recorded_at"],
        },
    ]


def test_vm_backup_uses_post_activation_start_observation(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.insert_vm("bvm", site="lima-local", hostname="bvm")
    db.update_vm_provisioning_status("bvm", ProvisioningStatus.COMPLETE)
    db.update_vm_tailscale("bvm", "100.64.0.8")
    _install_metadata_only_backup_fakes(monkeypatch)

    @contextmanager
    def _activating_boundary(*args: object, **kwargs: object):  # noqa: ANN202
        db.record_vm_started("bvm")
        yield

    monkeypatch.setattr(vm_backup, "gated_vm_boundary", _activating_boundary)
    config = SimpleNamespace(paths=SimpleNamespace(backups=tmp_path))

    destination = vm_backup.backup_vm(
        db,
        config,  # type: ignore[arg-type]
        "bvm",
        interaction=TtyInteractionPolicy.REFUSE,
    )

    manifest = loads((destination / "manifest.json").read_text(encoding="utf-8"))
    vm = loads((destination / "vm.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 5
    assert vm["last_started_at"] is not None


def test_windows_refuses_overlay_backup_before_any_json_write(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.insert_vm("bvm", site="lima-local", hostname="bvm")
    db.update_vm_provisioning_status("bvm", ProvisioningStatus.COMPLETE)
    db.update_vm_tailscale("bvm", "100.64.0.8")
    overlays = parse_vm_instance_specs('{"env":{"TOKEN":"plaintext"}}', None)
    assert overlays is not None
    db.instance_state.put_desired_overlay("vm", "bvm", overlays.payload)

    _install_metadata_only_backup_fakes(monkeypatch)
    monkeypatch.setattr(vm_backup, "_host_supports_private_backup_permissions", lambda: False)
    monkeypatch.setattr(vm_backup, "_write_json", lambda *a, **k: pytest.fail("JSON write reached"))
    config = SimpleNamespace(paths=SimpleNamespace(backups=tmp_path))

    with pytest.raises(BackupError):
        vm_backup.backup_vm(
            db,
            config,  # type: ignore[arg-type]
            "bvm",
            interaction=TtyInteractionPolicy.REFUSE,
        )


def test_windows_allows_backup_with_only_nonsecret_applied_state(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.insert_vm("bvm", site="lima-local", hostname="bvm")
    db.update_vm_provisioning_status("bvm", ProvisioningStatus.COMPLETE)
    db.update_vm_tailscale("bvm", "100.64.0.8")
    db.instance_state.replace_applied_slices(
        "vm",
        "bvm",
        "vm-create",
        {AppliedStateKey.HARDWARE_PROVENANCE: VersionedPayload(1, {})},
    )

    _install_metadata_only_backup_fakes(monkeypatch)
    monkeypatch.setattr(vm_backup, "_host_supports_private_backup_permissions", lambda: False)
    config = SimpleNamespace(paths=SimpleNamespace(backups=tmp_path))

    destination = vm_backup.backup_vm(
        db,
        config,  # type: ignore[arg-type]
        "bvm",
        interaction=TtyInteractionPolicy.REFUSE,
    )

    manifest = loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["instance_spec_count"] == 0
    assert manifest["applied_state_count"] == 1


def test_native_setup_export_and_database_restore_preserve_owner_tree(db, tmp_path, monkeypatch):
    from agentworks.db import AppliedStateSlice, Database, create_manual_backup, restore_backup
    from agentworks.harness_setup.model import NativeClaim, NativeSetupState, SetupRecord
    from agentworks.harness_setup.state import decode_native_setup, read_native_setup, write_native_setup

    db.insert_vm("bvm", site="lima-local", hostname="bvm")
    db.update_vm_provisioning_status("bvm", ProvisioningStatus.COMPLETE)
    db.update_vm_tailscale("bvm", "100.64.0.8")
    db.insert_agent("a", "bvm", "agt-a")
    db.insert_workspace("w", "/work/w", "bvm", "aw-w")
    db.insert_vm("other", site="lima-local", hostname="other")
    expected = {}
    for kind, name, components in (
        ("vm", "bvm", ("vm", "admin")),
        ("agent", "a", ("agent",)),
        ("workspace", "w", ("workspace",)),
        ("vm", "other", ("vm",)),
    ):
        state = NativeSetupState(
            records=tuple(
                SetupRecord(
                    component=component,
                    integration="shell",
                    destination_id="a" * 64,
                    declaration={"config": {"name": "shell"}},
                    complete=True,
                    claims=(NativeClaim(role="settings", identifier="native", destination=f"/{name}"),),
                )
                for component in components
            )
        )
        write_native_setup(db, kind, name, state, operation="fixture")
        if name != "other":
            expected[kind, name] = state
    recorder = _install_metadata_only_backup_fakes(monkeypatch)
    recorder.access().default_outcome = Result(Completed(1))
    monkeypatch.setattr(vm_backup, "_archive_workspaces", lambda *a: ([], []))
    destination = vm_backup.backup_vm(
        db,
        SimpleNamespace(paths=SimpleNamespace(backups=tmp_path / "exports")),
        "bvm",
        interaction=TtyInteractionPolicy.REFUSE,
    )
    exported = loads((destination / "instance-applied-state.json").read_text())
    decoded = {}
    for value in exported:
        record = AppliedStateSlice(
            value["instance_kind"],
            value["instance_name"],
            AppliedStateKey(value["key"]),
            VersionedPayload(value["payload_version"], value["value"]),
            value["operation"],
            value["recorded_at"],
        )
        decoded[record.instance_kind, record.instance_name] = decode_native_setup(record)
    assert decoded == expected

    snapshot = create_manual_backup(db.path)
    restored_path = tmp_path / "restored.db"
    restore_backup(snapshot, restored_path)
    restored = Database(restored_path)
    try:
        assert {(kind, name): read_native_setup(restored, kind, name) for kind, name in expected} == expected
    finally:
        restored.close()
