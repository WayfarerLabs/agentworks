"""Installed-client composition of explicit enrollment and complete-policy maintenance."""

from __future__ import annotations

import shlex
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentworks.cli import app
from agentworks.errors import StateError
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import Capture, CarrierIO, Deadline, Dispatch, ExitStatus, PreparedInvocation
from agentworks.execution.carriers.ssh.client import SSHCarrier
from agentworks.execution.carriers.ssh.enrollment import enroll_new_target, recover_enrollment
from agentworks.execution.carriers.ssh.trust import ManagedSSHTrust, resolve_trust, trust_status
from tests.execution.carriers.ssh.enrollment_server import LocalSSH

pytestmark = pytest.mark.integration
runner = CliRunner()


def _maintenance(command: str, *operands: str | Path):
    return runner.invoke(app, ["config", command, *(str(operand) for operand in operands)])


def _snapshot(paths: tuple[Path, ...]) -> dict[Path, tuple[bytes, int]]:
    return {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}


def _unchanged(snapshot: dict[Path, tuple[bytes, int]]) -> None:
    assert all((path.read_bytes(), path.stat().st_mtime_ns) == before for path, before in snapshot.items())


def _invocation(marker: Path) -> PreparedInvocation:
    # A fixture-local guest effect proves refused operations never ran the command.
    return PreparedInvocation(("/bin/sh", "-c", f"printf x >> {shlex.quote(str(marker))}; printf verified"))


def _execute(carrier: SSHCarrier, invocation: PreparedInvocation, *, custody: LocalDeliveryCustody):
    return carrier.execute(invocation, io=CarrierIO(output=Capture(1024)), deadline=Deadline.after(10), custody=custody)


def _strict_success(carrier: SSHCarrier, invocation: PreparedInvocation, *, custody: LocalDeliveryCustody) -> None:
    result = _execute(carrier, invocation, custody=custody)
    assert result.dispatch == Dispatch.SENT
    assert result.completion == ExitStatus(0)
    assert result.failure is None
    assert result.stdout.data == b"verified" and result.stdout.complete


def _blocked(carrier: SSHCarrier, invocation: PreparedInvocation, *, custody: LocalDeliveryCustody) -> None:
    result = _execute(carrier, invocation, custody=custody)
    assert result.dispatch == Dispatch.NOT_SENT
    assert result.completion is None and result.local_status is None
    assert result.failure is not None


@pytest.mark.parametrize("enrollment_sshd", ["workflow", "workflow_alias"], indirect=True)
def test_enrollment_publication_block_failed_refresh_repair_and_strict_reconnect(
    custody: LocalDeliveryCustody, enrollment_sshd: LocalSSH, tmp_path: Path
) -> None:
    connection = enrollment_sshd.connection
    assert connection.port != 22
    assert isinstance(connection.trust, ManagedSSHTrust)
    bundle = connection.trust
    initial = trust_status(bundle)
    assert initial.generation is not None and not initial.blocked
    old_policy = resolve_trust(bundle)
    assert old_policy.revoked_host_keys is not None
    assert initial.sources.revoked_host_keys is not None
    originals = _snapshot((*initial.sources.known_hosts, initial.sources.revoked_host_keys))
    retained = _snapshot((*old_policy.known_hosts, old_policy.revoked_host_keys))
    carrier = SSHCarrier(connection)
    marker = tmp_path.resolve() / "executed"
    invocation = _invocation(marker)

    # Unknown trust on an existing managed connection must not enroll or execute.
    refused = _execute(carrier, invocation, custody=custody)
    assert refused.local_status == 255 and refused.completion is None
    assert not marker.exists()
    assert not tuple(bundle.directory.glob("enrollment-*"))

    candidate = enroll_new_target(connection, provenance=enrollment_sshd.provenance, deadline=Deadline.after(10))
    learned = candidate.known_hosts_file.read_bytes()
    assert enrollment_sshd.host_public_key.split()[1] in learned
    assert candidate.base_generation == initial.generation
    assert (
        recover_enrollment(connection, provenance=enrollment_sshd.provenance, deadline=Deadline.after(10)) == candidate
    )
    assert candidate.known_hosts_file.read_bytes() == learned
    # A creation receipt alone cannot admit an ordinary command.
    refused = _execute(carrier, invocation, custody=custody)
    assert refused.local_status == 255 and refused.completion is None
    assert not marker.exists()

    result = _maintenance(
        "refresh-ssh-trust",
        bundle.directory,
        candidate.known_hosts_file,
        *initial.sources.known_hosts,
        "--authority",
        "owned fixture maintainer",
        "--expected-generation",
        initial.generation,
        "--revoked-host-keys",
        initial.sources.revoked_host_keys,
    )
    assert result.exit_code == 0, result.exception
    published = trust_status(bundle)
    assert published.generation is not None and published.generation != initial.generation and not published.blocked
    current = resolve_trust(bundle)
    assert [path.read_bytes() for path in current.known_hosts] == [
        learned,
        *[p.read_bytes() for p in initial.sources.known_hosts],
    ]
    assert current.revoked_host_keys is not None
    assert current.revoked_host_keys.read_bytes() == old_policy.revoked_host_keys.read_bytes()
    retained.update(
        _snapshot(
            (
                *current.known_hosts,
                current.revoked_host_keys,
                candidate.known_hosts_file,
                candidate.directory / "state.json",
            )
        )
    )
    _strict_success(carrier, invocation, custody=custody)
    assert marker.read_bytes() == b"x"

    result = _maintenance("block-ssh-trust", bundle.directory, "--expected-generation", published.generation)
    assert result.exit_code == 0, result.exception
    _blocked(carrier, invocation, custody=custody)
    assert marker.read_bytes() == b"x"
    generations_before = {path for path in bundle.directory.iterdir() if path.is_dir()}
    result = _maintenance(
        "refresh-ssh-trust",
        bundle.directory,
        candidate.known_hosts_file,
        *initial.sources.known_hosts,
        tmp_path.resolve() / "missing-source",
        "--authority",
        "owned fixture maintainer",
        "--expected-generation",
        published.generation,
        "--revoked-host-keys",
        initial.sources.revoked_host_keys,
    )
    assert isinstance(result.exception, StateError)
    failed = trust_status(bundle)
    assert failed.blocked and failed.generation == published.generation
    partials = {path for path in bundle.directory.iterdir() if path.is_dir()} - generations_before
    assert len(partials) == 1
    partial = partials.pop()
    assert (partial / "known-hosts-0").read_bytes() == learned
    assert (partial / "known-hosts-1").read_bytes() == initial.sources.known_hosts[0].read_bytes()
    retained.update(_snapshot(tuple(partial.iterdir())))
    _blocked(carrier, invocation, custody=custody)
    assert marker.read_bytes() == b"x"
    _unchanged(retained)
    _unchanged(originals)

    # Explicit repair republishes all policy; failure never rolls back admission.
    result = _maintenance(
        "refresh-ssh-trust",
        bundle.directory,
        candidate.known_hosts_file,
        *initial.sources.known_hosts,
        "--authority",
        "owned fixture maintainer",
        "--expected-generation",
        failed.generation,
        "--revoked-host-keys",
        initial.sources.revoked_host_keys,
    )
    assert result.exit_code == 0, result.exception
    repaired = trust_status(bundle)
    assert not repaired.blocked and repaired.generation not in {initial.generation, published.generation}
    repaired_policy = resolve_trust(bundle)
    assert [path.read_bytes() for path in repaired_policy.known_hosts] == [
        path.read_bytes() for path in current.known_hosts
    ]
    assert repaired_policy.revoked_host_keys is not None
    assert repaired_policy.revoked_host_keys.read_bytes() == current.revoked_host_keys.read_bytes()
    _strict_success(carrier, invocation, custody=custody)
    assert marker.read_bytes() == b"xx"
    _unchanged(retained)
    _unchanged(originals)


@pytest.mark.parametrize("enrollment_sshd", ["hashed", "hashed_alias"], indirect=True)
def test_real_hashed_policy_matches_only_selected_alias_and_nondefault_port(
    custody: LocalDeliveryCustody, enrollment_sshd: LocalSSH, tmp_path: Path
) -> None:
    connection = enrollment_sshd.connection
    assert connection.port != 22
    assert isinstance(connection.trust, ManagedSSHTrust)
    admitted = resolve_trust(connection.trust)
    original = _snapshot(admitted.known_hosts)
    assert admitted.known_hosts[0].read_bytes().startswith(b"|1|")
    invocation = _invocation(tmp_path.resolve() / "hashed-executed")
    _strict_success(SSHCarrier(connection), invocation, custody=custody)
    # Same server and identity, but another lookup identity must not match the hash.
    mismatched = replace(connection, host_key_alias="different-owned-fixture-alias")
    refused = _execute(SSHCarrier(mismatched), invocation, custody=custody)
    assert refused.local_status == 255 and refused.completion is None
    assert (tmp_path.resolve() / "hashed-executed").read_bytes() == b"x"
    _unchanged(original)
