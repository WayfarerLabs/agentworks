"""Explicit first-contact maintenance with durable evidence and strict-only recovery.

Creation provenance is a trusted composition assertion, not proof of provider
ownership. Candidate evidence never enables ordinary connections by itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agentworks.errors import StateError, ValidationError
from agentworks.execution.carrier import Capture, CarrierIO, Deadline, Failure, PreparedInvocation
from agentworks.execution.carriers.ssh import _trust_files as files
from agentworks.execution.carriers.ssh._io import run_process
from agentworks.execution.carriers.ssh.client import check_client_version
from agentworks.execution.carriers.ssh.connection import (
    SSHConnection,
    _build_enrollment_argv,
    admit_connection,
    build_ssh_argv,
)
from agentworks.execution.carriers.ssh.trust import ManagedSSHTrust, SSHTrustFiles, trust_status

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class SSHCreationProvenance:
    """Stable provider creation identity and endpoint supplied by trusted composition.

    Adapter authors can call this outside static typing. Resource IDs are opaque;
    a display name or a newly invented ID cannot authorize another first contact.
    """

    resource_id: str
    host: str
    port: int = 22
    host_key_alias: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.resource_id, str)
            or not self.resource_id.strip()
            or not self.resource_id.isprintable()
            or len(self.resource_id.encode("utf-8")) > 4096
        ):
            raise ValidationError("SSH enrollment requires a bounded stable resource creation identity")
        if (
            not isinstance(self.host, str)
            or not self.host
            or type(self.port) is not int
            or not 1 <= self.port <= 65535
            or (self.host_key_alias is not None and not isinstance(self.host_key_alias, str))
        ):
            raise ValidationError("SSH enrollment provenance requires an explicit endpoint")


@dataclass(frozen=True)
class SSHEnrollmentCandidate:
    """Verified local evidence awaiting explicit complete-policy publication.

    This receipt grants no permission to cache a managed generation or construct
    an ordinary connection using its retained files.
    """

    directory: Path
    known_hosts_file: Path
    base_generation: str


class SSHEnrollmentError(StateError):
    """Enrollment did not verify; candidate evidence remains for explicit recovery."""

    def __init__(self, *, failure: Failure, local_status: int | None = None) -> None:
        super().__init__("SSH enrollment did not verify; retain evidence and inspect policy before recovery")
        self.failure = failure
        self.local_status = local_status


def _check_deadline(deadline: Deadline) -> None:
    if deadline.expired:
        raise SSHEnrollmentError(failure=Failure.DEADLINE)


def _bound_bundle(connection: SSHConnection, provenance: SSHCreationProvenance, deadline: Deadline) -> ManagedSSHTrust:
    if not isinstance(provenance, SSHCreationProvenance) or (
        connection.host,
        connection.port,
        connection.host_key_alias,
    ) != (provenance.host, provenance.port, provenance.host_key_alias):
        raise ValidationError("SSH creation provenance must match the connection endpoint exactly")
    if not isinstance(connection.trust, ManagedSSHTrust):
        raise ValidationError("SSH enrollment requires managed trust")
    if deadline.expires_at is None:
        raise ValidationError("SSH enrollment requires a finite deadline")
    return connection.trust


def _admit(connection: SSHConnection, bundle: ManagedSSHTrust, deadline: Deadline) -> tuple[str, SSHTrustFiles]:
    """Pair fresh admission with its generation, refusing concurrent policy changes."""
    _check_deadline(deadline)
    before = trust_status(bundle)
    trust = admit_connection(connection)
    after = trust_status(bundle)
    _check_deadline(deadline)
    if before.blocked or after.blocked or before.generation is None or before.generation != after.generation:
        raise SSHEnrollmentError(failure=Failure.DISPATCH)
    return before.generation, trust


def _document(provenance: SSHCreationProvenance, generation: str) -> dict[str, object]:
    return {
        "version": 1,
        "resource_id": provenance.resource_id,
        "host": provenance.host,
        "port": provenance.port,
        "host_key_alias": provenance.host_key_alias,
        "base_generation": generation,
    }


def _verify_document(directory: Path, provenance: SSHCreationProvenance, generation: str) -> None:
    with files.read_file(directory / "state.json", owned=True) as source:
        payload = source.read(files.MANIFEST_LIMIT + 1)
    if len(payload) > files.MANIFEST_LIMIT:
        raise SSHEnrollmentError(failure=Failure.DISPATCH)
    value = json.loads(payload)
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or type(value.get("port")) is not int
        or value != _document(provenance, generation)
    ):
        raise SSHEnrollmentError(failure=Failure.DISPATCH)


def _sync_candidate(candidate: SSHEnrollmentCandidate) -> None:
    files.check_path(candidate.known_hosts_file, owned=True)
    descriptor = os.open(candidate.known_hosts_file, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    files.sync_directory(candidate.directory)


def _acknowledge(
    connection: SSHConnection,
    bundle: ManagedSSHTrust,
    candidate: SSHEnrollmentCandidate,
    *,
    deadline: Deadline,
    first_contact: bool,
) -> None:
    generation, trust = _admit(connection, bundle, deadline)
    if generation != candidate.base_generation:
        raise SSHEnrollmentError(failure=Failure.DISPATCH)
    files.check_path(candidate.known_hosts_file, owned=True)
    selection = SSHTrustFiles((candidate.known_hosts_file, *trust.known_hosts), trust.revoked_host_keys)
    nonce = "agw-enroll-" + uuid.uuid4().hex
    invocation = PreparedInvocation(("sh", "-c", f"printf '%s\\n' '{nonce}'"))
    builder = _build_enrollment_argv if first_contact else build_ssh_argv
    argv = builder(connection, invocation, trust=selection)
    _check_deadline(deadline)
    result = run_process(argv, io=CarrierIO(output=Capture(4096)), deadline=deadline)
    if result.failure is not None:
        raise SSHEnrollmentError(failure=result.failure, local_status=result.local_status)
    if result.exit_status != 0 or not result.stdout.complete or result.stdout.data != (nonce + "\n").encode():
        raise SSHEnrollmentError(failure=Failure.OBSERVATION, local_status=result.local_status)
    _check_deadline(deadline)


def enroll_new_target(
    connection: SSHConnection, *, provenance: SSHCreationProvenance, deadline: Deadline
) -> SSHEnrollmentCandidate:
    """Try first contact once, then independently verify retained trust strictly.

    A finite deadline is required. Every failed or interrupted attempt keeps its
    directory and bytes. An existing candidate, including a partial one, never
    starts another accept-new operation.
    """
    return _maintain(connection, provenance=provenance, deadline=deadline, first_contact=True)


def recover_enrollment(
    connection: SSHConnection, *, provenance: SSHCreationProvenance, deadline: Deadline
) -> SSHEnrollmentCandidate:
    """Verify retained evidence against its active base policy within a finite deadline."""
    return _maintain(connection, provenance=provenance, deadline=deadline, first_contact=False)


def _maintain(
    connection: SSHConnection, *, provenance: SSHCreationProvenance, deadline: Deadline, first_contact: bool
) -> SSHEnrollmentCandidate:
    bundle = _bound_bundle(connection, provenance, deadline)
    try:
        generation, _ = _admit(connection, bundle, deadline)
        version_failure = check_client_version(connection, deadline=deadline)
        _check_deadline(deadline)
        if version_failure is not None:
            raise SSHEnrollmentError(failure=version_failure)
        directory = bundle.directory / ("enrollment-" + hashlib.sha256(provenance.resource_id.encode()).hexdigest())
        candidate = SSHEnrollmentCandidate(directory, directory / "known-hosts", generation)
        if first_contact:
            files.create_bundle(directory)
        with files.bundle_lock(directory):
            if first_contact:
                files.write_state(directory, _document(provenance, generation))
                descriptor = os.open(candidate.known_hosts_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as target:
                    target.flush()
                    os.fsync(target.fileno())
                files.sync_directory(directory)
                _check_deadline(deadline)
                try:
                    _acknowledge(connection, bundle, candidate, deadline=deadline, first_contact=True)
                except BaseException as error:
                    try:
                        _sync_candidate(candidate)
                    except BaseException as flush_error:
                        note = "SSH enrollment evidence could not be flushed; inspect retained storage"
                        if isinstance(flush_error, (KeyboardInterrupt, SystemExit)) and not isinstance(
                            error, (KeyboardInterrupt, SystemExit)
                        ):
                            flush_error.add_note(note)
                            raise
                        error.add_note(note)
                    raise
            else:
                _verify_document(directory, provenance, generation)
            _sync_candidate(candidate)
            _check_deadline(deadline)
            _acknowledge(connection, bundle, candidate, deadline=deadline, first_contact=False)
            # A refresh after admission cannot silently turn this into evidence
            # about the new policy. The receipt remains tied to its base policy.
            current, _ = _admit(connection, bundle, deadline)
            if current != generation:
                raise SSHEnrollmentError(failure=Failure.DISPATCH)
            return candidate
    except SSHEnrollmentError:
        raise
    except (OSError, StateError, ValidationError, ValueError, TypeError, RecursionError):
        raise SSHEnrollmentError(failure=Failure.DISPATCH) from None
