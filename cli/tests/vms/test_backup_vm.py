"""``backup_vm``'s precondition ordering: deterministic fatal checks
run before the composition root; ``gated_vm_boundary`` opens the gate
and runs the boundary resolve pass, which can prompt for site secrets,
and the operator must never answer a prompt for a backup the row
already sank.
"""

from __future__ import annotations

import os
import stat
from contextlib import nullcontext
from json import loads
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.db import VersionedPayload
from agentworks.errors import BackupError, StateError
from agentworks.instance_specs import parse_vm_instance_specs
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import backup as vm_backup

if TYPE_CHECKING:
    from agentworks.db import Database


def test_missing_tailscale_fails_before_the_boundary(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    db.insert_vm("bvm", site="lima-local", hostname="bvm")  # no tailscale

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

    class _FakeSSHTransport:
        pass

    class _FakeLogger:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.path: Path | None = None

        def close(self) -> None:
            pass

    monkeypatch.setattr(vm_backup, "gated_vm_boundary", lambda *a, **k: nullcontext())
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda config, **_kwargs: object())
    monkeypatch.setattr("agentworks.ssh.SSHLogger", _FakeLogger)
    monkeypatch.setattr("agentworks.transports.SSHTransport", _FakeSSHTransport)
    monkeypatch.setattr("agentworks.transports.transport", lambda *a, **k: _FakeSSHTransport())
    config = SimpleNamespace(paths=SimpleNamespace(backups=tmp_path))

    destination = vm_backup.backup_vm(
        db,
        config,  # type: ignore[arg-type]
        "bvm",
        interaction=TtyInteractionPolicy.REFUSE,
    )

    manifest = loads((destination / "manifest.json").read_text(encoding="utf-8"))
    specs = loads((destination / "instance-specs.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 3
    assert manifest["instance_spec_count"] == 1
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


def test_windows_refuses_overlay_backup_before_any_json_write(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db.insert_vm("bvm", site="lima-local", hostname="bvm")
    db.update_vm_tailscale("bvm", "100.64.0.8")
    overlays = parse_vm_instance_specs('{"env":{"TOKEN":"plaintext"}}', None)
    assert overlays is not None
    db.instance_state.put_desired_overlay("vm", "bvm", overlays.payload)

    class _FakeSSHTransport:
        pass

    class _FakeLogger:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.path: Path | None = None

        def close(self) -> None:
            pass

    monkeypatch.setattr(vm_backup, "gated_vm_boundary", lambda *a, **k: nullcontext())
    monkeypatch.setattr("agentworks.bootstrap.load_request_registry", lambda config: object())
    monkeypatch.setattr("agentworks.ssh.SSHLogger", _FakeLogger)
    monkeypatch.setattr("agentworks.transports.SSHTransport", _FakeSSHTransport)
    monkeypatch.setattr("agentworks.transports.transport", lambda *a, **k: _FakeSSHTransport())
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
