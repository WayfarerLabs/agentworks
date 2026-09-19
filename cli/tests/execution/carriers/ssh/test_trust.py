"""Owned policy preserves bytes and refuses incomplete or superseded generations."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentworks.errors import StateError, ValidationError
from agentworks.execution.carriers.ssh import _trust_files as files
from agentworks.execution.carriers.ssh.trust import (
    ManagedSSHTrust,
    SSHTrustFiles,
    TrustBlockedError,
    TrustBlockUnprovenError,
    TrustBusyError,
    block_trust,
    import_trust,
    refresh_trust,
    resolve_trust,
    trust_status,
)

pytestmark = pytest.mark.windows


@pytest.fixture
def sources(tmp_path: Path) -> SSHTrustFiles:
    # The bytes are opaque here: installed OpenSSH tests own key interpretation.
    root = tmp_path.resolve()
    first = root / "operator-known-hosts"
    second = root / "system-known-hosts"
    revoked = root / "revoked.krl"
    first.write_bytes(b"# retained comment\r\n|1|hashed|record key\r\n[alias]:2222 key")
    second.write_bytes(b"@cert-authority *.example key\n@revoked host key\n")
    revoked.write_bytes(b"SSHKRL\n\0\0\xff\x80\r\n")
    return SSHTrustFiles((first, second), revoked)


def imported(tmp_path: Path, sources: SSHTrustFiles) -> ManagedSSHTrust:
    return import_trust(tmp_path.resolve() / "bundle", sources=sources, authority="fixture policy maintainer")


def test_values_are_passive_and_literal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        pytest.fail("Passive trust value inspected the filesystem")

    monkeypatch.setattr(Path, "stat", fail)
    monkeypatch.setattr(Path, "open", fail)
    SSHTrustFiles((tmp_path / "missing",), tmp_path / "missing-revocations")
    ManagedSSHTrust(tmp_path / "missing-bundle")
    with pytest.raises(ValidationError):
        SSHTrustFiles(())
    with pytest.raises(ValidationError):
        SSHTrustFiles((Path("relative"),))
    with pytest.raises(ValidationError):
        ManagedSSHTrust(tmp_path / ".." / "bundle")


def test_complete_files_and_metadata_survive_import_without_source_writes(
    tmp_path: Path, sources: SSHTrustFiles
) -> None:
    assert sources.revoked_host_keys is not None
    original = [
        (path, path.read_bytes(), path.stat().st_mtime_ns) for path in (*sources.known_hosts, sources.revoked_host_keys)
    ]
    bundle = imported(tmp_path, sources)
    status = trust_status(bundle)
    admitted = resolve_trust(bundle)
    assert status.sources == sources
    assert status.authority == "fixture policy maintainer"
    assert status.generation is not None and not status.blocked
    assert [path.read_bytes() for path in admitted.known_hosts] == [path.read_bytes() for path in sources.known_hosts]
    assert admitted.revoked_host_keys is not None
    assert admitted.revoked_host_keys.read_bytes() == sources.revoked_host_keys.read_bytes()
    assert all(path.parent == bundle.directory / status.generation for path in admitted.known_hosts)
    for path, content, timestamp in original:
        assert path.read_bytes() == content
        assert path.stat().st_mtime_ns == timestamp
    if os.name != "nt":
        assert bundle.directory.stat().st_mode & 0o777 == 0o700
        assert admitted.known_hosts[0].stat().st_mode & 0o777 == 0o600


def test_read_only_trust_is_checked_without_creating_a_bundle(tmp_path: Path, sources: SSHTrustFiles) -> None:
    before = set(tmp_path.iterdir())
    assert resolve_trust(sources) is sources
    assert set(tmp_path.iterdir()) == before
    sources.known_hosts[0].unlink()
    with pytest.raises(TrustBlockedError):
        resolve_trust(sources)


def test_existing_destination_is_never_replaced(tmp_path: Path, sources: SSHTrustFiles) -> None:
    bundle = imported(tmp_path, sources)
    before = trust_status(bundle)
    with pytest.raises(StateError):
        imported(tmp_path, sources)
    assert trust_status(bundle) == before
    resolve_trust(bundle)


def test_refresh_has_coherent_generations_and_refuses_stale_writer(tmp_path: Path, sources: SSHTrustFiles) -> None:
    bundle = imported(tmp_path, sources)
    before = trust_status(bundle)
    admitted = resolve_trust(bundle)
    previous = tuple(path.read_bytes() for path in admitted.known_hosts)
    sources.known_hosts[0].write_bytes(b"replacement policy")
    after = refresh_trust(
        bundle, sources=sources, authority="new named maintenance owner", expected_generation=before.generation
    )
    assert after.generation != before.generation
    assert after.authority == "new named maintenance owner"
    assert tuple(path.read_bytes() for path in admitted.known_hosts) == previous
    assert resolve_trust(bundle).known_hosts[0].read_bytes() == b"replacement policy"
    with pytest.raises(StateError):
        refresh_trust(bundle, sources=sources, authority="stale writer", expected_generation=before.generation)
    with pytest.raises(StateError):
        block_trust(bundle, expected_generation=before.generation)
    assert trust_status(bundle) == after


def test_explicit_block_requires_complete_refresh(tmp_path: Path, sources: SSHTrustFiles) -> None:
    bundle = imported(tmp_path, sources)
    before = trust_status(bundle)
    blocked = block_trust(bundle, expected_generation=before.generation)
    assert blocked.blocked and blocked.generation == before.generation
    with pytest.raises(TrustBlockedError):
        resolve_trust(bundle)
    after = refresh_trust(bundle, sources=sources, authority=before.authority, expected_generation=before.generation)
    assert not after.blocked and after.generation != before.generation
    resolve_trust(bundle)


@pytest.mark.parametrize("missing", ["known", "revoked"])
def test_failed_import_keeps_evidence_and_can_be_explicitly_repaired(
    tmp_path: Path, sources: SSHTrustFiles, missing: str
) -> None:
    path = sources.known_hosts[1] if missing == "known" else sources.revoked_host_keys
    assert path is not None
    content = path.read_bytes()
    path.unlink()
    with pytest.raises(StateError):
        imported(tmp_path, sources)
    bundle = ManagedSSHTrust(tmp_path.resolve() / "bundle")
    state = trust_status(bundle)
    assert state.blocked and state.generation is None
    assert list(bundle.directory.glob("*/known-hosts-0"))
    with pytest.raises(TrustBlockedError):
        resolve_trust(bundle)
    path.write_bytes(content)
    refresh_trust(bundle, sources=sources, authority=state.authority, expected_generation=None)
    resolve_trust(bundle)


@pytest.mark.parametrize(
    "failure", ["copy-first", "copy-second", "copy-revoked", "flush-generation", "publish", "after-publish"]
)
def test_refresh_failure_boundaries_remain_blocked(
    tmp_path: Path, sources: SSHTrustFiles, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    bundle = imported(tmp_path, sources)
    before = trust_status(bundle)
    admitted = resolve_trust(bundle)
    previous = [path.read_bytes() for path in admitted.known_hosts]
    copy_file = files.copy_file
    write_state = files.write_state
    sync_directory = files.sync_directory
    copies = 0

    def copy(source: Path, destination: Path) -> str:
        nonlocal copies
        copies += 1
        target = {"copy-first": 1, "copy-second": 2, "copy-revoked": 3}.get(failure)
        if copies == target:
            raise OSError("injected copy failure")
        return copy_file(source, destination)

    def write(directory: Path, state: dict[str, object]) -> None:
        if failure == "publish" and not state["blocked"]:
            raise OSError("injected publication failure")
        write_state(directory, state)
        if failure == "after-publish" and not state["blocked"]:
            raise OSError("injected post-replacement durability failure")

    def sync(directory: Path) -> None:
        if failure == "flush-generation" and directory.parent == bundle.directory:
            raise OSError("injected generation flush failure")
        sync_directory(directory)

    monkeypatch.setattr(files, "copy_file", copy)
    monkeypatch.setattr(files, "write_state", write)
    monkeypatch.setattr(files, "sync_directory", sync)
    with pytest.raises(StateError):
        refresh_trust(bundle, sources=sources, authority=before.authority, expected_generation=before.generation)
    assert trust_status(bundle).blocked
    assert trust_status(bundle).generation == before.generation
    assert [path.read_bytes() for path in admitted.known_hosts] == previous
    with pytest.raises(TrustBlockedError):
        resolve_trust(bundle)


def test_failure_to_record_initial_block_is_reported_without_claiming_refusal(
    tmp_path: Path, sources: SSHTrustFiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = imported(tmp_path, sources)
    before = trust_status(bundle)

    def fail(directory: Path, state: dict[str, object]) -> None:
        raise PermissionError("cannot persist new knowledge")

    monkeypatch.setattr(files, "write_state", fail)
    with pytest.raises(TrustBlockUnprovenError):
        refresh_trust(bundle, sources=sources, authority=before.authority, expected_generation=before.generation)
    assert trust_status(bundle) == before
    # The specific failure means callers must arrange quiescence; storage did not block admission.
    resolve_trust(bundle)


def test_interrupt_does_not_reactivate_previous_generation(
    tmp_path: Path, sources: SSHTrustFiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = imported(tmp_path, sources)
    before = trust_status(bundle)

    def interrupt(source: Path, destination: Path) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(files, "copy_file", interrupt)
    with pytest.raises(KeyboardInterrupt):
        refresh_trust(bundle, sources=sources, authority=before.authority, expected_generation=before.generation)
    assert trust_status(bundle).blocked


@pytest.mark.parametrize("damage", ["content", "missing", "manifest", "generation", "revoked", "traversal"])
def test_corrupt_or_missing_policy_never_admits(tmp_path: Path, sources: SSHTrustFiles, damage: str) -> None:
    bundle = imported(tmp_path, sources)
    admitted = resolve_trust(bundle)
    if damage == "content":
        admitted.known_hosts[0].write_bytes(b"changed without publication")
    elif damage == "missing":
        admitted.known_hosts[0].unlink()
    elif damage == "revoked":
        assert admitted.revoked_host_keys is not None
        admitted.revoked_host_keys.write_bytes(b"replacement revocations")
    elif damage == "manifest":
        (bundle.directory / "state.json").write_bytes(b"{partial")
    else:
        path = bundle.directory / "state.json"
        document = json.loads(path.read_bytes())
        document["generation"] = "0" * 32 if damage == "generation" else "../../elsewhere"
        path.write_text(json.dumps(document))
    with pytest.raises(TrustBlockedError):
        resolve_trust(bundle)


def test_source_mutation_during_copy_is_refused(
    tmp_path: Path, sources: SSHTrustFiles, monkeypatch: pytest.MonkeyPatch
) -> None:
    fsync = os.fsync
    changed = False

    def sync(descriptor: int) -> None:
        nonlocal changed
        fsync(descriptor)
        if not changed and list((tmp_path / "bundle").glob("*/known-hosts-0")):
            sources.known_hosts[0].write_bytes(b"concurrent source writer")
            changed = True

    monkeypatch.setattr(os, "fsync", sync)
    with pytest.raises(StateError):
        imported(tmp_path, sources)
    assert trust_status(ManagedSSHTrust(tmp_path.resolve() / "bundle")).blocked


def test_process_contention_and_death_release_permanent_lock(tmp_path: Path, sources: SSHTrustFiles) -> None:
    bundle = imported(tmp_path, sources)
    before = trust_status(bundle)
    script = """
import sys
from pathlib import Path
from agentworks.execution.carriers.ssh._trust_files import bundle_lock
with bundle_lock(Path(sys.argv[1])):
    print('held', flush=True)
    sys.stdin.read()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(bundle.directory)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lock_identity = (bundle.directory / "lock").stat().st_ino
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "held"
        with pytest.raises(TrustBusyError):
            resolve_trust(bundle)
        with pytest.raises(TrustBusyError):
            refresh_trust(bundle, sources=sources, authority=before.authority, expected_generation=before.generation)
    finally:
        process.kill()
        process.communicate(timeout=15)
    assert (bundle.directory / "lock").stat().st_ino == lock_identity
    assert trust_status(bundle) == before
    resolve_trust(bundle)


@pytest.mark.skipif(
    os.name == "nt", reason="Windows reparse creation requires privileges not present in every CI runner"
)
@pytest.mark.parametrize("target", ["source", "generation", "manifest", "lock", "bundle"])
def test_links_are_refused_without_following_or_overwriting_target(
    tmp_path: Path, sources: SSHTrustFiles, target: str
) -> None:
    if target == "source":
        original = sources.known_hosts[0]
        link = tmp_path / "linked-source"
        link.symlink_to(original)
        with pytest.raises(StateError):
            import_trust(tmp_path.resolve() / "bundle", sources=SSHTrustFiles((link,)), authority="fixture")
        assert original.read_bytes().startswith(b"# retained")
        return
    bundle = imported(tmp_path, sources)
    admitted = resolve_trust(bundle)
    path = {
        "generation": admitted.known_hosts[0].parent,
        "manifest": bundle.directory / "state.json",
        "lock": bundle.directory / "lock",
        "bundle": bundle.directory,
    }[target]
    original = path.with_name(path.name + "-original")
    path.rename(original)
    path.symlink_to(original, target_is_directory=original.is_dir())
    with pytest.raises(StateError):
        resolve_trust(bundle)


def test_process_death_mid_refresh_keeps_block_and_prior_evidence(tmp_path: Path, sources: SSHTrustFiles) -> None:
    bundle = imported(tmp_path, sources)
    before = trust_status(bundle)
    admitted = resolve_trust(bundle)
    script = """
import sys
from pathlib import Path
from agentworks.execution.carriers.ssh import _trust_files as files
from agentworks.execution.carriers.ssh.trust import ManagedSSHTrust, refresh_trust, trust_status
bundle = ManagedSSHTrust(Path(sys.argv[1]))
state = trust_status(bundle)
copy = files.copy_file
def pause(source, destination):
    result = copy(source, destination)
    print('copied', flush=True)
    sys.stdin.read()
    return result
files.copy_file = pause
refresh_trust(bundle, sources=state.sources, authority=state.authority, expected_generation=state.generation)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(bundle.directory)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "copied"
    finally:
        process.kill()
        process.communicate(timeout=15)
    assert trust_status(bundle).blocked
    assert admitted.known_hosts[0].read_bytes() == sources.known_hosts[0].read_bytes()
    with pytest.raises(TrustBlockedError):
        resolve_trust(bundle)
    refresh_trust(bundle, sources=sources, authority=before.authority, expected_generation=before.generation)
    resolve_trust(bundle)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and permission checks")
def test_nonprivate_owned_policy_refuses_admission(tmp_path: Path, sources: SSHTrustFiles) -> None:
    bundle = imported(tmp_path, sources)
    admitted = resolve_trust(bundle)
    admitted.known_hosts[0].chmod(0o666)
    with pytest.raises(StateError):
        resolve_trust(bundle)


@pytest.mark.skipif(os.name != "nt", reason="Native Windows sharing semantics")
def test_windows_manifest_sharing_failure_reports_unproven_block(tmp_path: Path, sources: SSHTrustFiles) -> None:
    bundle = imported(tmp_path, sources)
    before = trust_status(bundle)
    with files.read_file(bundle.directory / "state.json", owned=True), pytest.raises(TrustBlockUnprovenError):
        block_trust(bundle, expected_generation=before.generation)
    assert trust_status(bundle) == before
