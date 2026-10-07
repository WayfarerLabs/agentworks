"""Installed-client acceptance of isolated-copy CA and revocation maintenance."""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import signal
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


def _policy_refusal(diagnostics: bytes, unknown_ca_line: bytes, revoked_line: bytes | None) -> str | None:
    """Recognize only complete upstream refusal sequences for these owned fixtures."""
    # OpenSSH sshconnect.c HOST_NEW and verify_host_key's SSH_ERR_KEY_REVOKED
    # branches, followed by sshconnect2.c verify_host_key_callback's fatal line:
    # https://github.com/openssh/openssh-portable/blob/V_8_5_P1/sshconnect.c#L1127-L1129
    # https://github.com/openssh/openssh-portable/blob/V_10_2_P1/sshconnect.c#L1517-L1521
    lines = tuple(diagnostics.splitlines())
    terminal = b"Host key verification failed."
    if lines == (unknown_ca_line, terminal):
        return "unknown_ca"
    if revoked_line is not None and lines == (revoked_line, terminal):
        return "host_revoked"
    return None


# Each application attempt runs in a fresh interpreter with retired execution
# modules unavailable. The parent never uses the CLI's unrelated import graph.
_STRICT_PROBE = (
    inspect.getsource(_policy_refusal)
    + r"""
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
from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution.carrier import Capture, CarrierIO, Deadline, Dispatch, ExitStatus, Failure, PreparedInvocation
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
custody = LocalDeliveryCustody()
try:
    result = SSHCarrier(connection).execute(
        invocation, io=CarrierIO(output=Capture(4096)), deadline=Deadline.after(10), custody=custody,
    )
finally:
    assert custody.close(Deadline.after(3))
classification = _policy_refusal(
    result.stderr.data, value["unknown_ca_line"].encode(),
    value["revoked_line"].encode() if value["revoked_line"] is not None else None,
) if result.stderr.complete else None
if value["expected"] == "accepted":
    passed = (result.dispatch == Dispatch.SENT and result.completion == ExitStatus(0)
        and result.failure is None and result.stdout.data == b"verified" and result.stdout.complete)
elif value["expected"] == "blocked":
    passed = (result.dispatch == Dispatch.NOT_SENT and result.local_status is None
        and result.completion is None and result.failure is not None)
else:
    passed = (value["expected"] in ("unknown_ca", "host_revoked") and classification == value["expected"]
        and result.local_status == 255 and result.completion is None and result.failure == Failure.OBSERVATION
        and result.stdout.complete and not result.stdout.data)
retired_loaded = any(name == module or name.startswith(module + ".") for name in sys.modules for module in retired)
report = json.dumps({
    "policy_refusal": classification, "stderr_complete": result.stderr.complete,
    "stdout_bytes": len(result.stdout.data), "stderr_bytes": len(result.stderr.data),
    "passed": passed, "retired_loaded": retired_loaded,
})
assert len(report.encode()) <= 2048
Path(value["observations"]).write_text(report)
assert passed and not retired_loaded
"""
)


def _probe_log(path: Path, returncode: int | None) -> bool:
    """Retain only validated bounded child observations, including on probe failure."""
    report = None
    try:
        with path.open("rb") as source:
            raw = source.read(2049)
        value = json.loads(raw) if len(raw) <= 2048 else None
        if (
            isinstance(value, dict)
            and set(value)
            == {"policy_refusal", "stderr_complete", "stdout_bytes", "stderr_bytes", "passed", "retired_loaded"}
            and value["policy_refusal"] in (None, "unknown_ca", "host_revoked")
            and all(type(value[name]) is bool for name in ("stderr_complete", "passed", "retired_loaded"))
            and all(type(value[name]) is int and 0 <= value[name] <= 4096 for name in ("stdout_bytes", "stderr_bytes"))
        ):
            report = value
    except (OSError, ValueError, RecursionError):
        pass
    safe = {"probe_returncode": returncode, "report_valid": report is not None}
    if returncode is None:
        safe["controller_observation_failed"] = True
    if report is not None:
        safe.update(report)
    path.write_text(json.dumps(safe))
    return bool(report is not None and returncode == 0 and report["passed"] and not report["retired_loaded"])


def _strict(connection: SSHConnection, marker: Path, expected: str, *, host_public_key: bytes | None = None) -> None:
    assert isinstance(connection.trust, ManagedSSHTrust)
    attempt = uuid.uuid4().hex
    # A 255 observation never authorizes retry of the same mutating invocation.
    # Refusal attempts each name their own effect and are issued exactly once.
    effect = marker if expected == "accepted" else marker.with_name(f"{marker.name}-refused-{attempt}")
    observations = marker.with_name(f"{marker.name}-probe-{attempt}.json")
    lookup = connection.host_key_alias or f"[{connection.host}]:{connection.port}"
    revoked_line = None
    if expected == "host_revoked":
        assert host_public_key is not None
        fingerprint = (
            base64.b64encode(hashlib.sha256(base64.b64decode(host_public_key.split()[1], validate=True)).digest())
            .rstrip(b"=")
            .decode()
        )
        revoked = resolve_trust(connection.trust).revoked_host_keys
        assert revoked is not None
        revoked_line = f"Host key ED25519 SHA256:{fingerprint} revoked by file {revoked}"
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
        "unknown_ca_line": f"No ED25519 host key is known for {lookup} and you have requested strict checking.",
        "revoked_line": revoked_line,
        "observations": str(observations),
    }
    try:
        returncode = fixture_call(
            partial(_retained_probe, [sys.executable, "-I", "-c", _STRICT_PROBE, json.dumps(value)])
        )
    except BaseException as error:
        try:
            _probe_log(observations, None)
        except BaseException as logging_error:
            raise BaseExceptionGroup("SSH probe and safe metadata retention failed", [error, logging_error]) from None
        raise
    # No client diagnostics or fixture key contents enter a failure message.
    assert _probe_log(observations, returncode), observations
    if expected != "accepted":
        assert not effect.exists()


def _retained_probe(argv: list[str]) -> int:
    """Keep the isolated controller until its SSH owner settles, even after expiry."""
    # This runs on the admitted fixture worker: main-thread control cannot detach
    # construction from its returned owner or release borrowed server/key paths.
    controller = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    errors: list[BaseException] = []
    try:
        return controller.wait(timeout=15)
    except BaseException as error:
        errors.append(error)
    # One cooperative request to this exact retained child, never a destructive
    # controller/group kill. A late successful result cannot erase prior expiry.
    try:
        controller.send_signal(signal.SIGINT)
    except BaseException as error:
        errors.append(error)
    while True:
        try:
            controller.wait()
            break
        except BaseException as error:
            errors.append(error)
    raise BaseExceptionGroup("Isolated SSH probe failed after retained controller settlement", errors)


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


@pytest.mark.integration
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
    _strict(migrated, marker, "unknown_ca")
    assert not marker.exists()

    # Include the explicit superseded-CA revocation in the complete replacement.
    source.write_bytes(replacement_policy + b"@revoked " + lookup + b" " + prior_public)
    replacement = source.read_bytes()
    _unchanged(retained)
    assert trust_status(bundle) == initial
    _strict(migrated, marker, "unknown_ca")
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


@pytest.mark.integration
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
    _strict(migrated, marker, "host_revoked", host_public_key=enrollment_sshd.host_public_key)
    _restore(source_before)
    _strict(replace(migrated), marker, "host_revoked", host_public_key=enrollment_sshd.host_public_key)
    assert marker.read_bytes() == b"xx"
    assert trust_status(bundle) == repaired
    _unchanged(retained)
    _unchanged(originals)
    _unchanged(source_before)
