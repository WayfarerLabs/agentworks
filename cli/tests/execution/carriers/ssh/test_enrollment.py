"""Enrollment persistence and strict recovery, including owned installed-client fixtures."""

from __future__ import annotations

import json
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.execution.carrier import CapturedOutput, CarrierIO, Deadline, Failure
from agentworks.execution.carriers._subprocess import ProcessResult
from agentworks.execution.carriers.ssh import _trust_files as files
from agentworks.execution.carriers.ssh import enrollment
from agentworks.execution.carriers.ssh._io import run_process
from agentworks.execution.carriers.ssh.connection import SSHConnection, admit_connection
from agentworks.execution.carriers.ssh.enrollment import (
    SSHCreationProvenance,
    SSHEnrollmentError,
    enroll_new_target,
    recover_enrollment,
)
from agentworks.execution.carriers.ssh.trust import (
    ManagedSSHTrust,
    SSHTrustFiles,
    block_trust,
    import_trust,
    refresh_trust,
    resolve_trust,
    trust_status,
)
from tests.execution.carriers.ssh.enrollment_server import LocalSSH

pytestmark = pytest.mark.windows


@dataclass
class SyntheticEnrollment:
    connection: SSHConnection
    provenance: SSHCreationProvenance
    calls: list[list[str]]
    action: Callable[[list[str]], ProcessResult] | None = None

    def run(self, argv: list[str], *, io: CarrierIO, deadline: Deadline) -> ProcessResult:
        self.calls.append(argv)
        if self.action is not None:
            return self.action(argv)
        return self.ack(argv)

    def ack(self, argv: list[str]) -> ProcessResult:
        nonce = re.search(r"agw-enroll-[a-f0-9]{32}", argv[-1])
        assert nonce is not None
        return ProcessResult(True, 0, 0, CapturedOutput((nonce.group() + "\n").encode(), True), CapturedOutput(), None)

    @property
    def bundle(self) -> ManagedSSHTrust:
        assert isinstance(self.connection.trust, ManagedSSHTrust)
        return self.connection.trust

    @property
    def directory(self) -> Path:
        return next(self.bundle.directory.glob("enrollment-*"))

    def enroll(self) -> enrollment.SSHEnrollmentCandidate:
        return enroll_new_target(self.connection, provenance=self.provenance, deadline=Deadline.after(5))

    def recover(self) -> enrollment.SSHEnrollmentCandidate:
        return recover_enrollment(self.connection, provenance=self.provenance, deadline=Deadline.after(5))


@pytest.fixture
def synthetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SyntheticEnrollment:
    tmp_path = tmp_path.resolve()
    identity = tmp_path / "identity"
    identity.write_bytes(b"synthetic private identity")
    trust = tmp_path / "trust"
    trust.write_bytes(b"complete existing policy\n")
    revoked = tmp_path / "revoked"
    revoked.write_bytes(b"revocation fixture bytes")
    bundle = import_trust(tmp_path / "managed", sources=SSHTrustFiles((trust,), revoked), authority="fixture")
    result = SyntheticEnrollment(
        SSHConnection("fixture.invalid", "fixture", identity, bundle, ssh_executable=sys.executable),
        SSHCreationProvenance("provider/resource-creation-123", "fixture.invalid"),
        [],
    )
    monkeypatch.setattr(enrollment, "check_client_version", lambda *args, **kwargs: None)
    monkeypatch.setattr(enrollment, "run_process", result.run)
    return result


def test_success_preserves_complete_policy_and_requires_strict_second_connection(
    synthetic: SyntheticEnrollment,
) -> None:
    result = synthetic.enroll()
    assert result.base_generation == trust_status(synthetic.bundle).generation
    assert "StrictHostKeyChecking=accept-new" in synthetic.calls[0]
    assert "StrictHostKeyChecking=yes" in synthetic.calls[1]
    admitted = resolve_trust(synthetic.bundle)
    for argv in synthetic.calls:
        selection = next(arg for arg in argv if arg.startswith("UserKnownHostsFile="))
        assert selection.index(result.known_hosts_file.as_posix()) < selection.index(admitted.known_hosts[0].as_posix())
        assert admitted.revoked_host_keys is not None
        assert f'RevokedHostKeys="{admitted.revoked_host_keys.as_posix()}"' in argv
    with pytest.raises(SSHEnrollmentError):
        synthetic.enroll()
    assert len(synthetic.calls) == 2
    assert synthetic.recover() == result
    assert "StrictHostKeyChecking=yes" in synthetic.calls[-1]


@pytest.mark.parametrize("change", [{"host": "other.invalid"}, {"port": 23}, {"host_key_alias": "alias"}])
def test_mismatched_provenance_refuses_before_mutation(
    synthetic: SyntheticEnrollment, change: dict[str, object]
) -> None:
    provenance = replace(synthetic.provenance, **change)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        enroll_new_target(synthetic.connection, provenance=provenance, deadline=Deadline.after(5))
    assert not list(synthetic.bundle.directory.glob("enrollment-*"))
    assert not synthetic.calls


@pytest.mark.parametrize("seconds", [None, 0])
def test_deadline_refuses_before_mutation(synthetic: SyntheticEnrollment, seconds: float | None) -> None:
    with pytest.raises((ValidationError, SSHEnrollmentError)):
        enroll_new_target(synthetic.connection, provenance=synthetic.provenance, deadline=Deadline.after(seconds))
    assert not list(synthetic.bundle.directory.glob("enrollment-*"))
    assert not synthetic.calls


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(17), OSError("sensitive-path")])
def test_interruption_retains_written_bytes_for_strict_recovery(
    synthetic: SyntheticEnrollment, error: BaseException
) -> None:
    def interrupted(argv: list[str]) -> ProcessResult:
        (synthetic.directory / "known-hosts").write_bytes(b"retained key bytes\n")
        raise error

    synthetic.action = interrupted
    with pytest.raises(type(error) if not isinstance(error, OSError) else SSHEnrollmentError) as caught:
        synthetic.enroll()
    if isinstance(error, OSError):
        assert "sensitive-path" not in str(caught.value)
        assert caught.value.__suppress_context__
    assert (synthetic.directory / "known-hosts").read_bytes() == b"retained key bytes\n"
    synthetic.action = None
    synthetic.recover()
    assert "StrictHostKeyChecking=yes" in synthetic.calls[-1]


@pytest.mark.parametrize("failure", [Failure.DEADLINE, Failure.OUTPUT, None])
def test_failed_ack_preserves_candidate_and_never_retries_first_contact(
    synthetic: SyntheticEnrollment, failure: Failure | None
) -> None:
    synthetic.action = lambda argv: ProcessResult(True, 255, 255, CapturedOutput(), CapturedOutput(), failure)
    with pytest.raises(SSHEnrollmentError):
        synthetic.enroll()
    with pytest.raises(SSHEnrollmentError):
        synthetic.enroll()
    assert len(synthetic.calls) == 1
    assert (synthetic.directory / "known-hosts").exists()


def test_successful_ack_cannot_hide_failed_persistence(
    synthetic: SyntheticEnrollment, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(enrollment, "_sync_candidate", lambda candidate: (_ for _ in ()).throw(OSError("private")))
    with pytest.raises(SSHEnrollmentError):
        synthetic.enroll()
    assert len(synthetic.calls) == 1


@pytest.mark.parametrize("operation", ["enroll", "recover"])
def test_candidate_lock_refuses_concurrent_operations(synthetic: SyntheticEnrollment, operation: str) -> None:
    candidate = synthetic.enroll()
    with files.bundle_lock(candidate.directory), pytest.raises(SSHEnrollmentError):
        getattr(synthetic, operation)()
    assert len(synthetic.calls) == 2
    synthetic.recover()


@pytest.mark.parametrize("change", ["block", "refresh"])
def test_recovery_refuses_changed_policy(synthetic: SyntheticEnrollment, change: str) -> None:
    candidate = synthetic.enroll()
    status = trust_status(synthetic.bundle)
    if change == "block":
        block_trust(synthetic.bundle, expected_generation=status.generation)
    else:
        refresh_trust(
            synthetic.bundle, sources=status.sources, authority="fixture", expected_generation=status.generation
        )
    with pytest.raises(SSHEnrollmentError):
        synthetic.recover()
    assert len(synthetic.calls) == 2
    assert candidate.known_hosts_file.exists()


@pytest.mark.parametrize("damage", ["missing", "partial", "nested", "endpoint", "boolean_port", "primary_missing"])
def test_recovery_refuses_partial_or_mismatched_metadata(synthetic: SyntheticEnrollment, damage: str) -> None:
    candidate = synthetic.enroll()
    manifest = candidate.directory / "state.json"
    if damage == "missing":
        manifest.unlink()
    elif damage == "partial":
        manifest.write_bytes(b"{")
    elif damage == "nested":
        manifest.write_bytes(b"[" * 10000 + b"]" * 10000)
    elif damage == "primary_missing":
        candidate.known_hosts_file.unlink()
    else:
        value = json.loads(manifest.read_bytes())
        value["host" if damage == "endpoint" else "port"] = "another.invalid" if damage == "endpoint" else True
        manifest.write_text(json.dumps(value))
    with pytest.raises(SSHEnrollmentError):
        synthetic.recover()
    assert len(synthetic.calls) == 2


def test_policy_refresh_during_first_ack_refuses_strict_connection(synthetic: SyntheticEnrollment) -> None:
    def changing(argv: list[str]) -> ProcessResult:
        status = trust_status(synthetic.bundle)
        refresh_trust(
            synthetic.bundle, sources=status.sources, authority="fixture", expected_generation=status.generation
        )
        return synthetic.ack(argv)

    synthetic.action = changing
    with pytest.raises(SSHEnrollmentError):
        synthetic.enroll()
    assert len(synthetic.calls) == 1


def test_expired_filesystem_admission_never_dispatches(
    synthetic: SyntheticEnrollment, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    original = admit_connection

    def delayed(connection: SSHConnection) -> SSHTrustFiles:
        result = original(connection)
        now[0] = 100.0
        return result

    monkeypatch.setattr(enrollment, "admit_connection", delayed)
    with pytest.raises(SSHEnrollmentError) as caught:
        synthetic.enroll()
    assert caught.value.failure == Failure.DEADLINE
    assert not synthetic.calls


@pytest.mark.integration
@pytest.mark.parametrize("enrollment_sshd", ["empty", "matching", "ca"], indirect=True)
def test_installed_ssh_enrollment_and_strict_recovery(enrollment_sshd: LocalSSH) -> None:
    candidate = enroll_new_target(
        enrollment_sshd.connection, provenance=enrollment_sshd.provenance, deadline=Deadline.after(5)
    )
    saved = candidate.known_hosts_file.read_bytes()
    if enrollment_sshd.policy_kind == "empty":
        assert enrollment_sshd.host_public_key.split()[1] in saved
    else:
        assert saved == b""
    recovered = recover_enrollment(
        enrollment_sshd.connection, provenance=enrollment_sshd.provenance, deadline=Deadline.after(5)
    )
    assert recovered == candidate
    assert candidate.known_hosts_file.read_bytes() == saved
    with pytest.raises(SSHEnrollmentError):
        enroll_new_target(enrollment_sshd.connection, provenance=enrollment_sshd.provenance, deadline=Deadline.after(5))


@pytest.mark.integration
@pytest.mark.parametrize("enrollment_sshd", ["mismatch", "revoked", "revoked_marker", "revoked_ca"], indirect=True)
def test_installed_ssh_existing_policy_refuses_first_contact(enrollment_sshd: LocalSSH) -> None:
    with pytest.raises(SSHEnrollmentError):
        enroll_new_target(enrollment_sshd.connection, provenance=enrollment_sshd.provenance, deadline=Deadline.after(5))
    with pytest.raises(SSHEnrollmentError):
        recover_enrollment(
            enrollment_sshd.connection, provenance=enrollment_sshd.provenance, deadline=Deadline.after(5)
        )


@pytest.mark.integration
@pytest.mark.parametrize("enrollment_sshd", ["auth_failure"], indirect=True)
def test_installed_ssh_auth_failure_retains_host_key_for_strict_recovery(enrollment_sshd: LocalSSH) -> None:
    with pytest.raises(SSHEnrollmentError):
        enroll_new_target(enrollment_sshd.connection, provenance=enrollment_sshd.provenance, deadline=Deadline.after(5))
    assert isinstance(enrollment_sshd.connection.trust, ManagedSSHTrust)
    primary = next(enrollment_sshd.connection.trust.directory.glob("enrollment-*/known-hosts"))
    saved = primary.read_bytes()
    assert enrollment_sshd.host_public_key.split()[1] in saved
    enrollment_sshd.authorized.write_bytes(enrollment_sshd.connection.identity_file.with_suffix(".pub").read_bytes())
    candidate = recover_enrollment(
        enrollment_sshd.connection, provenance=enrollment_sshd.provenance, deadline=Deadline.after(5)
    )
    assert candidate.known_hosts_file.read_bytes() == saved


@pytest.mark.integration
@pytest.mark.parametrize("enrollment_sshd", ["empty"], indirect=True)
def test_installed_ssh_positive_ack_without_saved_key_fails_strict_verification(
    enrollment_sshd: LocalSSH, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = run_process
    statuses: list[int | None] = []

    def lose_saved_key(argv: list[str], *, io: CarrierIO, deadline: Deadline) -> ProcessResult:
        if "StrictHostKeyChecking=accept-new" in argv:
            # Model a lost append independently of authentication: OpenSSH can
            # authenticate after failing to save a key. The strict attempt must
            # use the real, still-empty candidate and observe missing trust.
            argv = [re.sub(r'^(UserKnownHostsFile=)"[^"]+"', r'\1"/dev/null"', arg) for arg in argv]
        result = original(argv, io=io, deadline=deadline)
        statuses.append(result.exit_status)
        return result

    monkeypatch.setattr(enrollment, "run_process", lose_saved_key)
    with pytest.raises(SSHEnrollmentError):
        enroll_new_target(enrollment_sshd.connection, provenance=enrollment_sshd.provenance, deadline=Deadline.after(5))
    assert statuses == [0, 255]
    assert isinstance(enrollment_sshd.connection.trust, ManagedSSHTrust)
    primary = next(enrollment_sshd.connection.trust.directory.glob("enrollment-*/known-hosts"))
    assert primary.read_bytes() == b""
    with pytest.raises(SSHEnrollmentError):
        recover_enrollment(
            enrollment_sshd.connection, provenance=enrollment_sshd.provenance, deadline=Deadline.after(5)
        )
    assert statuses[-1] == 255


def test_partial_creation_never_reopens_first_contact(
    synthetic: SyntheticEnrollment, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(files, "write_state", lambda *args: (_ for _ in ()).throw(OSError("storage")))
        with pytest.raises(SSHEnrollmentError):
            synthetic.enroll()
    with pytest.raises(SSHEnrollmentError):
        synthetic.enroll()
    with pytest.raises(SSHEnrollmentError):
        synthetic.recover()
    assert not synthetic.calls
    assert synthetic.directory.is_dir()
    assert resolve_trust(synthetic.bundle).known_hosts


def test_interruption_is_not_masked_by_flush_failure(
    synthetic: SyntheticEnrollment, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupt(argv: list[str]) -> ProcessResult:
        raise KeyboardInterrupt

    synthetic.action = interrupt
    monkeypatch.setattr(enrollment, "_sync_candidate", lambda candidate: (_ for _ in ()).throw(OSError("storage")))
    with pytest.raises(KeyboardInterrupt):
        synthetic.enroll()
    assert (synthetic.directory / "known-hosts").exists()


def test_admission_generation_change_refuses_before_creation(
    synthetic: SyntheticEnrollment, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = admit_connection

    def changed(connection: SSHConnection) -> SSHTrustFiles:
        trust = original(connection)
        status = trust_status(synthetic.bundle)
        refresh_trust(
            synthetic.bundle, sources=status.sources, authority="fixture", expected_generation=status.generation
        )
        return trust

    monkeypatch.setattr(enrollment, "admit_connection", changed)
    with pytest.raises(SSHEnrollmentError):
        synthetic.enroll()
    assert not synthetic.calls
    assert not list(synthetic.bundle.directory.glob("enrollment-*"))


@pytest.mark.integration
@pytest.mark.parametrize("enrollment_sshd", ["auth_failure"], indirect=True)
def test_installed_ssh_recovery_retains_mismatching_primary(enrollment_sshd: LocalSSH) -> None:
    with pytest.raises(SSHEnrollmentError):
        enroll_new_target(enrollment_sshd.connection, provenance=enrollment_sshd.provenance, deadline=Deadline.after(5))
    assert isinstance(enrollment_sshd.connection.trust, ManagedSSHTrust)
    primary = next(enrollment_sshd.connection.trust.directory.glob("enrollment-*/known-hosts"))
    other = (enrollment_sshd.authorized.parent / "other-key.pub").read_bytes()
    mismatch = f"[127.0.0.1]:{enrollment_sshd.connection.port} ".encode() + other
    primary.write_bytes(mismatch)
    enrollment_sshd.authorized.write_bytes(enrollment_sshd.connection.identity_file.with_suffix(".pub").read_bytes())
    with pytest.raises(SSHEnrollmentError):
        recover_enrollment(
            enrollment_sshd.connection, provenance=enrollment_sshd.provenance, deadline=Deadline.after(5)
        )
    assert primary.read_bytes() == mismatch


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_interruption_during_failed_attempt_flush_is_preserved(
    synthetic: SyntheticEnrollment, monkeypatch: pytest.MonkeyPatch, interruption: type[BaseException]
) -> None:
    synthetic.action = lambda argv: ProcessResult(True, 255, 255, CapturedOutput(), CapturedOutput(), None)

    def interrupt_flush(candidate: enrollment.SSHEnrollmentCandidate) -> None:
        raise interruption()

    monkeypatch.setattr(enrollment, "_sync_candidate", interrupt_flush)
    with pytest.raises(interruption):
        synthetic.enroll()
    assert (synthetic.directory / "known-hosts").exists()


def test_enrollment_pins_probe_and_both_acknowledgments_despite_path_change(
    synthetic: SyntheticEnrollment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.execution.carriers.ssh.test_client_selection import selectable_client

    selected = selectable_client(tmp_path / "selected")
    replacement = selectable_client(tmp_path / "replacement")
    synthetic.connection = replace(synthetic.connection, ssh_executable="ssh")
    monkeypatch.setenv("PATH", str(selected.parent))
    probes: list[str] = []

    def version(executable: str, *, deadline: Deadline) -> None:
        probes.append(executable)
        monkeypatch.setenv("PATH", str(replacement.parent))

    monkeypatch.setattr(enrollment, "check_client_version", version)
    synthetic.enroll()
    assert probes == [str(selected)]
    assert [argv[0] for argv in synthetic.calls] == [str(selected), str(selected)]
    synthetic.recover()
    assert probes == [str(selected), str(replacement)]
    assert synthetic.calls[-1][0] == str(replacement)
