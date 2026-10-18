"""Real fixed-bundle checks for Linux metadata operations."""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agentworks.execution._delivery_custody import LocalDeliveryCustody
from agentworks.execution._file_metadata import MetadataStep
from agentworks.execution._file_metadata_bundle import _MODULE_NAMES, _PACKAGE, FIXED_BUNDLE
from agentworks.execution._file_metadata_exchange import (
    FileMetadataCandidateResult,
    FileMetadataObservationState,
    create_file_directory,
    ensure_file_directory,
    set_file_metadata,
)
from agentworks.execution._file_metadata_protocol import FileMetadataFailureCode
from agentworks.execution._helper_bundle import FixedFileHelperBundle
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState
from agentworks.execution.carrier import (
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    PreparedInvocation,
)
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, ProxmoxConnection
from tests.execution._bound_carrier_support import bind_carrier as bind_carrier
from tests.execution._bound_carrier_support import fixture_dispatch, run_fixture_process
from tests.execution.files._fixed_bundle_support import fixture_file_bundle
from tests.execution.files._runtime_support import runtime_selection

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the metadata helper requires Linux")


class LocalCarrier:
    def __init__(self, *, guest_deadline_grace: bool = False) -> None:
        self.local_delivery = LocalDeliveryCustody()
        self.calls = 0
        self.invocation: PreparedInvocation | None = None
        self.io: CarrierIO | None = None
        self._guest_deadline_grace = guest_deadline_grace

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def validate(self, invocation: PreparedInvocation, *, io: CarrierIO) -> None:
        del invocation, io

    def execute(
        self,
        invocation: PreparedInvocation,
        *,
        io: CarrierIO,
        deadline: Deadline,
        custody: LocalDeliveryCustody | None = None,
    ) -> CarrierReport:
        self.validate(invocation, io=io)
        self.calls += 1
        self.invocation = invocation
        self.io = io
        process_deadline = Deadline.after(2) if self._guest_deadline_grace else deadline
        result = run_fixture_process(
            list(invocation.argv),
            io=io,
            deadline=process_deadline,
            custody=custody,
            standalone_custody=self.local_delivery,
        )
        completion = None
        if result.exit_status is not None:
            completion = (
                ExitStatus(signal=-result.exit_status)
                if result.exit_status < 0
                else ExitStatus(code=result.exit_status)
            )
        return CarrierReport(
            fixture_dispatch(result),
            completion,
            result.local_status,
            result.stdout,
            result.stderr,
            result.failure,
        )


@pytest.fixture
def plan() -> IdentityPlan:
    return IdentityPlan(
        IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()}))),
        IdentityMode.DIRECT,
    )


def _fixture_source(injection: str = "") -> FixedFileHelperBundle:
    patch_source = f"import os\ng=guest\nm=sys.modules[{(_PACKAGE + '._file_metadata')!r}]\n" + injection
    return fixture_file_bundle(_PACKAGE, _MODULE_NAMES, "_file_metadata_guest", patch_source)


def _set(
    root: Path,
    relative: str,
    plan: IdentityPlan,
    source: FixedFileHelperBundle,
    *,
    mode: int,
    runtime: Path = Path(sys.executable),
    deadline: float = 15,
    carrier: LocalCarrier | None = None,
) -> tuple[LocalCarrier, FileMetadataCandidateResult]:
    carrier = LocalCarrier() if carrier is None else carrier
    with patch("agentworks.execution._file_metadata_exchange.FIXED_BUNDLE", source):
        result = set_file_metadata(
            carrier,
            trusted_root_path=str(root),
            relative_path=relative,
            uid=os.geteuid(),
            gid=os.getegid(),
            mode=mode,
            plan=plan,
            deadline=Deadline.after(deadline),
            runtime_selection=runtime_selection(str(runtime)),
        )
    return carrier, result


def _ensure(
    root: Path,
    relative: str,
    plan: IdentityPlan,
    source: FixedFileHelperBundle,
    *,
    mode: int,
    runtime: Path = Path(sys.executable),
    deadline: float = 15,
    create_only: bool = False,
) -> tuple[LocalCarrier, FileMetadataCandidateResult]:
    carrier = LocalCarrier()
    with patch("agentworks.execution._file_metadata_exchange.FIXED_BUNDLE", source):
        function = create_file_directory if create_only else ensure_file_directory
        result = function(
            carrier,
            trusted_root_path=str(root),
            relative_path=relative,
            uid=os.geteuid(),
            gid=os.getegid(),
            mode=mode,
            plan=plan,
            deadline=Deadline.after(deadline),
            runtime_selection=runtime_selection(str(runtime)),
        )
    return carrier, result


@pytest.mark.parametrize("runtime", [Path(sys.executable), Path("/usr/bin/python3.11")])
def test_create_only_fixed_bundle_refuses_existing_and_missing_parent(
    tmp_path: Path, plan: IdentityPlan, runtime: Path
) -> None:
    if not runtime.is_file():
        pytest.skip(f"compatibility interpreter is unavailable: {runtime}")
    source = _fixture_source()
    parent = tmp_path / "parent"
    parent.mkdir()
    _, created = _ensure(tmp_path, "parent/created", plan, source, mode=0o3770, runtime=runtime, create_only=True)
    observed = (parent / "created").stat()
    assert created.observation is not None
    assert created.observation.state is FileMetadataObservationState.CHANGED
    assert (observed.st_uid, observed.st_gid) == (os.geteuid(), os.getegid())
    assert stat.S_IMODE(observed.st_mode) == 0o3770
    (parent / "file").write_bytes(b"content")
    (parent / "link").symlink_to(parent / "created")
    for name in ["parent/created", "parent/file", "parent/link", "missing/child"]:
        before = (tmp_path / name).lstat() if name != "missing/child" else None
        carrier, refused = _ensure(tmp_path, name, plan, source, mode=0o755, runtime=runtime, create_only=True)
        assert carrier.calls == 1
        assert refused.observation is not None
        assert refused.observation.state is FileMetadataObservationState.REFUSED
        if before is not None:
            assert (tmp_path / name).lstat() == before
    assert not (tmp_path / "missing").exists()
    assert (parent / "created").stat() == observed


def test_create_only_helper_reports_partial_deadline(tmp_path: Path, plan: IdentityPlan) -> None:
    source = _fixture_source(
        "original=m._create_directory\n"
        "def expired(*args):\n"
        " result=original(*args)\n"
        " m.time.monotonic=lambda:float('inf')\n"
        " return result\n"
        "m._create_directory=expired\n"
    )
    carrier, result = _ensure(tmp_path, "created", plan, source, mode=0o755, create_only=True)
    assert carrier.calls == 1
    assert result.observation is not None
    assert result.observation.state is FileMetadataObservationState.PARTIAL
    assert stat.S_IMODE((tmp_path / "created").stat().st_mode) == 0o700


def test_create_only_helper_refuses_racing_creator(tmp_path: Path, plan: IdentityPlan) -> None:
    source = _fixture_source(
        "original=os.mkdir\n"
        "def race(path,mode,*,dir_fd):\n"
        " original(path,0o700,dir_fd=dir_fd)\n"
        " original(path,mode,dir_fd=dir_fd)\n"
        "os.mkdir=race\n"
    )
    carrier, result = _ensure(tmp_path, "created", plan, source, mode=0o755, create_only=True)
    assert carrier.calls == 1
    assert result.observation is not None
    assert result.observation.state is FileMetadataObservationState.REFUSED
    assert stat.S_IMODE((tmp_path / "created").stat().st_mode) == 0o700


@pytest.mark.parametrize("step", ["ownership", "mode"])
def test_create_only_helper_preserves_uncertain_mutation_facts(tmp_path: Path, plan: IdentityPlan, step: str) -> None:
    # Make ownership necessary without requiring privilege for a real chown.
    injection = f"def fail(*args):raise PermissionError()\nos.{'chown' if step == 'ownership' else 'chmod'}=fail\n"
    if step == "ownership":
        injection += (
            "original=g.create_directory\n"
            "def create(*args,**kwargs):\n"
            " kwargs['uid']+=1\n"
            " return original(*args,**kwargs)\n"
            "g.create_directory=create\n"
        )
    carrier, result = _ensure(tmp_path, "created", plan, _fixture_source(injection), mode=0o755, create_only=True)
    assert carrier.calls == 1
    assert result.observation is not None
    assert result.observation.state is FileMetadataObservationState.UNCERTAIN
    failure = result.observation.failure
    assert failure is not None
    assert failure.completed_steps == (MetadataStep.CREATION,)
    assert failure.attempted_step is MetadataStep(step)
    assert stat.S_IMODE((tmp_path / "created").stat().st_mode) == 0o700


def test_create_only_parent_link_is_confined(tmp_path: Path, plan: IdentityPlan) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    carrier, result = _ensure(root, "link/created", plan, _fixture_source(), mode=0o755, create_only=True)
    assert carrier.calls == 1
    assert result.observation is not None
    assert result.observation.state is FileMetadataObservationState.REFUSED
    assert result.observation.failure is not None
    assert result.observation.failure.code is FileMetadataFailureCode.PARENT_REFUSED
    assert list(outside.iterdir()) == []


def test_missing_runtime_yields_no_metadata_observation(tmp_path: Path, plan: IdentityPlan) -> None:
    carrier, result = _set(
        tmp_path,
        "missing",
        plan,
        _fixture_source(),
        mode=0o600,
        runtime=Path("/missing/agentworks-python"),
    )

    assert carrier.calls == 1
    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.MISSING
    assert result.observation is None


@pytest.mark.parametrize(
    "runtime",
    [Path(sys.executable), Path("/usr/bin/python3.11")],
    ids=["current", "python311"],
)
def test_fixed_bundle_creates_converges_and_is_idempotent(
    tmp_path: Path,
    plan: IdentityPlan,
    runtime: Path,
) -> None:
    if not runtime.is_file():
        pytest.skip(f"compatibility interpreter is unavailable: {runtime}")
    root = tmp_path / "root"
    root.mkdir()
    existing = root / "existing"
    existing.write_bytes(b"unrelated-content")
    existing.chmod(0o600)
    source = _fixture_source()

    carrier, changed_file = _set(root, "existing", plan, source, mode=0o640, runtime=runtime)
    _, created = _ensure(root, "shared", plan, source, mode=0o3770, runtime=runtime)
    _, unchanged = _ensure(root, "shared", plan, source, mode=0o3770, runtime=runtime)

    assert carrier.calls == 1
    assert carrier.io is not None and carrier.io.sensitive
    assert carrier.invocation is not None and str(root) not in " ".join(carrier.invocation.argv)
    assert changed_file.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    changed_file_observation = changed_file.observation
    assert changed_file_observation is not None
    assert changed_file_observation.state is FileMetadataObservationState.CHANGED
    changed_file_revision = changed_file_observation.revision
    assert changed_file_revision is not None
    assert existing.read_bytes() == b"unrelated-content"
    assert stat.S_IMODE(existing.stat().st_mode) == 0o640
    created_observation = created.observation
    assert created_observation is not None
    assert created_observation.state is FileMetadataObservationState.CHANGED
    assert stat.S_IMODE((root / "shared").stat().st_mode) == 0o3770
    unchanged_observation = unchanged.observation
    assert unchanged_observation is not None
    assert unchanged_observation.state is FileMetadataObservationState.UNCHANGED


def test_ensure_creates_only_final_component_and_preserves_children(tmp_path: Path, plan: IdentityPlan) -> None:
    root = tmp_path / "root"
    parent = root / "parent"
    parent.mkdir(parents=True)
    child = parent / "child"
    child.write_bytes(b"child-content")
    child.chmod(0o600)
    source = _fixture_source()

    _, missing_parent = _ensure(root, "missing/final", plan, source, mode=0o755)
    _, existing_parent = _ensure(root, "parent", plan, source, mode=0o2770)

    missing_parent_observation = missing_parent.observation
    assert missing_parent_observation is not None
    assert missing_parent_observation.state is FileMetadataObservationState.REFUSED
    missing_parent_failure = missing_parent_observation.failure
    assert missing_parent_failure is not None
    assert missing_parent_failure.code is FileMetadataFailureCode.PARENT_REFUSED
    assert not (root / "missing").exists()
    existing_parent_observation = existing_parent.observation
    assert existing_parent_observation is not None
    assert existing_parent_observation.state is FileMetadataObservationState.CHANGED
    assert child.read_bytes() == b"child-content"
    assert stat.S_IMODE(child.stat().st_mode) == 0o600


def test_missing_root_conflicting_file_socket_and_unsupported_modes_refuse(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    regular = root / "regular"
    regular.write_bytes(b"content")
    listener = socket.socket(socket.AF_UNIX)
    monkeypatch.chdir(root)
    listener.bind("socket")
    source = _fixture_source()
    try:
        _, missing_root = _set(tmp_path / "missing", "leaf", plan, source, mode=0o600)
        _, conflict = _ensure(root, "regular", plan, source, mode=0o755)
        _, socket_result = _set(root, "socket", plan, source, mode=0o600)
        _, setuid_file = _set(root, "regular", plan, source, mode=0o4600)
    finally:
        listener.close()

    missing_root_observation = missing_root.observation
    assert missing_root_observation is not None
    assert missing_root_observation.state is FileMetadataObservationState.REFUSED
    missing_root_failure = missing_root_observation.failure
    assert missing_root_failure is not None
    assert missing_root_failure.code is FileMetadataFailureCode.ROOT_REFUSED
    for result in (conflict, socket_result, setuid_file):
        result_observation = result.observation
        assert result_observation is not None
        assert result_observation.state is FileMetadataObservationState.REFUSED
        result_failure = result_observation.failure
        assert result_failure is not None
        assert result_failure.code is FileMetadataFailureCode.METADATA
    assert regular.read_bytes() == b"content"


@pytest.mark.parametrize("create_only", [False, True])
def test_identity_mismatch_precedes_target_access(tmp_path: Path, plan: IdentityPlan, create_only: bool) -> None:
    carrier = LocalCarrier()
    mismatched = IdentityPlan(
        IdentityExpectation((plan.expected.euid + 1) % (2**32), plan.expected.egid, plan.expected.groups),
        IdentityMode.DIRECT,
    )
    with patch("agentworks.execution._file_metadata_exchange.FIXED_BUNDLE", _fixture_source()):
        function = create_file_directory if create_only else set_file_metadata
        result = function(
            carrier,
            trusted_root_path=str(tmp_path / "absent-target"),
            relative_path="leaf",
            uid=os.geteuid(),
            gid=os.getegid(),
            mode=0o600,
            plan=mismatched,
            deadline=Deadline.after(15),
            runtime_selection=runtime_selection(sys.executable),
        )

    assert carrier.calls == 1
    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileMetadataObservationState.REFUSED
    result_failure = result_observation.failure
    assert result_failure is not None
    assert result_failure.code is FileMetadataFailureCode.IDENTITY_MISMATCH
    assert not (tmp_path / "absent-target").exists()


def test_verified_partial_creation_and_uncertain_attempt_are_not_replayed(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    partial_injection = (
        "def partial(parent_fd,leaf_name,**kwargs):\n"
        " os.mkdir(leaf_name,0o700,dir_fd=parent_fd)\n"
        " raise m.MetadataError(m.MetadataFailureKind.IO,m.MetadataPhase.VERIFICATION,"
        "completed_steps=(m.MetadataStep.CREATION,))\n"
        "g.ensure_directory=partial\n"
    )
    partial_carrier, partial = _ensure(
        root,
        "partial",
        plan,
        _fixture_source(partial_injection),
        mode=0o755,
    )
    target = root / "target"
    target.write_bytes(b"content")
    target.chmod(0o600)
    uncertain_injection = (
        "base_set=g.set_metadata\n"
        "def uncertain(*args,**kwargs):\n"
        " base_set(*args,**kwargs)\n"
        " raise m.MetadataError(m.MetadataFailureKind.METADATA,m.MetadataPhase.MODE,"
        "attempted_step=m.MetadataStep.MODE)\n"
        "g.set_metadata=uncertain\n"
    )
    uncertain_carrier, uncertain = _set(
        root,
        "target",
        plan,
        _fixture_source(uncertain_injection),
        mode=0o640,
    )

    assert partial_carrier.calls == 1
    partial_observation = partial.observation
    assert partial_observation is not None
    assert partial_observation.state is FileMetadataObservationState.PARTIAL
    assert (root / "partial").is_dir()
    assert stat.S_IMODE((root / "partial").stat().st_mode) == 0o700
    assert uncertain_carrier.calls == 1
    uncertain_observation = uncertain.observation
    assert uncertain_observation is not None
    assert uncertain_observation.state is FileMetadataObservationState.UNCERTAIN
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_proc_bridge_changes_held_inode_not_replacement_name(tmp_path: Path, plan: IdentityPlan) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "target"
    target.write_bytes(b"old")
    target.chmod(0o600)
    old_inode = target.stat().st_ino
    replacement = root / "replacement"
    replacement.write_bytes(b"new")
    replacement.chmod(0o644)
    retained = root / "retained"
    injection = (
        "base_chmod=m.os.chmod\n"
        "def replacing_chmod(path,mode):\n"
        f" os.rename({str(target)!r},{str(retained)!r})\n"
        f" os.rename({str(replacement)!r},{str(target)!r})\n"
        " return base_chmod(path,mode)\n"
        "m.os.chmod=replacing_chmod\n"
    )

    _, result = _set(root, "target", plan, _fixture_source(injection), mode=0o640)

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileMetadataObservationState.PARTIAL
    assert retained.stat().st_ino == old_inode
    assert stat.S_IMODE(retained.stat().st_mode) == 0o640
    assert target.read_bytes() == b"new"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_metadata_mutation_succeeds_without_protected_lock_namespace(
    tmp_path: Path,
    plan: IdentityPlan,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "target"
    target.write_bytes(b"content")
    target.chmod(0o600)
    _, result = _set(root, "target", plan, _fixture_source(), mode=0o640)

    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileMetadataObservationState.CHANGED
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_complete_proxmox_post_fits_provider_bound_and_returns_typed_refusal(
    tmp_path: Path,
    plan: IdentityPlan,
    monkeypatch: pytest.MonkeyPatch,
    bind_carrier,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    carrier = ProxmoxCarrier(ProxmoxConnection("https://pve.invalid", "node1", 101, "token", "synthetic"))
    delivery = bind_carrier(carrier)
    body_sizes: list[int] = []
    status: dict[str, object] = {}

    def request(
        method: str, suffix: str, *, body: bytes | None = None, timeout: float | None, custody: LocalDeliveryCustody
    ) -> dict[str, object]:
        if method == "POST":
            assert body is not None and body.isascii()
            body_sizes.append(len(body))
            envelope = json.loads(body)
            completed = subprocess.run(
                envelope["command"],
                input=envelope["input-data"].encode("ascii"),
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            status.update(
                exited=True,
                exitcode=completed.returncode,
                **{"out-data": completed.stdout.decode("ascii"), "err-data": ""},
            )
            return {"pid": 42}
        assert suffix == "exec-status?pid=42" and body is None
        return status

    monkeypatch.setattr(carrier._wire, "request", request)
    mismatched = IdentityPlan(
        IdentityExpectation((plan.expected.euid + 1) % (2**32), plan.expected.egid, plan.expected.groups),
        IdentityMode.DIRECT,
    )
    result = set_file_metadata(
        delivery,
        trusted_root_path=str(root),
        relative_path="leaf",
        uid=os.geteuid(),
        gid=os.getegid(),
        mode=0o600,
        plan=mismatched,
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(sys.executable),
    )

    assert len(FIXED_BUNDLE.prefix) < body_sizes[0] < 65_536
    assert result.dispatch is Dispatch.SENT
    result_observation = result.observation
    assert result_observation is not None
    assert result_observation.state is FileMetadataObservationState.REFUSED
    result_failure = result_observation.failure
    assert result_failure is not None
    assert result_failure.code is FileMetadataFailureCode.IDENTITY_MISMATCH
