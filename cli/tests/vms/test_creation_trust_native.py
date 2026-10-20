"""Installed-client proof of marker-bound trust across delivery endpoints.

Run only under an authorized native-test charter with a private account home
and startup-free account shell. The shared fixture owns loopback sshd and keys.
Only its nonstandard port is injected into the production connection selector;
enrollment, publication, strict delivery and SQLite ownership are real.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from agentworks.db import Database, OperationResourceKind, OperationScope
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import Capture, CarrierIO, CarrierReport, Deadline, ExitStatus, PreparedInvocation
from agentworks.execution.carriers.ssh import ManagedSSHTrust, SSHCarrier, SSHConnection, SSHSettings, SSHTrustFiles
from agentworks.execution.carriers.ssh.enrollment import (
    SSHCreationProvenance,
    SSHEnrollmentCandidate,
    SSHEnrollmentCustody,
)
from agentworks.execution.carriers.ssh.trust import (
    SSHTrustStatus,
    block_trust,
    refresh_trust,
    resolve_trust,
    trust_status,
)
from agentworks.operations import OperationOwner
from agentworks.vms import _creation_trust as composition
from agentworks.vms._ssh_native_binding import vm_ssh_connection
from tests.execution.carriers.ssh._held_resources import held_resource
from tests.execution.carriers.ssh.enrollment_server import LocalSSH, enrollment_server

pytestmark = pytest.mark.integration
_MARKER = "c" * 32


@dataclass
class NativeTrust:
    server: LocalSSH
    settings: SSHSettings
    writes: list[str]

    def connection(self) -> SSHConnection:
        selected = vm_ssh_connection(
            "box", self.server.connection.user, "127.0.0.1", self.settings, instance_marker=_MARKER
        )
        return replace(selected, port=self.server.connection.port)


@pytest.fixture
def native_trust(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[NativeTrust]:
    server_root = tmp_path / "server"
    server_root.mkdir(mode=0o700)
    with enrollment_server(server_root, "workflow_alias") as server, closing(Database(tmp_path / "state.db")) as db:
        assert isinstance(server.connection.trust, ManagedSSHTrust)
        settings = SSHSettings(server.connection.trust.directory, server.connection.identity_file)
        db.insert_vm("box", "fixture", "template", admin_username=server.connection.user, instance_marker=_MARKER)
        owner = OperationOwner.acquire(
            db.operations, OperationScope(OperationResourceKind.VM, "box"), "native-creation-trust"
        )
        writes: list[str] = []
        original_enroll, original_refresh = composition.enroll_new_target, composition.refresh_trust

        def selected(
            vm_name: str,
            admin_username: str,
            endpoint: object,
            policy: SSHSettings,
            *,
            instance_marker: str | None,
        ) -> SSHConnection:
            return replace(
                vm_ssh_connection(vm_name, admin_username, endpoint, policy, instance_marker=instance_marker),
                port=server.connection.port,
            )

        def enroll(
            connection: SSHConnection,
            *,
            provenance: SSHCreationProvenance,
            deadline: Deadline,
            custody: SSHEnrollmentCustody,
        ) -> SSHEnrollmentCandidate:
            writes.append("enrollment")
            return original_enroll(connection, provenance=provenance, deadline=deadline, custody=custody)

        def publish(
            bundle: ManagedSSHTrust,
            *,
            sources: SSHTrustFiles,
            authority: str,
            expected_generation: str | None,
        ) -> SSHTrustStatus:
            writes.append("publication")
            return original_refresh(bundle, sources=sources, authority=authority, expected_generation=expected_generation)

        monkeypatch.setattr(composition, "vm_ssh_connection", selected)
        monkeypatch.setattr(composition, "enroll_new_target", enroll)
        monkeypatch.setattr(composition, "refresh_trust", publish)
        consumer = composition.CreationSSHTrust(
            owner,
            vm_name="box",
            admin_username=server.connection.user,
            instance_marker=_MARKER,
            endpoint="127.0.0.1",
            settings=settings,
            deadline=Deadline.after(20),
        )
        with held_resource(consumer):
            consumer.prepare()
            yield NativeTrust(server, settings, writes)
        assert not owner.list_pending_lifecycle_obligations()
        owner.stop_admission()
        owner.seal_lifecycle_obligations()
        owner.record_effects_resolved()
        owner.close()


def _run(connection: SSHConnection, marker: Path) -> CarrierReport:
    # The shell receives the fixture path as a literal positional argument.
    invocation = PreparedInvocation(("/bin/sh", "-c", 'printf x >> "$1"; printf verified', "fixture", str(marker)))
    custody = LocalDeliveryCustody()
    with held_resource(custody):
        return SSHCarrier(connection).execute(
            invocation, io=CarrierIO(output=Capture(4096)), deadline=Deadline.after(10), custody=custody
        )


def test_marker_alias_strictly_reconnects_without_another_enrollment(native_trust: NativeTrust, tmp_path: Path) -> None:
    selected = native_trust.connection()
    assert isinstance(selected.trust, ManagedSSHTrust)
    generation = trust_status(selected.trust).generation
    marker = tmp_path / "executed"
    # Change the delivery spelling to the same fixture server. Core IPv4 endpoint
    # selection is separately covered; this proves native alias lookup, not DNS
    # selection or actual VM provisioning.
    report = _run(replace(selected, host="localhost"), marker)
    assert report.completion == ExitStatus(0) and report.failure is None
    assert report.stdout.data == b"verified" and report.stdout.complete
    assert marker.read_bytes() == b"x"
    assert native_trust.writes == ["enrollment", "publication"]
    assert trust_status(selected.trust).generation == generation
    assert len(tuple(selected.trust.directory.glob("enrollment-*"))) == 1


@pytest.mark.parametrize("change", ["missing", "blocked", "revoked"])
def test_changed_policy_refuses_without_new_first_contact(
    native_trust: NativeTrust, tmp_path: Path, change: str
) -> None:
    selected = native_trust.connection()
    assert isinstance(selected.trust, ManagedSSHTrust)
    assert selected.host_key_alias is not None
    current = trust_status(selected.trust)
    assert current.generation is not None
    if change == "blocked":
        block_trust(selected.trust, expected_generation=current.generation)
    else:
        policy = tmp_path / "changed-policy"
        policy.write_bytes(
            b"" if change == "missing" else b"@revoked " + selected.host_key_alias.encode() + b" "
            + native_trust.server.host_public_key
        )
        admitted = resolve_trust(selected.trust)
        refresh_trust(
            selected.trust,
            sources=SSHTrustFiles(
                (policy,) if change == "missing" else (policy, *admitted.known_hosts), admitted.revoked_host_keys
            ),
            authority="fixture policy change",
            expected_generation=current.generation,
        )
    generation = trust_status(selected.trust).generation
    marker = tmp_path / "refused"
    report = _run(replace(selected, host="localhost"), marker)
    assert report.completion is None and report.failure is not None
    assert not marker.exists()
    assert native_trust.writes == ["enrollment", "publication"]
    assert trust_status(selected.trust).generation == generation


def test_changed_key_refuses_before_command_without_reenrollment(native_trust: NativeTrust, tmp_path: Path) -> None:
    other_root = tmp_path / "other"
    other_root.mkdir(mode=0o700)
    with enrollment_server(other_root, "unknown") as other:
        selected = native_trust.connection()
        assert isinstance(selected.trust, ManagedSSHTrust)
        assert other.host_public_key.split()[1] != native_trust.server.host_public_key.split()[1]
        # A separate explicit fixture policy proves the second server accepts its
        # own identity. It does not modify or enroll the VM's managed bundle.
        other_policy = tmp_path / "other-policy"
        assert selected.host_key_alias is not None
        other_policy.write_bytes(selected.host_key_alias.encode() + b" " + other.host_public_key)
        other_connection = replace(
            selected, port=other.connection.port, identity_file=other.connection.identity_file
        )
        positive = _run(replace(other_connection, trust=SSHTrustFiles((other_policy,))), tmp_path / "other-verified")
        assert positive.completion == ExitStatus(0) and positive.failure is None
        generation = trust_status(selected.trust).generation
        marker = tmp_path / "refused"
        report = _run(other_connection, marker)
        assert report.completion is None and report.failure is not None
        assert not marker.exists()
        assert native_trust.writes == ["enrollment", "publication"]
        assert trust_status(selected.trust).generation == generation
