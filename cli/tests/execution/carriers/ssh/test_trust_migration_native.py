"""Installed-client acceptance of isolated-copy CA and revocation maintenance."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest

from agentworks.errors import StateError
from agentworks.execution.carrier import Deadline
from agentworks.execution.carriers.ssh.connection import SSHConnection
from agentworks.execution.carriers.ssh.enrollment import enroll_new_target
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
from tests.execution.carriers.ssh.fixture_worker import fixture_call

pytestmark = pytest.mark.integration

# Each application attempt runs in a fresh interpreter with retired execution
# modules unavailable. The parent never uses the CLI's unrelated import graph.
_STRICT_PROBE = r"""
import importlib.abc
import json
import shlex
import sys
from pathlib import Path

retired = (
    "agentworks.transports", "agentworks.ssh", "agentworks.remote_exec",
    "agentworks.harness_setup.runner", "agentworks.native_files",
    "agentworks.plugins.proxmox.transport",
)
class BlockRetired(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in retired):
            raise ImportError("Retired execution module is unavailable: " + fullname)

sys.meta_path.insert(0, BlockRetired())
from agentworks.execution.carrier import Capture, CarrierIO, Deadline, Dispatch, ExitStatus, PreparedInvocation
from agentworks.execution.carriers.ssh import SSHCarrier, SSHConnection
from agentworks.execution.carriers.ssh.trust import ManagedSSHTrust

value = json.loads(sys.argv[1])
connection = SSHConnection(
    value["host"], value["user"], Path(value["identity"]), ManagedSSHTrust(Path(value["bundle"])),
    port=value["port"], host_key_alias=value["alias"], ssh_executable=value["executable"],
)
invocation = PreparedInvocation((
    "/bin/sh", "-c", "printf x >> " + shlex.quote(value["marker"]) + "; printf verified",
))
result = SSHCarrier(connection).execute(invocation, io=CarrierIO(output=Capture(1024)), deadline=Deadline.after(10))
print(json.dumps({
    "dispatch": result.dispatch, "local_status": result.local_status,
    "completion": result.completion.code if result.completion is not None else None,
    "failure": result.failure, "stdout_bytes": len(result.stdout.data), "stdout_complete": result.stdout.complete,
}), flush=True)
if value["expected"] == "accepted":
    assert result.dispatch == Dispatch.SENT and result.completion == ExitStatus(0)
    assert result.failure is None and result.stdout.data == b"verified" and result.stdout.complete
elif value["expected"] == "blocked":
    assert result.dispatch == Dispatch.NOT_SENT and result.local_status is None
    assert result.completion is None and result.failure is not None
else:
    assert value["expected"] == "refused"
    assert result.local_status == 255 and result.completion is None
assert not any(name == module or name.startswith(module + ".") for name in sys.modules for module in retired)
"""


def _strict(connection: SSHConnection, marker: Path, expected: str) -> None:
    assert isinstance(connection.trust, ManagedSSHTrust)
    attempt = uuid.uuid4().hex
    # A 255 observation never authorizes retry of the same mutating invocation.
    # Refusal attempts each name their own effect and are issued exactly once.
    effect = marker if expected == "accepted" else marker.with_name(f"{marker.name}-refused-{attempt}")
    value = {
        "host": connection.host,
        "user": connection.user,
        "identity": str(connection.identity_file),
        "bundle": str(connection.trust.directory),
        "port": connection.port,
        "alias": connection.host_key_alias,
        "executable": connection.ssh_executable,
        "marker": str(effect),
        "expected": expected,
    }
    result = fixture_call(
        partial(
            subprocess.run,
            [sys.executable, "-I", "-c", _STRICT_PROBE, json.dumps(value)],
            capture_output=True,
            timeout=15,
        )
    )
    observations = marker.with_name(f"{marker.name}-probe-{attempt}.log")
    observations.write_bytes(result.stdout + result.stderr)
    # No client diagnostics or fixture key contents enter a failure message.
    assert result.returncode == 0, observations
    if expected != "accepted":
        assert not effect.exists()


def _snapshot(paths: tuple[Path, ...]) -> dict[Path, tuple[bytes, int]]:
    return {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}


def _unchanged(snapshot: dict[Path, tuple[bytes, int]]) -> None:
    assert all((path.read_bytes(), path.stat().st_mtime_ns) == before for path, before in snapshot.items())


def _restore(snapshot: dict[Path, tuple[bytes, int]]) -> None:
    # A rollback of owned source bytes and timestamps is not a bundle-state rollback.
    for path, (data, modified) in snapshot.items():
        path.write_bytes(data)
        os.utime(path, ns=(path.stat().st_atime_ns, modified))


def _policy_snapshot(bundle: ManagedSSHTrust) -> dict[Path, tuple[bytes, int]]:
    selected = resolve_trust(bundle)
    paths = (*selected.known_hosts, *((selected.revoked_host_keys,) if selected.revoked_host_keys else ()))
    return _snapshot(paths)


@pytest.mark.parametrize("enrollment_sshd", ["ca"], indirect=True)
def test_explicit_ca_policy_transition_preserves_generations_on_source_rollback(
    enrollment_sshd: LocalSSH, tmp_path: Path
) -> None:
    connection = enrollment_sshd.connection
    assert isinstance(connection.trust, ManagedSSHTrust)
    certificate_policy = resolve_trust(connection.trust)
    originals = _snapshot((*certificate_policy.known_hosts, *trust_status(connection.trust).sources.known_hosts))
    # The server already serves a replacement-CA certificate. This is a policy
    # transition against that certificate, not a live server reload experiment.
    replacement_policy = certificate_policy.known_hosts[0].read_bytes()
    prior_ca = tmp_path.resolve() / "prior-ca"
    fixture_call(
        partial(
            subprocess.run, ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(prior_ca)], check=True, timeout=10
        )
    )
    prior_public = prior_ca.with_suffix(".pub").read_bytes()
    assert prior_public.split()[1] not in replacement_policy
    lookup = f"[127.0.0.1]:{connection.port}".encode()
    source = tmp_path.resolve() / "owned-ca-source"
    source.write_bytes(b"@cert-authority " + lookup + b" " + prior_public)
    source_before = _snapshot((source,))
    bundle = import_trust(
        tmp_path.resolve() / "ca-migration", sources=SSHTrustFiles((source,)), authority="fixture CA owner"
    )
    migrated = replace(connection, trust=bundle)
    initial = trust_status(bundle)
    assert initial.generation is not None and not initial.blocked
    retained = _policy_snapshot(bundle)
    marker = tmp_path.resolve() / "ca-executed"
    _strict(migrated, marker, "refused")
    assert not marker.exists()

    # Include the explicit superseded-CA revocation in the complete replacement.
    source.write_bytes(replacement_policy + b"@revoked " + lookup + b" " + prior_public)
    replacement = source.read_bytes()
    _unchanged(retained)
    assert trust_status(bundle) == initial
    _strict(migrated, marker, "refused")
    assert not marker.exists()
    published = refresh_trust(
        bundle, sources=SSHTrustFiles((source,)), authority="fixture CA owner", expected_generation=initial.generation
    )
    assert published.generation is not None and published.generation != initial.generation
    assert resolve_trust(bundle).known_hosts[0].read_bytes() == replacement
    retained.update(_policy_snapshot(bundle))
    _strict(migrated, marker, "accepted")
    assert marker.read_bytes() == b"x"

    _restore(source_before)
    _unchanged(source_before)
    current_state = _snapshot((bundle.directory / "state.json",))
    with pytest.raises(StateError):
        refresh_trust(
            bundle,
            sources=SSHTrustFiles((source,)),
            authority="stale fixture writer",
            expected_generation=initial.generation,
        )
    with pytest.raises(StateError):
        import_trust(bundle.directory, sources=SSHTrustFiles((source,)), authority="rolled-back fixture source")
    _unchanged(current_state)
    assert trust_status(bundle) == published
    assert resolve_trust(bundle).known_hosts[0].read_bytes() == replacement
    # Reconstructing the same explicit connection cannot replace retained state.
    _strict(replace(migrated), marker, "accepted")
    assert marker.read_bytes() == b"xx"
    _unchanged(retained)
    _unchanged(originals)


@pytest.mark.parametrize("enrollment_sshd", ["workflow_alias"], indirect=True)
def test_applicable_krl_refresh_blocks_failed_update_and_retains_learned_trust_on_rollback(
    enrollment_sshd: LocalSSH, tmp_path: Path
) -> None:
    connection = enrollment_sshd.connection
    assert isinstance(connection.trust, ManagedSSHTrust)
    original_policy = resolve_trust(connection.trust)
    assert original_policy.revoked_host_keys is not None
    original_sources = trust_status(connection.trust).sources
    assert original_sources.revoked_host_keys is not None
    originals = _snapshot(
        (
            *original_policy.known_hosts,
            original_policy.revoked_host_keys,
            *original_sources.known_hosts,
            original_sources.revoked_host_keys,
        )
    )
    source = tmp_path.resolve() / "owned-known-host-source"
    revocations = tmp_path.resolve() / "owned-revocation-source"
    source.write_bytes(original_policy.known_hosts[0].read_bytes())
    revocations.write_bytes(original_policy.revoked_host_keys.read_bytes())
    source_before = _snapshot((source, revocations))
    bundle = import_trust(
        tmp_path.resolve() / "krl-migration",
        sources=SSHTrustFiles((source,), revocations),
        authority="fixture revocation owner",
    )
    migrated = replace(connection, trust=bundle)
    initial = trust_status(bundle)
    assert initial.generation is not None
    retained = _policy_snapshot(bundle)
    marker = tmp_path.resolve() / "krl-executed"
    candidate = enroll_new_target(migrated, provenance=enrollment_sshd.provenance, deadline=Deadline.after(10))
    learned = candidate.known_hosts_file.read_bytes()
    assert enrollment_sshd.host_public_key.split()[1] in learned
    published = refresh_trust(
        bundle,
        sources=SSHTrustFiles((candidate.known_hosts_file, source), revocations),
        authority="fixture revocation owner",
        expected_generation=initial.generation,
    )
    assert published.generation is not None and published.generation != initial.generation
    retained.update(_policy_snapshot(bundle))
    retained.update(_snapshot((candidate.known_hosts_file, candidate.directory / "state.json")))
    _strict(migrated, marker, "accepted")
    assert marker.read_bytes() == b"x"

    update = tmp_path.resolve() / "complete-revocation-update"
    fixture_call(
        partial(
            subprocess.run,
            [
                "ssh-keygen",
                "-q",
                "-k",
                "-f",
                str(update),
                str(tmp_path.resolve() / "other-key.pub"),
                str(tmp_path.resolve() / "host-key.pub"),
            ],
            check=True,
            timeout=10,
        )
    )
    assert update.read_bytes() != revocations.read_bytes()
    retained.update(_snapshot((update,)))
    revocations.write_bytes(update.read_bytes())
    # Updating an owned source is not a subscription or implicit refresh.
    _unchanged(retained)
    assert trust_status(bundle) == published
    _strict(migrated, marker, "accepted")
    assert marker.read_bytes() == b"xx"

    block_trust(bundle, expected_generation=published.generation)
    _strict(migrated, marker, "blocked")
    generations = {path for path in bundle.directory.iterdir() if path.is_dir()}
    with pytest.raises(StateError):
        refresh_trust(
            bundle,
            sources=SSHTrustFiles((candidate.known_hosts_file, source, tmp_path.resolve() / "missing"), revocations),
            authority="fixture revocation owner",
            expected_generation=published.generation,
        )
    failed = trust_status(bundle)
    assert failed.blocked and failed.generation == published.generation
    partials = {path for path in bundle.directory.iterdir() if path.is_dir()} - generations
    assert len(partials) == 1
    failed_generation = partials.pop()
    assert (failed_generation / "known-hosts-0").read_bytes() == learned
    retained.update(_snapshot(tuple(failed_generation.iterdir())))
    _strict(migrated, marker, "blocked")
    assert marker.read_bytes() == b"xx"

    _restore(source_before)
    _unchanged(source_before)
    state = _snapshot((bundle.directory / "state.json",))
    with pytest.raises(StateError):
        refresh_trust(
            bundle,
            sources=SSHTrustFiles((source,), revocations),
            authority="stale fixture writer",
            expected_generation=initial.generation,
        )
    _unchanged(state)
    with pytest.raises(StateError):
        import_trust(
            bundle.directory, sources=SSHTrustFiles((source,), revocations), authority="rolled-back fixture source"
        )
    _unchanged(state)
    _strict(replace(migrated), marker, "blocked")
    assert marker.read_bytes() == b"xx"

    # Forward repair uses the retained complete update and learned key, never the
    # rolled-back unrevoked source or an older empty enrollment policy.
    repaired = refresh_trust(
        bundle,
        sources=SSHTrustFiles((candidate.known_hosts_file, source), update),
        authority="fixture revocation owner",
        expected_generation=failed.generation,
    )
    assert not repaired.blocked and repaired.generation not in {initial.generation, published.generation}
    selected = resolve_trust(bundle)
    assert selected.known_hosts[0].read_bytes() == learned
    assert selected.known_hosts[1].read_bytes() == source.read_bytes()
    assert selected.revoked_host_keys is not None and selected.revoked_host_keys.read_bytes() == update.read_bytes()
    retained.update(_policy_snapshot(bundle))
    _strict(migrated, marker, "refused")
    _restore(source_before)
    _strict(replace(migrated), marker, "refused")
    assert marker.read_bytes() == b"xx"
    assert trust_status(bundle) == repaired
    _unchanged(retained)
    _unchanged(originals)
    _unchanged(source_before)
