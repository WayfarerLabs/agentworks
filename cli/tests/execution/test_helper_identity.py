"""Identity-plan and launcher checks shared by private buffered helpers."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import _helper_identity, _inline_guest
from agentworks.execution._evidence_wire import Frame, FrameKind, FrameReader
from agentworks.execution._file_read import FileReadObservationState, execute_file_read, prepare_file_read
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan, build_helper_argv
from agentworks.execution._inline import execute_inline_candidate, prepare_inline_candidate
from agentworks.execution._inline_control import FailureCode, FailurePhase, parse_failure
from agentworks.execution._inline_observer import ObservationError
from agentworks.execution.carrier import (
    CapturedOutput,
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    PreparedInvocation,
    Retention,
)
from agentworks.execution.models import Command, Script, Shell


def _identity(uid: int = 1001, gid: int = 1002, groups: tuple[int, ...] = (1002, 1003)) -> IdentityExpectation:
    return IdentityExpectation(uid, gid, groups)


def _plan(mode: IdentityMode, *, uid: int = 1001) -> IdentityPlan:
    gid = 0 if uid == 0 else 1002
    groups = (0,) if uid == 0 else (1002, 1003)
    return IdentityPlan(_identity(uid, gid, groups), mode)


def test_direct_launcher_is_the_fixed_minimal_helper_environment() -> None:
    argv = build_helper_argv(
        _plan(IdentityMode.DIRECT),
        runtime_path="/usr/bin/python3.11",
        fixed_source="fixed-helper-source",
        nonce="0" * 32,
    )

    assert argv == (
        "/usr/bin/env",
        "-i",
        "PATH=/usr/bin:/bin",
        "LANG=C",
        "LC_ALL=C",
        "/usr/bin/python3.11",
        "-I",
        "-S",
        "-B",
        "-c",
        "fixed-helper-source",
        "0" * 32,
    )


@pytest.mark.parametrize(
    ("mode", "uid", "prefix"),
    [
        (IdentityMode.SUDO_ROOT, 0, ("/usr/bin/sudo", "-n", "--user=#0", "--")),
        (
            IdentityMode.DEMOTE,
            1001,
            (
                "/usr/bin/setpriv",
                "--reuid=1001",
                "--regid=1002",
                "--groups=1002,1003",
                "--inh-caps=-all",
                "--ambient-caps=-all",
                "--",
            ),
        ),
    ],
)
def test_wrapper_argv_is_literal_and_payload_free(
    mode: IdentityMode,
    uid: int,
    prefix: tuple[str, ...],
) -> None:
    plan = _plan(mode, uid=uid)
    canary = "wrapper-payload-canary-423cab"
    inline = prepare_inline_candidate(
        Script(canary, Shell.SH),
        plan=plan,
        stdin=canary.encode(),
        env={"CANARY": canary},
        cwd=f"/{canary}",
    )
    file_read = prepare_file_read(
        trusted_root_path=f"/{canary}",
        relative_path=canary,
        max_bytes=1,
        plan=plan,
    )

    for prepared in (inline, file_read):
        assert prepared.invocation.argv[: len(prefix)] == prefix
        assert prepared.invocation.argv[len(prefix) : len(prefix) + 5] == (
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "LANG=C",
            "LC_ALL=C",
        )
        assert all(canary not in argument for argument in prepared.invocation.argv)


@pytest.mark.parametrize(
    "plan",
    [
        _plan(IdentityMode.SUDO_ROOT),
        _plan(IdentityMode.DEMOTE, uid=0),
        IdentityPlan(_identity(groups=(1003, 1002)), IdentityMode.DIRECT),
        IdentityPlan(_identity(groups=(1002, 1002)), IdentityMode.DIRECT),
        IdentityPlan(_identity(groups=(1003,)), IdentityMode.DIRECT),
    ],
)
@pytest.mark.parametrize("kind", ["inline", "file"])
def test_invalid_identity_plan_is_rejected_before_payload_encoding(
    monkeypatch: pytest.MonkeyPatch,
    plan: IdentityPlan,
    kind: str,
) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("payload encoder reached")

    if kind == "inline":
        monkeypatch.setattr("agentworks.execution._inline.encode_manifest", unexpected)
    else:
        monkeypatch.setattr("agentworks.execution._file_read.encode_file_read_request", unexpected)

    with pytest.raises(ValidationError):
        if kind == "inline":
            prepare_inline_candidate(Command(["/bin/true"]), plan=plan)
        else:
            prepare_file_read(
                trusted_root_path="/trusted",
                relative_path="file",
                max_bytes=1,
                plan=plan,
            )


@pytest.mark.parametrize(
    ("resuid", "resgid", "groups", "matches"),
    [
        ((1001, 1001, 1001), (1002, 1002, 1002), [1003], True),
        ((0, 1001, 1001), (1002, 1002, 1002), [1003], False),
        ((1001, 1001, 0), (1002, 1002, 1002), [1003], False),
        ((1001, 1001, 1001), (0, 1002, 1002), [1003], False),
        ((1001, 1001, 1001), (1002, 1002, 0), [1003], False),
        ((1001, 1001, 1001), (1002, 1002, 1002), [1004], False),
    ],
)
def test_guest_identity_requires_real_effective_saved_and_exact_groups(
    monkeypatch: pytest.MonkeyPatch,
    resuid: tuple[int, int, int],
    resgid: tuple[int, int, int],
    groups: list[int],
    matches: bool,
) -> None:
    monkeypatch.setattr(os, "getresuid", lambda: resuid, raising=False)
    monkeypatch.setattr(os, "getresgid", lambda: resgid, raising=False)
    monkeypatch.setattr(os, "getegid", lambda: resgid[1], raising=False)
    monkeypatch.setattr(os, "getgroups", lambda: groups, raising=False)

    assert _helper_identity.matches_current_identity(_identity()) is matches


def test_inline_guest_refuses_unsupported_runtime_before_identity_or_workload_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepare_inline_candidate(Command(["/bin/true"]), plan=_plan(IdentityMode.DIRECT))
    manifest = prepared.io.input.data  # type: ignore[union-attr]
    reads = [manifest, b""]
    output = bytearray()

    def read(_descriptor: int, _maximum: int) -> bytes:
        return reads.pop(0)

    def write(_descriptor: int, data: bytes) -> int:
        output.extend(data)
        return len(data)

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("identity or workload access reached")

    monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(os, "write", write)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(_inline_guest, "matches_current_identity", unexpected)
    monkeypatch.setattr(_inline_guest, "run_owned_process", unexpected)

    assert _inline_guest.main(prepared.nonce) == 0
    frames: list[Frame] = []
    reader = FrameReader(prepared.nonce, frames.append)
    reader.try_write(memoryview(output))
    reader.finish()

    assert reader.error is None
    assert [frame.kind for frame in frames] == [FrameKind.FAILED, FrameKind.FINISHED]
    failure = parse_failure(frames[0].body)
    assert failure.phase is FailurePhase.PREPARE
    assert failure.code is FailureCode.RUNTIME


@dataclass
class _PreHelperFailureCarrier:
    calls: int = 0

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        del invocation, io, deadline
        self.calls += 1
        empty = CapturedOutput(complete=True, retention=Retention.DELIVERED)
        return CarrierReport(Dispatch.SENT, ExitStatus(code=127), 127, empty, empty)


@pytest.mark.parametrize("mode", [IdentityMode.SUDO_ROOT, IdentityMode.DEMOTE])
def test_pre_helper_wrapper_failure_never_claims_application_or_file_success(mode: IdentityMode) -> None:
    plan = _plan(mode, uid=0 if mode is IdentityMode.SUDO_ROOT else 1001)
    carrier = _PreHelperFailureCarrier()
    inline = prepare_inline_candidate(Command(["/bin/true"]), plan=plan)
    inline_result = execute_inline_candidate(carrier, inline, deadline=Deadline.after(1))
    file_read = prepare_file_read(
        trusted_root_path="/trusted",
        relative_path="file",
        max_bytes=1,
        plan=plan,
    )
    file_result = execute_file_read(carrier, file_read, deadline=Deadline.after(1))

    assert carrier.calls == 2
    assert not inline_result.observation.launching
    assert not inline_result.observation.trusted_terminal
    assert inline_result.observation.wait is None
    assert inline_result.observation.error is ObservationError.MISSING_TERMINAL
    assert file_result.observation.state is FileReadObservationState.INCOMPLETE
    assert file_result.observation.snapshot is None
