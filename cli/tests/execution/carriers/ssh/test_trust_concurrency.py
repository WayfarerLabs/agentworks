"""Readers overlap while publication and failure recovery remain exclusive."""

from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, current_thread

import pytest

from agentworks.errors import StateError
from agentworks.execution.carriers.ssh import _trust_files as files
from agentworks.execution.carriers.ssh.trust import (
    ManagedSSHTrust,
    SSHTrustFiles,
    TrustBlockedError,
    TrustBusyError,
    block_trust,
    import_trust,
    refresh_trust,
    resolve_trust,
    trust_status,
)

pytestmark = pytest.mark.windows


@pytest.fixture
def bundle(tmp_path: Path) -> ManagedSSHTrust:
    source = tmp_path.resolve() / "source"
    source.write_bytes(b"complete old policy")
    return import_trust(tmp_path.resolve() / "bundle", sources=SSHTrustFiles((source,)), authority="fixture")


def test_parallel_readers_exclude_writer_and_keep_admitted_generation(
    bundle: ManagedSSHTrust, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = trust_status(bundle)
    original = resolve_trust(bundle)
    hashing = Event()
    release = Event()
    file_hash = files.file_hash
    caller = current_thread()

    def pause(path: Path, *, owned: bool = True) -> str:
        if current_thread() is not caller:
            hashing.set()
            assert release.wait(15)
        return file_hash(path, owned=owned)

    monkeypatch.setattr(files, "file_hash", pause)
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(resolve_trust, bundle)
        try:
            assert hashing.wait(15)
            # The first admission is still checking integrity. The second
            # admission and status observation must share its reader lock.
            assert resolve_trust(bundle) == original
            assert trust_status(bundle) == before
            with pytest.raises(TrustBusyError):
                block_trust(bundle, expected_generation=before.generation)
            with pytest.raises(TrustBusyError):
                refresh_trust(
                    bundle,
                    sources=before.sources,
                    authority=before.authority,
                    expected_generation=before.generation,
                )
        finally:
            release.set()
        admitted = pending.result(timeout=15)
    assert admitted == original
    before.sources.known_hosts[0].write_bytes(b"complete replacement policy")
    after = refresh_trust(
        bundle, sources=before.sources, authority=before.authority, expected_generation=before.generation
    )
    assert after.generation != before.generation
    assert resolve_trust(bundle).known_hosts[0].read_bytes() == b"complete replacement policy"
    assert admitted.known_hosts[0].read_bytes() == b"complete old policy"


@pytest.mark.parametrize("stage", ["copy", "after-replace"])
@pytest.mark.parametrize("fail", [False, True])
def test_writer_excludes_admission_until_publication_or_failure_reblocking_finishes(
    bundle: ManagedSSHTrust, monkeypatch: pytest.MonkeyPatch, stage: str, fail: bool
) -> None:
    before = trust_status(bundle)
    original = resolve_trust(bundle)
    before.sources.known_hosts[0].write_bytes(b"complete replacement policy")
    publication = Event()
    release = Event()
    copy_file = files.copy_file
    sync_directory = files.sync_directory
    paused = False

    def pause() -> None:
        nonlocal paused
        if paused:
            return
        paused = True
        publication.set()
        assert release.wait(15)
        if fail:
            raise OSError("injected publication failure")

    def copy(source: Path, destination: Path) -> str:
        result = copy_file(source, destination)
        if stage == "copy":
            pause()
        return result

    def sync(directory: Path) -> None:
        # The active manifest has been replaced, but its directory flush has
        # not completed. Readers must not inspect that unconfirmed publication.
        if stage == "after-replace" and directory == bundle.directory:
            import json

            state = json.loads((directory / "state.json").read_bytes())
            if not state["blocked"]:
                pause()
        sync_directory(directory)

    monkeypatch.setattr(files, "copy_file", copy)
    monkeypatch.setattr(files, "sync_directory", sync)
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(
            refresh_trust,
            bundle,
            sources=before.sources,
            authority=before.authority,
            expected_generation=before.generation,
        )
        try:
            assert publication.wait(15)
            with pytest.raises(TrustBusyError):
                resolve_trust(bundle)
            with pytest.raises(TrustBusyError):
                trust_status(bundle)
            assert original.known_hosts[0].read_bytes() == b"complete old policy"
        finally:
            release.set()
        if fail:
            with pytest.raises(StateError):
                pending.result(timeout=15)
        else:
            assert not pending.result(timeout=15).blocked
    if fail:
        state = trust_status(bundle)
        assert state.blocked and state.generation == before.generation
        with pytest.raises(TrustBlockedError):
            resolve_trust(bundle)
    else:
        assert resolve_trust(bundle).known_hosts[0].read_bytes() == b"complete replacement policy"
    assert original.known_hosts[0].read_bytes() == b"complete old policy"


def test_cross_process_shared_reader_excludes_writer_and_death_releases_same_lock(
    bundle: ManagedSSHTrust,
) -> None:
    before = trust_status(bundle)
    script = """
import sys
from pathlib import Path
from agentworks.execution.carriers.ssh._trust_files import bundle_lock
with bundle_lock(Path(sys.argv[1]), shared=True):
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
    identity = (bundle.directory / "lock").stat().st_ino
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "held"
        assert resolve_trust(bundle).known_hosts[0].read_bytes() == b"complete old policy"
        assert trust_status(bundle) == before
        with pytest.raises(TrustBusyError):
            block_trust(bundle, expected_generation=before.generation)
    finally:
        process.kill()
        process.communicate(timeout=15)
    assert (bundle.directory / "lock").stat().st_ino == identity
    assert block_trust(bundle, expected_generation=before.generation).blocked


def test_passive_owner_retains_lock_when_native_acquisition_is_interrupted(
    bundle: ManagedSSHTrust,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = files.BundleLock(bundle.directory)
    assert owner.settled
    if sys.platform == "win32":
        native_lock = files._windows_lock

        def interrupted_windows(descriptor: int, *, shared: bool) -> None:
            native_lock(descriptor, shared=shared)
            raise KeyboardInterrupt

        monkeypatch.setattr(files, "_windows_lock", interrupted_windows)
    else:
        import fcntl

        flock = fcntl.flock

        def interrupted_posix(descriptor: int, operation: int) -> None:
            flock(descriptor, operation)
            raise KeyboardInterrupt

        monkeypatch.setattr(fcntl, "flock", interrupted_posix)
    with pytest.raises(KeyboardInterrupt):
        owner.acquire()
    assert not owner.settled
    with pytest.raises(TrustBusyError):
        resolve_trust(bundle)
    assert owner.release() and owner.settled
    monkeypatch.undo()
    resolve_trust(bundle)


def test_failed_contention_keeps_descriptor_until_explicit_owner_release(bundle: ManagedSSHTrust) -> None:
    owner = files.BundleLock(bundle.directory)
    with files.bundle_lock(bundle.directory, shared=True):
        with pytest.raises(TrustBusyError):
            owner.acquire()
        assert not owner.settled
        assert owner.release() and owner.settled
        resolve_trust(bundle)
    owner.acquire()
    assert owner.release()


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_interrupted_open_without_captured_descriptor_never_claims_settlement(
    bundle: ManagedSSHTrust,
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException],
) -> None:
    import os

    owner = files.BundleLock(bundle.directory)
    control = interruption()

    def interrupted_open(path: object, flags: int) -> int:
        raise control

    monkeypatch.setattr(os, "open", interrupted_open)
    with pytest.raises(interruption) as raised:
        owner.acquire()
    assert raised.value is control
    assert not owner.settled and not owner.release()
    with pytest.raises(StateError):
        owner.acquire()


@pytest.mark.parametrize("failure", [OSError, KeyboardInterrupt, SystemExit])
def test_ambiguous_close_never_retries_a_potentially_reused_descriptor(
    bundle: ManagedSSHTrust,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    import os

    owner = files.BundleLock(bundle.directory)
    owner.acquire()
    close = os.close
    released: list[int] = []
    control = failure()

    def uncertain_close(descriptor: int) -> None:
        close(descriptor)
        released.append(descriptor)
        raise control

    monkeypatch.setattr(os, "close", uncertain_close)
    if failure is OSError:
        assert not owner.release()
    else:
        with pytest.raises(failure) as raised:
            owner.release()
        assert raised.value is control
    assert not owner.settled
    assert len(released) == 1
    monkeypatch.setattr(os, "close", close)
    unrelated = os.open(bundle.directory / "lock", os.O_RDWR)
    try:
        if unrelated != released[0]:
            os.dup2(unrelated, released[0])
        assert not owner.release()
        assert os.fstat(released[0]).st_size == 1
    finally:
        close(unrelated)
        if unrelated != released[0]:
            close(released[0])
    resolve_trust(bundle)


@pytest.mark.parametrize("body_failure", [StateError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("release_failure", [KeyboardInterrupt, SystemExit])
def test_context_cleanup_preserves_control_flow_when_release_is_interrupted(
    bundle: ManagedSSHTrust,
    monkeypatch: pytest.MonkeyPatch,
    body_failure: type[BaseException],
    release_failure: type[BaseException],
) -> None:
    import os

    close = os.close
    original = body_failure("injected body failure")
    cleanup = release_failure()

    def interrupted_close(descriptor: int) -> None:
        close(descriptor)
        raise cleanup

    monkeypatch.setattr(os, "close", interrupted_close)
    expected = original if isinstance(original, (KeyboardInterrupt, SystemExit)) else cleanup
    with pytest.raises(type(expected)) as raised, files.bundle_lock(bundle.directory):
        raise original
    assert raised.value is expected
    assert raised.value.__notes__
