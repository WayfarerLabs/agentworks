"""Local-only proof of the DOWNLOAD helper's guest effect fence."""

from __future__ import annotations

import multiprocessing
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentworks.db import Database, LifecycleObligationState, OperationResourceKind, OperationScope
from agentworks.errors import ValidationError
from agentworks.execution._file_download import FileDownloadStatus
from agentworks.execution._file_effect_gate import (
    FileEffectGateBinding,
    FileEffectGateError,
    advance_file_effect_gate,
    decode_file_effect_gate,
    encode_file_effect_gate,
    hold_file_effect_gate,
    inspect_file_effect_gate,
    setup_file_effect_gate,
)
from agentworks.execution._file_gate_setup import file_effect_gate_path
from agentworks.execution._file_obligation import (
    FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
    FileCallFamily,
    FileCallObligation,
    decode_file_call_obligation,
    encode_file_call_obligation,
)
from agentworks.execution._file_operation import FileOperation
from agentworks.execution._file_snapshot_exchange import (
    FileSnapshotObservationState,
    snapshot_begin,
    snapshot_cleanup,
    snapshot_reconcile,
    snapshot_stream,
)
from agentworks.execution._file_snapshot_protocol import FileSnapshotFailureCode
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_runs import ManagedTargetIdentity, ManagedTargetKind
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity, vm_guest_boot_id
from agentworks.execution.carrier import Deadline
from agentworks.operations import OperationOwner
from tests.execution.files._file_download_support import BytesSink
from tests.execution.files._file_snapshot_support import LocalCarrier, fixture_source, install_fixture_bundle
from tests.execution.files._runtime_support import runtime_selection

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the fixed snapshot helper requires Linux")


@pytest.fixture(autouse=True)
def _gate_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agentworks.execution._file_effect_gate as _file_effect_gate
    import agentworks.execution._file_gate_setup as _file_gate_setup

    namespace = tmp_path / "run" / "agentworks" / "file-gates-v1"
    (namespace / str(os.geteuid())).mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(_file_effect_gate, "_GATE_NAMESPACE", str(namespace))
    monkeypatch.setattr(_file_effect_gate, "_ROOT_UID", os.geteuid())
    monkeypatch.setattr(_file_gate_setup, "_NAMESPACE", str(namespace))


def _gate_path(tmp_path: Path) -> Path:
    name = Path(file_effect_gate_path(_target(), os.geteuid(), _GUEST)).name
    return tmp_path / "run" / "agentworks" / "file-gates-v1" / str(os.geteuid()) / name


_GUEST = VMGuestIdentity("a" * 32, "123e4567-e89b-12d3-a456-426614174000", 10)


def _observe_guest() -> VMGuestIdentity:
    return _GUEST


def _plan() -> IdentityPlan:
    groups = tuple(sorted(set(os.getgroups()) | {os.getegid()}))
    return IdentityPlan(IdentityExpectation(os.geteuid(), os.getegid(), groups), IdentityMode.DIRECT)


def _target() -> ManagedTargetIdentity:
    return ManagedTargetIdentity(ManagedTargetKind.VM, "gate-vm", "v1:" + "b" * 64, vm_guest_boot_id(_GUEST))


def _fixture(monkeypatch: pytest.MonkeyPatch, scratch: Path, guest: VMGuestIdentity = _GUEST) -> None:
    scratch.mkdir(exist_ok=True)
    scratch.chmod(0o1777)
    install_fixture_bundle(
        monkeypatch,
        scratch,
        f"""
guest._identity=lambda: guest._fixture_guest
from _agw_file_snapshot._vm_guest_identity_protocol import VMGuestIdentity
guest._fixture_guest=VMGuestIdentity(
    {guest.instance_marker!r}, {guest.boot_id!r}, {guest.init_start_ticks!r})
""",
    )


def _hold_until_released(binding: FileEffectGateBinding, entered: str, release: str) -> None:
    import agentworks.execution._file_effect_gate as _file_effect_gate

    _file_effect_gate._GATE_NAMESPACE = str(Path(binding.path).parent.parent)
    _file_effect_gate._ROOT_UID = os.geteuid()
    with hold_file_effect_gate(binding, _observe_guest):
        Path(entered).touch()
        while not Path(release).exists():
            time.sleep(0.01)


def _gate(tmp_path: Path) -> FileEffectGateBinding:
    gate = _gate_path(tmp_path)
    return setup_file_effect_gate(str(gate), _GUEST, os.geteuid(), "gate-vm", _observe_guest)


def _call(binding: FileEffectGateBinding, root: Path) -> FileCallObligation:
    return FileCallObligation(
        FileCallFamily.DOWNLOAD,
        _target(),
        str(root),
        "source",
        _plan(),
        runtime_selection(sys.executable),
        token=secrets.token_bytes(16),
        effect_gate=binding,
    )


def _crash_controller_with_fixed_snapshot(
    database_path: str,
    root_path: str,
    scratch_path: str,
    binding: FileEffectGateBinding,
    entered: str,
    release: str,
) -> None:
    import agentworks.execution._file_effect_gate as _file_effect_gate
    import agentworks.execution._file_gate_setup as _file_gate_setup
    import agentworks.execution._file_snapshot_exchange as _file_snapshot_exchange

    namespace = str(Path(binding.path).parent.parent)
    _file_effect_gate._GATE_NAMESPACE = namespace
    _file_effect_gate._ROOT_UID = os.geteuid()
    _file_gate_setup._NAMESPACE = namespace

    _file_snapshot_exchange.FIXED_BUNDLE = fixture_source(  # type: ignore[attr-defined]
        Path(scratch_path),
        f"""
import time
from _agw_file_snapshot._vm_guest_identity_protocol import VMGuestIdentity
guest._identity=lambda: VMGuestIdentity({_GUEST.instance_marker!r}, {_GUEST.boot_id!r}, {_GUEST.init_start_ticks!r})
_original_operate=guest._operate
def _held_operate(request, expires_at):
    result=_original_operate(request, expires_at)
    assert result is not None
    with open({binding.path!r}, 'rb') as same_inode_source:
        same_inode_source.read(1)
    bound_descriptors=[]
    for candidate in os.listdir('/proc/self/fd'):
        try:
            metadata=os.fstat(int(candidate))
        except OSError:
            continue
        if (metadata.st_dev, metadata.st_ino)==({binding.device!r}, {binding.inode!r}):
            bound_descriptors.append(candidate)
    assert len(bound_descriptors)==1
    open({entered!r}, 'wb').close()
    while not os.path.exists({release!r}):
        time.sleep(0.01)
    return result
guest._operate=_held_operate
""",
    )
    database = Database(Path(database_path))
    owner = OperationOwner.acquire(database.operations, OperationScope(OperationResourceKind.VM, "gate-vm"), "download")
    operation = FileOperation(owner, _call(binding, Path(root_path)).target)

    def crash_after_helper_enters() -> None:
        until = time.monotonic() + 10
        while not Path(entered).exists() and time.monotonic() < until:
            time.sleep(0.01)
        os._exit(91 if Path(entered).exists() else 92)

    threading.Thread(target=crash_after_helper_enters, daemon=True).start()
    operation.download(
        LocalCarrier(),
        trusted_root_path=root_path,
        relative_path=Path(binding.path).name,
        sink=BytesSink(),
        max_bytes=65_536,
        plan=_plan(),
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
        effect_gate=binding,
    )
    os._exit(93)


def test_binding_codec_and_exact_file_call_row_survive_reopen(tmp_path: Path) -> None:
    binding = _gate(tmp_path)
    metadata = Path(binding.path).stat()
    assert (binding.device, binding.inode) == (metadata.st_dev, metadata.st_ino)
    call = _call(binding, tmp_path)
    database = Database(tmp_path / "state.db")
    try:
        owner = OperationOwner.acquire(
            database.operations, OperationScope(OperationResourceKind.VM, "gate-vm"), "download"
        )
        owner.register_lifecycle_obligation(
            "file-call",
            payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
            payload=encode_file_call_obligation(call),
            obligation_id="a" * 32,
        )
        row = database.operations.list_lifecycle_obligations(owner.ownership)[0]
        assert decode_file_call_obligation(row.payload).effect_gate == binding
        proposed = replace(binding, proposed_generation=secrets.token_bytes(16))
        assert decode_file_effect_gate(encode_file_effect_gate(proposed)) == proposed
        with pytest.raises(ValidationError):
            FileOperation(owner, call.target).download(
                LocalCarrier(),
                trusted_root_path=str(tmp_path),
                relative_path="source",
                sink=BytesSink(),
                max_bytes=1024,
                plan=_plan(),
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
                effect_gate=replace(binding, scope_name="another-vm"),
            )
    finally:
        database.close()


def test_setup_observes_live_guest_before_creating_gate(tmp_path: Path) -> None:
    path = _gate_path(tmp_path)
    observed: list[bool] = []

    def stale_guest() -> VMGuestIdentity:
        observed.append(path.exists())
        return replace(_GUEST, init_start_ticks=11)

    with pytest.raises(FileEffectGateError):
        setup_file_effect_gate(str(path), _GUEST, os.geteuid(), "gate-vm", stale_guest)
    assert observed == [False]
    assert not path.exists()

    with pytest.raises(FileEffectGateError):
        setup_file_effect_gate(str(path), _GUEST, os.geteuid(), "", _observe_guest)
    assert not path.exists()


def test_gate_binding_numeric_wire_bounds_round_trip_and_reject_oversize(tmp_path: Path) -> None:
    maximum = replace(_gate(tmp_path), euid=(1 << 32) - 1, device=(1 << 64) - 1, inode=(1 << 64) - 1)
    encoded = encode_file_effect_gate(maximum)
    assert decode_file_effect_gate(encoded) == maximum

    for field, oversize in (("euid", 1 << 32), ("device", 1 << 64), ("inode", 1 << 64)):
        with pytest.raises(FileEffectGateError):
            if field == "euid":
                replace(maximum, euid=oversize)
            elif field == "device":
                replace(maximum, device=oversize)
            else:
                replace(maximum, inode=oversize)
        malformed = dict(encoded)
        malformed[field] = oversize
        with pytest.raises(FileEffectGateError):
            decode_file_effect_gate(malformed)


def test_setup_rejects_oversize_linux_uid_before_observation_or_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _gate_path(tmp_path)
    observed = False

    def observe() -> VMGuestIdentity:
        nonlocal observed
        observed = True
        return _GUEST

    monkeypatch.setattr(os, "geteuid", lambda: 1 << 32)
    with pytest.raises(FileEffectGateError):
        setup_file_effect_gate(str(path), _GUEST, 1 << 32, "gate-vm", observe)
    assert not observed
    assert not path.exists()


def test_setup_deadline_prevents_gate_creation_before_and_after_guest_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agentworks.execution._file_effect_gate as _file_effect_gate

    path = _gate_path(tmp_path)
    observations = 0
    now = [2.0]
    monkeypatch.setattr(_file_effect_gate, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=time.sleep))

    def observe() -> VMGuestIdentity:
        nonlocal observations
        observations += 1
        now[0] = 2.0
        return _GUEST

    with pytest.raises(FileEffectGateError):
        setup_file_effect_gate(str(path), _GUEST, os.geteuid(), "gate-vm", observe, expires_at=1.0)
    assert observations == 0
    assert not path.exists()

    now[0] = 0.0
    with pytest.raises(FileEffectGateError):
        setup_file_effect_gate(str(path), _GUEST, os.geteuid(), "gate-vm", observe, expires_at=1.0)
    assert observations == 1
    assert not path.exists()


def test_setup_deadline_before_commit_retains_incomplete_inode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import agentworks.execution._file_effect_gate as _file_effect_gate

    path = _gate_path(tmp_path)
    original = _file_effect_gate._connect
    now = [0.0]
    monkeypatch.setattr(_file_effect_gate, "time", SimpleNamespace(monotonic=lambda: now[0], sleep=time.sleep))

    def slow_connect(path: str, euid: int, descriptor: int, expected: tuple[int, int]):
        connection = original(path, euid, descriptor, expected)
        now[0] = 2.0
        return connection

    monkeypatch.setattr(_file_effect_gate, "_connect", slow_connect)
    with pytest.raises(FileEffectGateError):
        setup_file_effect_gate(str(path), _GUEST, os.geteuid(), "gate-vm", _observe_guest, expires_at=1.0)
    assert path.exists()
    inode = path.stat().st_ino
    with pytest.raises(FileEffectGateError):
        inspect_file_effect_gate(str(path), _GUEST, os.geteuid(), "gate-vm", _observe_guest)
    assert path.stat().st_ino == inode


def test_lost_setup_reply_can_inspect_exact_complete_gate_without_advancing(tmp_path: Path) -> None:
    binding = _gate(tmp_path)
    inspected = inspect_file_effect_gate(binding.path, _GUEST, os.geteuid(), "gate-vm", _observe_guest)
    assert inspected == binding
    assert (binding.device, binding.inode) == (
        Path(binding.path).stat().st_dev,
        Path(binding.path).stat().st_ino,
    )

    proposed = replace(binding, proposed_generation=secrets.token_bytes(16))
    advanced = advance_file_effect_gate(proposed, _observe_guest)
    assert inspect_file_effect_gate(binding.path, _GUEST, os.geteuid(), "gate-vm", _observe_guest) == advanced
    with hold_file_effect_gate(advanced, _observe_guest):
        pass


@pytest.mark.parametrize("unsafe", ["missing", "symlink", "uid_mode", "root_mode", "wrong_owner", "acl"])
def test_gate_namespace_refuses_unsafe_setup_without_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    path = _gate_path(tmp_path)
    uid_dir = path.parent
    namespace = uid_dir.parent
    if unsafe == "missing":
        uid_dir.rmdir()
    elif unsafe == "symlink":
        uid_dir.rmdir()
        uid_dir.symlink_to(tmp_path, target_is_directory=True)
    elif unsafe == "uid_mode":
        uid_dir.chmod(0o750)
    elif unsafe == "root_mode":
        namespace.parent.chmod(0o777)
    elif unsafe == "wrong_owner":
        original = os.lstat

        def wrong_owner(candidate: str):
            metadata = original(candidate)
            if candidate == str(namespace):
                return SimpleNamespace(st_mode=metadata.st_mode, st_uid=os.geteuid() + 1)
            return metadata

        monkeypatch.setattr(os, "lstat", wrong_owner)
    else:
        original_acl = os.getxattr

        def extra_acl(candidate: str, name: str, *, follow_symlinks: bool = True) -> bytes:
            if candidate == str(uid_dir) and name == "system.posix_acl_access":
                return b"unexpected"
            return original_acl(candidate, name, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(os, "getxattr", extra_acl)

    with pytest.raises(FileEffectGateError):
        setup_file_effect_gate(str(path), _GUEST, os.geteuid(), "gate-vm", _observe_guest)
    assert not path.exists()


def test_unsafe_namespace_refuses_inspect_advance_and_hold_without_changing_gate(tmp_path: Path) -> None:
    binding = _gate(tmp_path)
    before = Path(binding.path).read_bytes()
    Path(binding.path).parent.chmod(0o755)

    with pytest.raises(FileEffectGateError):
        inspect_file_effect_gate(binding.path, _GUEST, os.geteuid(), "gate-vm", _observe_guest)
    with pytest.raises(FileEffectGateError):
        advance_file_effect_gate(replace(binding, proposed_generation=b"z" * 16), _observe_guest)
    with pytest.raises(FileEffectGateError), hold_file_effect_gate(binding, _observe_guest):
        pass
    assert Path(binding.path).read_bytes() == before


def test_setup_adopts_exact_existing_gate_at_its_current_generation(tmp_path: Path) -> None:
    original = _gate(tmp_path)
    advanced = advance_file_effect_gate(replace(original, proposed_generation=b"c" * 16), _observe_guest)
    adopted = setup_file_effect_gate(original.path, _GUEST, os.geteuid(), "gate-vm", _observe_guest)
    assert adopted == advanced


@pytest.mark.parametrize("existing", ["incomplete", "wrong_epoch", "wrong_scope"])
def test_setup_never_repairs_or_replaces_unacceptable_existing_gate(tmp_path: Path, existing: str) -> None:
    path = _gate_path(tmp_path)
    if existing == "incomplete":
        path.touch(mode=0o600)
    elif existing == "wrong_epoch":
        guest = replace(_GUEST, init_start_ticks=11)
        setup_file_effect_gate(str(path), guest, os.geteuid(), "gate-vm", lambda: guest)
    else:
        setup_file_effect_gate(str(path), _GUEST, os.geteuid(), "other-vm", _observe_guest)
    before = path.stat()
    with pytest.raises(FileEffectGateError):
        setup_file_effect_gate(str(path), _GUEST, os.geteuid(), "gate-vm", _observe_guest)
    after = path.stat()
    assert (after.st_dev, after.st_ino, after.st_size) == (before.st_dev, before.st_ino, before.st_size)


def test_setup_does_not_inspect_after_nonexistence_unrelated_open_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.execution import _file_effect_gate

    path = _gate_path(tmp_path)
    original_open = os.open

    def denied_open(candidate: str, flags: int, mode: int = 0o777) -> int:
        if candidate == str(path):
            raise PermissionError
        return original_open(candidate, flags, mode)

    def unexpected_inspection(*args: object, **kwargs: object) -> FileEffectGateBinding:
        raise AssertionError("non-EEXIST setup failure must not inspect")

    monkeypatch.setattr(os, "open", denied_open)
    monkeypatch.setattr(_file_effect_gate, "inspect_file_effect_gate", unexpected_inspection)
    with pytest.raises(FileEffectGateError):
        setup_file_effect_gate(str(path), _GUEST, os.geteuid(), "gate-vm", _observe_guest)
    assert not path.exists()


def test_concurrent_setup_adopts_one_complete_inode(tmp_path: Path) -> None:
    path = _gate_path(tmp_path)
    start = threading.Barrier(3)
    bindings: list[FileEffectGateBinding] = []
    errors: list[BaseException] = []

    def setup() -> None:
        try:
            start.wait(timeout=10)
            bindings.append(setup_file_effect_gate(str(path), _GUEST, os.geteuid(), "gate-vm", _observe_guest))
        except BaseException as error:
            errors.append(error)

    workers = [threading.Thread(target=setup, daemon=True) for _ in range(2)]
    for worker in workers:
        worker.start()
    start.wait(timeout=10)
    for worker in workers:
        worker.join(timeout=10)
    assert not any(worker.is_alive() for worker in workers)
    # An inspector may win the tiny create-before-flock window and refuse the
    # incomplete inode. It must never create a second one or repair the first.
    assert len(bindings) >= 1
    assert len(bindings) + len(errors) == 2
    assert all(isinstance(error, FileEffectGateError) for error in errors)
    assert all(binding == bindings[0] for binding in bindings)
    assert inspect_file_effect_gate(str(path), _GUEST, os.geteuid(), "gate-vm", _observe_guest) == bindings[0]


def test_bookworm_python_can_setup_and_inspect_gate(tmp_path: Path) -> None:
    python = Path("/usr/bin/python3.11")
    if not python.is_file():
        pytest.skip("Bookworm Python 3.11 is unavailable on this host")
    source = f"""
import os
import sys
import agentworks.execution._file_effect_gate as _file_effect_gate
from agentworks.execution._file_effect_gate import setup_file_effect_gate, inspect_file_effect_gate
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity

_file_effect_gate._GATE_NAMESPACE = {str(_gate_path(tmp_path).parent.parent)!r}
_file_effect_gate._ROOT_UID = os.geteuid()
guest = VMGuestIdentity('a' * 32, '123e4567-e89b-12d3-a456-426614174000', 10)
def observe_guest():
    return guest
binding = setup_file_effect_gate(sys.argv[1], guest, os.geteuid(), 'gate-vm', observe_guest)
assert inspect_file_effect_gate(sys.argv[1], guest, os.geteuid(), 'gate-vm', observe_guest) == binding
"""
    result = subprocess.run(
        [str(python), "-c", source, str(_gate_path(tmp_path))],
        check=False,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[3])},
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_inspection_refuses_absent_incomplete_and_unsafe_gate(tmp_path: Path) -> None:
    path = _gate_path(tmp_path)

    def inspect() -> FileEffectGateBinding:
        return inspect_file_effect_gate(str(path), _GUEST, os.geteuid(), "gate-vm", _observe_guest)

    with pytest.raises(FileEffectGateError):
        inspect()
    assert not path.exists()

    path.touch(mode=0o600)
    with pytest.raises(FileEffectGateError):
        inspect()
    with pytest.raises(FileEffectGateError):
        setup_file_effect_gate(str(path), _GUEST, os.geteuid(), "gate-vm", _observe_guest)
    assert path.stat().st_size == 0

    path.rename(tmp_path / "incomplete.db")
    binding = _gate(tmp_path)
    os.link(path, tmp_path / "other-link.db")
    with pytest.raises(FileEffectGateError):
        inspect()
    (tmp_path / "other-link.db").unlink()
    path.rename(tmp_path / "original.db")
    path.symlink_to(tmp_path / "original.db")
    with pytest.raises(FileEffectGateError):
        inspect()
    path.unlink()
    (tmp_path / "original.db").rename(path)
    with pytest.raises(FileEffectGateError):
        inspect_file_effect_gate(binding.path, _GUEST, os.geteuid(), "wrong-vm", _observe_guest)
    with pytest.raises(FileEffectGateError):
        inspect_file_effect_gate(
            binding.path,
            _GUEST,
            os.geteuid(),
            "gate-vm",
            lambda: replace(_GUEST, init_start_ticks=11),
        )
    assert inspect() == binding


def test_inspection_waits_for_same_flock_and_respects_deadline(tmp_path: Path) -> None:
    binding = _gate(tmp_path)
    entered = tmp_path / "entered"
    release = tmp_path / "release"
    helper = multiprocessing.get_context("spawn").Process(
        target=_hold_until_released, args=(binding, str(entered), str(release))
    )
    helper.daemon = True
    helper.start()
    try:
        until = time.monotonic() + 10
        while not entered.exists() and time.monotonic() < until:
            time.sleep(0.01)
        assert entered.exists()
        with pytest.raises(FileEffectGateError):
            inspect_file_effect_gate(
                binding.path,
                _GUEST,
                os.geteuid(),
                "gate-vm",
                _observe_guest,
                expires_at=time.monotonic() + 0.05,
            )
        release.touch()
        helper.join(10)
        assert helper.exitcode == 0
        assert inspect_file_effect_gate(binding.path, _GUEST, os.geteuid(), "gate-vm", _observe_guest) == binding
    finally:
        failed = sys.exc_info()[0] is not None
        with suppress(OSError):
            release.touch()
        helper.join(1)
        if helper.is_alive():
            with suppress(OSError):
                helper.terminate()
            helper.join(1)
        if helper.is_alive():
            with suppress(OSError):
                helper.kill()
            helper.join(1)
        if not failed:
            assert helper.exitcode is not None


def test_fixed_snapshot_survives_controller_loss_and_is_fenced_before_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _gate(tmp_path)
    root = Path(binding.path).parent
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    scratch.chmod(0o1777)
    database_path = tmp_path / "owner.db"
    entered = tmp_path / "entered"
    release = tmp_path / "release"
    controller = multiprocessing.get_context("spawn").Process(
        target=_crash_controller_with_fixed_snapshot,
        args=(str(database_path), str(root), str(scratch), binding, str(entered), str(release)),
    )
    controller.start()
    try:
        controller.join(15)
        assert controller.exitcode == 91
        database = Database(database_path)
        try:
            predecessor = database.operations.inspect(OperationScope(OperationResourceKind.VM, "gate-vm"))
            assert predecessor is not None
            row = database.operations.list_lifecycle_obligations(predecessor.ownership)[0]
            call = decode_file_call_obligation(row.payload)
            assert row.state is LifecycleObligationState.POSSIBLE_EFFECT
            assert call.effect_gate == binding and call.token is not None
            assert (call.root, call.relative_path) == (str(root), Path(binding.path).name)
            source_metadata = Path(binding.path).stat()
            assert (source_metadata.st_dev, source_metadata.st_ino) == (binding.device, binding.inode)
            proposed = replace(binding, proposed_generation=secrets.token_bytes(16))
            owner = OperationOwner.recover(database.operations, predecessor.ownership, "b" * 32)
            bound = owner.rebind_lifecycle_obligation(
                row.obligation_id, "file-call", payload_version=row.payload_version, payload=row.payload
            )
            bound.publish_payload(
                expected_revision=row.payload_revision,
                payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
                payload=encode_file_call_obligation(replace(call, effect_gate=proposed)),
            )
            with pytest.raises(FileEffectGateError):
                advance_file_effect_gate(proposed, _observe_guest)
            release.touch()
            until = time.monotonic() + 15
            advanced = None
            while advanced is None and time.monotonic() < until:
                try:
                    advanced = advance_file_effect_gate(proposed, _observe_guest)
                except FileEffectGateError:
                    time.sleep(0.05)
            assert advanced is not None
            # Simulate an acknowledgment lost after the guest committed.
            assert advance_file_effect_gate(proposed, _observe_guest) == advanced
            pending_row = database.operations.list_lifecycle_obligations(owner.ownership)[0]
            confirmed = owner.rebind_lifecycle_obligation(
                pending_row.obligation_id,
                "file-call",
                payload_version=pending_row.payload_version,
                payload=pending_row.payload,
            )
            confirmed.publish_payload(
                expected_revision=pending_row.payload_revision,
                payload_version=FILE_CALL_OBLIGATION_PAYLOAD_VERSION,
                payload=encode_file_call_obligation(replace(call, effect_gate=advanced)),
            )
            final_row = database.operations.list_lifecycle_obligations(owner.ownership)[0]
            assert decode_file_call_obligation(final_row.payload).effect_gate == advanced
            install_fixture_bundle(
                monkeypatch,
                scratch,
                f"""
from _agw_file_snapshot._vm_guest_identity_protocol import VMGuestIdentity
guest._identity=lambda: VMGuestIdentity({_GUEST.instance_marker!r}, {_GUEST.boot_id!r}, {_GUEST.init_start_ticks!r})
""",
            )
            delayed = snapshot_begin(
                LocalCarrier(),
                trusted_root_path=str(root),
                relative_path=Path(binding.path).name,
                max_bytes=65_536,
                token=call.token,
                plan=_plan(),
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
                effect_gate=binding,
            )
            assert delayed.observation is not None and delayed.observation.failure is not None
            assert delayed.observation.failure.code is FileSnapshotFailureCode.EFFECT_GATE_REFUSED
            reconciled = snapshot_reconcile(
                LocalCarrier(),
                token=call.token,
                plan=_plan(),
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
                effect_gate=advanced,
            )
            assert reconciled.observation is not None
            assert reconciled.observation.state is FileSnapshotObservationState.RECOVERED
            debt = reconciled.observation.cleanup_debt
            assert debt is not None
            cleaned = snapshot_cleanup(
                LocalCarrier(),
                token=call.token,
                cleanup_debt=debt,
                plan=_plan(),
                deadline=Deadline.after(30),
                runtime_selection=runtime_selection(sys.executable),
                effect_gate=advanced,
            )
            assert cleaned.observation is not None and cleaned.observation.state is FileSnapshotObservationState.CLEANED
        finally:
            database.close()
    finally:
        release.touch()
        controller.join(10)


def test_real_download_persists_gate_before_dispatch_and_retains_missing_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _gate(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    (root / "source").write_bytes(b"gated source")
    _fixture(monkeypatch, tmp_path / "scratch")
    database = Database(tmp_path / "owner.db")
    try:
        owner = OperationOwner.acquire(
            database.operations, OperationScope(OperationResourceKind.VM, "gate-vm"), "download"
        )
        operation = FileOperation(owner, _call(binding, root).target)

        class InspectingCarrier(LocalCarrier):
            def execute(self, invocation, *, io, deadline):
                rows = tuple(
                    row
                    for row in database.operations.list_lifecycle_obligations(owner.ownership)
                    if row.state is LifecycleObligationState.POSSIBLE_EFFECT
                )
                assert len(rows) == 1
                assert decode_file_call_obligation(rows[0].payload).effect_gate == binding
                return super().execute(invocation, io=io, deadline=deadline)

        sink = BytesSink()
        completed = operation.download(
            InspectingCarrier(),
            trusted_root_path=str(root),
            relative_path="source",
            sink=sink,
            max_bytes=1024,
            plan=_plan(),
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
            effect_gate=binding,
        )
        assert completed.status is FileDownloadStatus.COMPLETE
        Path(binding.path).rename(tmp_path / "missing-gate.db")
        refused = operation.download(
            InspectingCarrier(),
            trusted_root_path=str(root),
            relative_path="source",
            sink=BytesSink(),
            max_bytes=1024,
            plan=_plan(),
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
            effect_gate=binding,
        )
        assert refused.status is FileDownloadStatus.UNCERTAIN
        assert refused.requires_owner_retention
        assert not Path(binding.path).exists()
    finally:
        database.close()


def test_active_helper_blocks_advance_and_delayed_old_generation_refuses(tmp_path: Path) -> None:
    binding = _gate(tmp_path)
    entered = tmp_path / "entered"
    release = tmp_path / "release"
    helper = multiprocessing.get_context("spawn").Process(
        target=_hold_until_released, args=(binding, str(entered), str(release))
    )
    helper.start()
    try:
        until = time.monotonic() + 10
        while not entered.exists() and time.monotonic() < until:
            time.sleep(0.01)
        assert entered.exists()
        proposed = replace(binding, proposed_generation=secrets.token_bytes(16))
        # The original flock descriptor prevents takeover during the effect.
        expires_at = time.monotonic() + 0.05
        with pytest.raises(FileEffectGateError):
            advance_file_effect_gate(proposed, _observe_guest, expires_at=expires_at)
        assert time.monotonic() >= expires_at
        expires_at = time.monotonic() + 0.05
        with pytest.raises(FileEffectGateError), hold_file_effect_gate(binding, _observe_guest, expires_at=expires_at):
            raise AssertionError("second helper must not enter before its deadline")
        release.touch()
        helper.join(10)
        assert helper.exitcode == 0
        advanced = advance_file_effect_gate(proposed, _observe_guest)
        with pytest.raises(FileEffectGateError), hold_file_effect_gate(binding, _observe_guest):
            raise AssertionError("old helper must not enter")
        with hold_file_effect_gate(advanced, _observe_guest):
            pass
    finally:
        release.touch()
        helper.join(10)


def test_lost_advance_reply_reconciles_but_stale_advance_cannot_overwrite(tmp_path: Path) -> None:
    binding = _gate(tmp_path)
    first = replace(binding, proposed_generation=secrets.token_bytes(16))
    advanced = advance_file_effect_gate(first, _observe_guest)
    assert advance_file_effect_gate(first, _observe_guest) == advanced
    second = replace(advanced, proposed_generation=secrets.token_bytes(16))
    newest = advance_file_effect_gate(second, _observe_guest)
    with pytest.raises(FileEffectGateError):
        advance_file_effect_gate(first, _observe_guest)
    with hold_file_effect_gate(newest, _observe_guest):
        pass


def test_slow_guest_observation_cannot_advance_or_admit_after_deadline(tmp_path: Path) -> None:
    binding = _gate(tmp_path)
    proposed = replace(binding, proposed_generation=secrets.token_bytes(16))

    def slow_observation() -> VMGuestIdentity:
        time.sleep(0.05)
        return _GUEST

    with pytest.raises(FileEffectGateError):
        advance_file_effect_gate(proposed, slow_observation, expires_at=time.monotonic() + 0.01)
    entered = False
    with (
        pytest.raises(FileEffectGateError),
        hold_file_effect_gate(binding, slow_observation, expires_at=time.monotonic() + 0.01),
    ):
        entered = True
    assert not entered
    with hold_file_effect_gate(binding, _observe_guest):
        pass
    assert advance_file_effect_gate(proposed, _observe_guest).generation == proposed.proposed_generation


def test_deadline_during_sqlite_generation_check_rolls_back_before_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agentworks.execution._file_effect_gate as _file_effect_gate

    binding = _gate(tmp_path)
    proposed = replace(binding, proposed_generation=secrets.token_bytes(16))
    original = _file_effect_gate._record

    def slow_record(connection, expected):
        generation = original(connection, expected)
        time.sleep(0.05)
        return generation

    monkeypatch.setattr(_file_effect_gate, "_record", slow_record)
    with pytest.raises(FileEffectGateError):
        advance_file_effect_gate(proposed, _observe_guest, expires_at=time.monotonic() + 0.01)
    monkeypatch.setattr(_file_effect_gate, "_record", original)
    with hold_file_effect_gate(binding, _observe_guest):
        pass
    assert advance_file_effect_gate(proposed, _observe_guest).generation == proposed.proposed_generation


def test_missing_replaced_or_wrong_guest_state_refuses_in_same_epoch(tmp_path: Path) -> None:
    binding = _gate(tmp_path)
    wrong_guest = replace(_GUEST, init_start_ticks=11)
    with pytest.raises(FileEffectGateError), hold_file_effect_gate(binding, lambda: wrong_guest):
        pass
    original = tmp_path / "old-effect.db"
    Path(binding.path).rename(original)
    with pytest.raises(FileEffectGateError), hold_file_effect_gate(binding, _observe_guest):
        pass
    shutil.copyfile(original, binding.path)
    Path(binding.path).chmod(0o600)
    assert Path(binding.path).read_bytes() == original.read_bytes()
    assert Path(binding.path).stat().st_ino != binding.inode
    with pytest.raises(FileEffectGateError), hold_file_effect_gate(binding, _observe_guest):
        pass


def test_fixed_snapshot_helper_checks_independent_guest_and_fences_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _gate(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    (root / "source").write_bytes(b"file-effect-gate")
    scratch = tmp_path / "scratch"
    _fixture(monkeypatch, scratch)
    plan = _plan()
    token = secrets.token_bytes(16)

    def begin(gate: FileEffectGateBinding):
        return snapshot_begin(
            LocalCarrier(),
            trusted_root_path=str(root),
            relative_path="source",
            max_bytes=1024,
            token=token,
            plan=plan,
            deadline=Deadline.after(30),
            runtime_selection=runtime_selection(sys.executable),
            effect_gate=gate,
        )

    begun = begin(binding)
    assert begun.observation is not None and begun.observation.state is FileSnapshotObservationState.READY
    assert begun.observation.snapshot is not None
    from agentworks.execution._scratch import _cleanup_debt

    cleanup_debt = _cleanup_debt(begun.observation.snapshot.ready)
    proposed = replace(binding, proposed_generation=secrets.token_bytes(16))
    advanced = advance_file_effect_gate(proposed, _observe_guest)
    received = bytearray()

    def collect(block: bytes) -> bool:
        received.extend(block)
        return True

    refused_stream = snapshot_stream(
        LocalCarrier(live_stdio=True),
        token=token,
        ready=begun.observation.snapshot.ready,
        write_data=collect,
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
        effect_gate=binding,
    )
    assert refused_stream.observation is not None
    assert refused_stream.observation.failure is not None
    assert refused_stream.observation.failure.code is FileSnapshotFailureCode.EFFECT_GATE_REFUSED
    assert received == bytearray() and tuple(scratch.iterdir())
    delayed = begin(binding)
    assert delayed.observation is not None
    assert delayed.observation.failure is not None
    assert delayed.observation.failure.code is FileSnapshotFailureCode.EFFECT_GATE_REFUSED
    refused_cleanup = snapshot_cleanup(
        LocalCarrier(),
        token=token,
        cleanup_debt=cleanup_debt,
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
        effect_gate=binding,
    )
    assert refused_cleanup.observation is not None
    assert refused_cleanup.observation.failure is not None
    assert refused_cleanup.observation.failure.code is FileSnapshotFailureCode.EFFECT_GATE_REFUSED
    assert tuple(scratch.iterdir())
    cleaned = snapshot_cleanup(
        LocalCarrier(),
        token=token,
        cleanup_debt=cleanup_debt,
        plan=plan,
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
        effect_gate=advanced,
    )
    assert cleaned.observation is not None and cleaned.observation.state is FileSnapshotObservationState.CLEANED
    assert not tuple(scratch.iterdir())

    install_fixture_bundle(
        monkeypatch,
        scratch,
        f"""
guest._identity=lambda: guest._fixture_guest
from _agw_file_snapshot._vm_guest_identity_protocol import VMGuestIdentity
guest._fixture_guest=VMGuestIdentity(
    {_GUEST.instance_marker!r}, {_GUEST.boot_id!r}, {(_GUEST.init_start_ticks + 1)!r})
""",
    )
    wrong_epoch = begin(advanced)
    assert wrong_epoch.observation is not None
    assert wrong_epoch.observation.failure is not None
    assert wrong_epoch.observation.failure.code is FileSnapshotFailureCode.EFFECT_GATE_REFUSED
