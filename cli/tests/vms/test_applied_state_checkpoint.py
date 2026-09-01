"""VM initialization checkpoints for hardware and SSH applied state."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from agentworks.db import AppliedStateKey, InitStatus
from agentworks.db.instance_state import InstanceStateRepository
from agentworks.debian import DebianRelease
from agentworks.errors import ConfigError
from agentworks.ssh_identity import VerifiedSSHIdentity, read_private_ssh_identity
from agentworks.vms.applied_state import (
    VerifiedSSHAppliedState,
    decode_hardware_provenance,
    decode_ssh_identity,
    encode_hardware_provenance,
    encode_ssh_identity,
)
from agentworks.vms.initializer.driver import VMInitializationOperation, run_initialization
from agentworks.vms.initializer.ssh_keys import (
    AuthorizedKeysApplied,
    AuthorizedKeysOutcome,
    AuthorizedKeysUnproven,
    _reconcile_authorized_keys,
)
from tests.ssh_fixtures import write_test_ssh_keypair

if TYPE_CHECKING:
    from collections.abc import Callable

_SSH_KEYGEN = shutil.which("ssh-keygen")


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    @property
    def has_warnings(self) -> bool:
        return bool(self.warnings)

    def warning(self, message: str) -> None:
        self.warnings.append(message)

    def step(self, message: str) -> None:
        pass


def _insert_vm(db) -> None:  # noqa: ANN001
    db.insert_vm("box", site="lima-local", hostname="box")


def _run(
    db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    operation: VMInitializationOperation,
    outcome: AuthorizedKeysOutcome,
    *,
    logger: _Logger | None = None,
    private_key: Path | None = None,
    phase_b: Callable[[], AuthorizedKeysOutcome] | None = None,
) -> _Logger:
    private = private_key or tmp_path / "id_ed25519"
    if not private.exists():
        write_test_ssh_keypair(private)
    config = SimpleNamespace(
        operator=SimpleNamespace(
            ssh_public_key=private.with_name(f"{private.name}.pub"),
            ssh_private_key=private,
        )
    )

    def run_phase_b(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return phase_b() if phase_b is not None else outcome

    monkeypatch.setattr("agentworks.vms.initializer.driver._phase_b_setup", run_phase_b)
    active_logger = logger or _Logger()
    run_initialization(
        db,
        config,
        MagicMock(),
        MagicMock(),
        MagicMock(),
        "box",
        MagicMock(),
        (),
        "/home/agentworks",
        "agentworks",
        active_logger,
        debian_release=DebianRelease.TRIXIE,
        operation=operation,
    )
    return active_logger


def test_create_checkpoints_hardware_and_verified_ssh_atomically(
    db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _insert_vm(db)
    private = tmp_path / "id_ed25519"
    write_test_ssh_keypair(private)
    identity = read_private_ssh_identity(private)
    assert isinstance(identity, VerifiedSSHIdentity)

    _run(
        db,
        monkeypatch,
        tmp_path,
        VMInitializationOperation.VM_CREATE,
        AuthorizedKeysApplied(identity, str(private)),
    )

    records = {record.key: record for record in db.instance_state.get_applied_slices("vm", "box")}
    decode_hardware_provenance(records[AppliedStateKey.HARDWARE_PROVENANCE])
    applied = decode_ssh_identity(records[AppliedStateKey.SSH_IDENTITY])
    assert isinstance(applied, VerifiedSSHAppliedState)
    assert applied.fingerprint == identity.fingerprint
    assert {record.operation for record in records.values()} == {"vm-create"}
    assert db.get_vm("box").init_status == InitStatus.COMPLETE.value


@pytest.mark.skipif(_SSH_KEYGEN is None, reason="ssh-keygen is not installed")
@pytest.mark.parametrize(
    "operation",
    [VMInitializationOperation.VM_CREATE, VMInitializationOperation.VM_REINIT],
)
def test_password_protected_native_key_is_checkpointed_for_create_and_reinit(
    db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: VMInitializationOperation,
) -> None:
    _insert_vm(db)
    private = tmp_path / "id_protected"
    subprocess.run(
        [
            _SSH_KEYGEN or "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "test-passphrase",
            "-f",
            str(private),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    identity = read_private_ssh_identity(private)
    assert isinstance(identity, VerifiedSSHIdentity)
    config = SimpleNamespace(
        operator=SimpleNamespace(
            ssh_public_key=private.with_suffix(".pub"),
            ssh_private_key=private,
            extra_ssh_public_keys=[],
        )
    )
    outcome = _reconcile_authorized_keys(MagicMock(), config, "/home/agentworks", _Logger())
    assert isinstance(outcome, AuthorizedKeysApplied)
    if operation is VMInitializationOperation.VM_REINIT:
        db.instance_state.replace_applied_slices(
            "vm",
            "box",
            "vm-create",
            {AppliedStateKey.HARDWARE_PROVENANCE: encode_hardware_provenance()},
        )

    _run(
        db,
        monkeypatch,
        tmp_path,
        operation,
        outcome,
        private_key=private,
    )

    records = {record.key: record for record in db.instance_state.get_applied_slices("vm", "box")}
    applied = decode_ssh_identity(records[AppliedStateKey.SSH_IDENTITY])
    assert isinstance(applied, VerifiedSSHAppliedState)
    assert applied.fingerprint == identity.fingerprint


def test_unrelated_warning_keeps_proven_slices_and_records_partial_init(
    db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _insert_vm(db)
    private = tmp_path / "id_ed25519"
    write_test_ssh_keypair(private)
    identity = read_private_ssh_identity(private)
    assert isinstance(identity, VerifiedSSHIdentity)
    logger = _Logger()
    logger.warning("unrelated setup warning")

    _run(
        db,
        monkeypatch,
        tmp_path,
        VMInitializationOperation.VM_CREATE,
        AuthorizedKeysApplied(identity, str(private)),
        logger=logger,
    )

    records = {record.key for record in db.instance_state.get_applied_slices("vm", "box")}
    assert records == {AppliedStateKey.HARDWARE_PROVENANCE, AppliedStateKey.SSH_IDENTITY}
    assert db.get_vm("box").init_status == InitStatus.PARTIAL.value
    assert [event.event for event in db.list_vm_events("box")] == ["init_started", "init_partial"]


def test_reinit_replaces_only_ssh_and_preserves_hardware_marker(
    db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _insert_vm(db)
    db.instance_state.replace_applied_slices(
        "vm",
        "box",
        "vm-create",
        {
            AppliedStateKey.HARDWARE_PROVENANCE: encode_hardware_provenance(),
            AppliedStateKey.SSH_IDENTITY: encode_ssh_identity(
                "/old/key",
                VerifiedSSHIdentity("SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
            ),
        },
    )
    before = {record.key: record for record in db.instance_state.get_applied_slices("vm", "box")}
    private = tmp_path / "id_ed25519"
    write_test_ssh_keypair(private)
    identity = read_private_ssh_identity(private)
    assert isinstance(identity, VerifiedSSHIdentity)

    _run(
        db,
        monkeypatch,
        tmp_path,
        VMInitializationOperation.VM_REINIT,
        AuthorizedKeysApplied(identity, str(private)),
    )

    after = {record.key: record for record in db.instance_state.get_applied_slices("vm", "box")}
    assert after[AppliedStateKey.HARDWARE_PROVENANCE] == before[AppliedStateKey.HARDWARE_PROVENANCE]
    assert after[AppliedStateKey.SSH_IDENTITY].operation == "vm-reinit"


def test_unproven_reinit_clears_prior_ssh_evidence(
    db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    captured_output,
) -> None:
    _insert_vm(db)
    db.instance_state.replace_applied_slices(
        "vm",
        "box",
        "vm-create",
        {
            AppliedStateKey.HARDWARE_PROVENANCE: encode_hardware_provenance(),
            AppliedStateKey.SSH_IDENTITY: encode_ssh_identity(
                "/old/key",
                VerifiedSSHIdentity("SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
            ),
        },
    )
    logger = _Logger()
    logger.warning("remote key write was not proven")

    _run(
        db,
        monkeypatch,
        tmp_path,
        VMInitializationOperation.VM_REINIT,
        AuthorizedKeysUnproven(),
        logger=logger,
    )

    records = {record.key for record in db.instance_state.get_applied_slices("vm", "box")}
    assert records == {AppliedStateKey.HARDWARE_PROVENANCE}
    assert db.get_vm("box").init_status == InitStatus.PARTIAL.value
    assert len(captured_output.warnings) == 1


def test_fatal_phase_b_failure_preserves_prior_applied_evidence(
    db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _insert_vm(db)
    db.update_vm_init_status("box", InitStatus.IN_PROGRESS)
    db.instance_state.replace_applied_slices(
        "vm",
        "box",
        "vm-create",
        {
            AppliedStateKey.HARDWARE_PROVENANCE: encode_hardware_provenance(),
            AppliedStateKey.SSH_IDENTITY: encode_ssh_identity(
                "/old/key",
                VerifiedSSHIdentity("SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
            ),
        },
    )
    before = db.instance_state.get_applied_slices("vm", "box")

    def fail_phase_b() -> AuthorizedKeysOutcome:
        raise RuntimeError("fatal setup failure")

    with pytest.raises(RuntimeError):
        _run(
            db,
            monkeypatch,
            tmp_path,
            VMInitializationOperation.VM_REINIT,
            AuthorizedKeysUnproven(),
            phase_b=fail_phase_b,
        )

    assert db.get_vm("box").init_status == InitStatus.FAILED.value
    assert [event.event for event in db.list_vm_events("box")] == ["init_started", "init_failed"]
    assert db.instance_state.get_applied_slices("vm", "box") == before


@pytest.mark.skipif(_SSH_KEYGEN is None, reason="ssh-keygen is not installed")
def test_public_private_mismatch_refuses_before_any_remote_write(
    tmp_path: Path,
) -> None:
    first = tmp_path / "id_first"
    second = tmp_path / "id_second"
    for private in (first, second):
        subprocess.run(
            [_SSH_KEYGEN or "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(private)],
            check=True,
            capture_output=True,
            text=True,
        )
    config = SimpleNamespace(
        operator=SimpleNamespace(
            ssh_public_key=first.with_suffix(".pub"),
            ssh_private_key=second,
            extra_ssh_public_keys=[],
        )
    )
    target = MagicMock()

    with pytest.raises(ConfigError):
        _reconcile_authorized_keys(target, config, "/home/agentworks", _Logger())

    target.write_file.assert_not_called()
    target.run.assert_not_called()


def test_same_path_post_write_instability_clears_prior_ssh_evidence(
    db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _insert_vm(db)
    db.instance_state.replace_applied_slices(
        "vm",
        "box",
        "vm-create",
        {
            AppliedStateKey.HARDWARE_PROVENANCE: encode_hardware_provenance(),
            AppliedStateKey.SSH_IDENTITY: encode_ssh_identity(
                "/old/key",
                VerifiedSSHIdentity("SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
            ),
        },
    )
    private = tmp_path / "id_ed25519"
    write_test_ssh_keypair(private)
    written_identity = read_private_ssh_identity(private)
    assert isinstance(written_identity, VerifiedSSHIdentity)
    replacement = VerifiedSSHIdentity(f"SHA256:{'E' * 43}")
    monkeypatch.setattr("agentworks.vms.applied_state.read_private_ssh_identity", lambda path: replacement)

    logger = _run(
        db,
        monkeypatch,
        tmp_path,
        VMInitializationOperation.VM_REINIT,
        AuthorizedKeysApplied(written_identity, str(private)),
        private_key=private,
    )

    records = {record.key for record in db.instance_state.get_applied_slices("vm", "box")}
    assert records == {AppliedStateKey.HARDWARE_PROVENANCE}
    assert logger.has_warnings
    assert db.get_vm("box").init_status == InitStatus.PARTIAL.value


def test_terminal_checkpoint_failure_rolls_back_status_event_and_slices(
    db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _insert_vm(db)
    db.update_vm_init_status("box", InitStatus.IN_PROGRESS)
    db.instance_state.replace_applied_slices(
        "vm",
        "box",
        "vm-create",
        {
            AppliedStateKey.HARDWARE_PROVENANCE: encode_hardware_provenance(),
            AppliedStateKey.SSH_IDENTITY: encode_ssh_identity(
                "/old/key",
                VerifiedSSHIdentity("SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
            ),
        },
    )
    before = db.instance_state.get_applied_slices("vm", "box")
    private = tmp_path / "id_ed25519"
    write_test_ssh_keypair(private)
    identity = read_private_ssh_identity(private)
    assert isinstance(identity, VerifiedSSHIdentity)
    original_replace = InstanceStateRepository.replace_applied_slices

    def replace_then_fail(repository, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        original_replace(repository, *args, **kwargs)
        raise RuntimeError("checkpoint failed")

    monkeypatch.setattr(InstanceStateRepository, "replace_applied_slices", replace_then_fail)

    with pytest.raises(RuntimeError):
        _run(
            db,
            monkeypatch,
            tmp_path,
            VMInitializationOperation.VM_CREATE,
            AuthorizedKeysApplied(identity, str(private)),
        )

    assert db.get_vm("box").init_status == InitStatus.IN_PROGRESS.value
    assert [event.event for event in db.list_vm_events("box")] == ["init_started"]
    assert db.instance_state.get_applied_slices("vm", "box") == before
