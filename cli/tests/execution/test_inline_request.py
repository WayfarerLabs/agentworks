"""Boundary checks for private inline request preparation and guest decoding."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, ItemsView, Iterator, Mapping

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._evidence_wire import Frame, FrameKind, FrameReader
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._inline import PreparedInlineCandidate, prepare_inline_candidate
from agentworks.execution._inline_control import ControlError, FailureCode, FailurePhase, parse_failure, parse_wait
from agentworks.execution._inline_request import (
    MAX_MANIFEST_BYTES,
    ManifestError,
    ManifestErrorCode,
    decode_manifest,
)
from agentworks.execution.carrier import FiniteInput
from agentworks.execution.models import Command, Script, Shell

NONCE = "0123456789abcdef0123456789abcdef"


def _exception_graph(error: BaseException) -> list[BaseException]:
    pending = [error]
    seen: set[int] = set()
    result = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        if current.__context__ is not None:
            pending.append(current.__context__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return result


@pytest.fixture
def plan() -> IdentityPlan:
    return IdentityPlan(IdentityExpectation(1001, 1002, (1002, 1003)), IdentityMode.DIRECT)


@pytest.mark.windows
def test_canonical_manifest_is_closed_ascii_and_keeps_payload_out_of_helper_argv(
    plan: IdentityPlan,
) -> None:
    canary = "inline-manifest-canary-8b30480f"
    prepared = prepare_inline_candidate(
        Command(["/bin/tool", canary]),
        plan=plan,
        stdin=canary.encode(),
        env={"VALUE": canary},
        cwd=f"/{canary}",
    )

    assert isinstance(prepared.io.input, FiniteInput)
    encoded = prepared.io.input.data
    value = json.loads(encoded)
    assert encoded == json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
    assert set(value) == {
        "argv",
        "cwd",
        "env",
        "identity",
        "kind",
        "nonce",
        "output",
        "shell",
        "source",
        "stdin",
        "version",
    }
    assert canary.encode() not in encoded
    assert canary not in prepared.invocation.argv
    assert canary not in repr(prepared)
    assert prepared.invocation.argv[:5] == (
        "/usr/bin/env",
        "-i",
        "PATH=/usr/bin:/bin",
        "LANG=C",
        "LC_ALL=C",
    )


@pytest.mark.windows
def test_guest_module_import_does_not_require_posix_account_database() -> None:
    script = r"""
import sys

sys.modules["pwd"] = None
import agentworks.execution._inline_guest
assert sys.modules["pwd"] is None
"""
    result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, timeout=20)

    assert result.returncode == 0, result.stderr.decode(errors="replace")


@pytest.mark.parametrize(
    "data",
    [
        b'{"version":1,"version":1}',
        b'{"extra":true}',
        b" {}",
        b"[]",
        b"\xff",
    ],
)
def test_guest_decoder_rejects_duplicate_extra_noncanonical_and_wrong_type_json(data: bytes) -> None:
    with pytest.raises(ManifestError) as caught:
        decode_manifest(data)

    assert caught.value.code is ManifestErrorCode.INVALID
    visible_prefix = data[:8].decode("ascii", errors="ignore")
    assert not visible_prefix or visible_prefix not in repr(caught.value)


def test_guest_decoder_distinguishes_only_the_safe_oversize_category() -> None:
    with pytest.raises(ManifestError) as caught:
        decode_manifest(b"x" * (MAX_MANIFEST_BYTES + 1))

    assert caught.value.code is ManifestErrorCode.OVERSIZED


def test_boundary_errors_drop_payload_bearing_exception_context(plan: IdentityPlan) -> None:
    canary = "exception-graph-canary-b3627e"

    class BrokenMapping(Mapping[str, str]):
        def __getitem__(self, key: str) -> str:
            raise KeyError(key)

        def __iter__(self) -> Iterator[str]:
            return iter(())

        def __len__(self) -> int:
            return 0

        def items(self) -> ItemsView[str, str]:
            raise RuntimeError(canary)

    failures: list[BaseException] = []
    for action, error_type in (
        (lambda: decode_manifest(f'{{"{canary}":'.encode()), ManifestError),
        (lambda: parse_wait(f'{{"kind":"{canary}","value":0}}'.encode()), ControlError),
        (
            lambda: prepare_inline_candidate(Command(["/bin/echo", f"{canary}\ud800"]), plan=plan),
            ValidationError,
        ),
        (
            lambda: prepare_inline_candidate(Command(["/bin/true"]), plan=plan, env={f"{canary}-": "x"}),
            ValidationError,
        ),
        (
            lambda: prepare_inline_candidate(Command(["/bin/true"]), plan=plan, env=BrokenMapping()),
            ValidationError,
        ),
        (
            lambda: prepare_inline_candidate(Command(["/bin/true"]), plan=plan, runtime_path=f"/tmp/{canary}=python"),
            ValidationError,
        ),
    ):
        with pytest.raises(error_type) as caught:
            action()
        failures.append(caught.value)

    for failure in failures:
        graph = _exception_graph(failure)
        assert len(graph) == 1
        assert all(canary not in repr(node.args) for node in graph)


@pytest.mark.parametrize(
    "build",
    [
        lambda plan: prepare_inline_candidate(Command(["/bin/echo", "bad\0arg"]), plan=plan),
        lambda plan: prepare_inline_candidate(Command(["/bin/echo", "\ud800"]), plan=plan),
        lambda plan: prepare_inline_candidate(Script("bad\0source", Shell.SH), plan=plan),
        lambda plan: prepare_inline_candidate(Script("\ud800", Shell.SH), plan=plan),
        lambda plan: prepare_inline_candidate(Command(["/bin/true"]), plan=plan, env={"BAD-NAME": "x"}),
        lambda plan: prepare_inline_candidate(Command(["/bin/true"]), plan=plan, env={"ENV": "x"}),
        lambda plan: prepare_inline_candidate(Command(["/bin/true"]), plan=plan, env={"_agw_x": "x"}),
        lambda plan: prepare_inline_candidate(Command(["/bin/true"]), plan=plan, env={"VALUE": "bad\0"}),
        lambda plan: prepare_inline_candidate(Command(["/bin/true"]), plan=plan, cwd="relative"),
        lambda plan: prepare_inline_candidate(Command(["/bin/true"]), plan=plan, cwd="/bad\0cwd"),
        lambda plan: prepare_inline_candidate(Command(["/bin/true"]), plan=plan, capture_limit=4_097),
        lambda plan: prepare_inline_candidate(Command(["/bin/true"]), plan=plan, capture_limit=True),
        lambda plan: prepare_inline_candidate(Command(["/bin/true"]), plan=plan, runtime_path="python3"),
        lambda plan: prepare_inline_candidate(
            Command(["/bin/true"]), plan=plan, runtime_path="/tmp/runtime=payload-canary"
        ),
        lambda plan: prepare_inline_candidate(
            Command(["/bin/true"]),
            plan=IdentityPlan(
                IdentityExpectation(plan.expected.euid, plan.expected.egid, (plan.expected.egid, plan.expected.egid)),
                IdentityMode.DIRECT,
            ),
        ),
    ],
)
def test_host_rejects_invalid_text_options_and_identity_before_dispatch(
    build: Callable[[IdentityPlan], PreparedInlineCandidate],
    plan: IdentityPlan,
) -> None:
    with pytest.raises(ValidationError):
        build(plan)


@pytest.mark.parametrize(
    "invocation",
    [
        Script("exit 0", Shell.SH, login=True),
        Script("exit 0", Shell.BASH, interactive=True),
        Script("exit 0", Shell.USER_DEFAULT, login=True, interactive=True),
    ],
)
def test_host_refuses_unsupported_startup_modes(invocation: Script, plan: IdentityPlan) -> None:
    with pytest.raises(ValidationError):
        prepare_inline_candidate(invocation, plan=plan)


def test_host_refuses_oversized_manifest_without_dispatch(plan: IdentityPlan) -> None:
    with pytest.raises(ValidationError) as caught:
        prepare_inline_candidate(Command(["/bin/true"]), plan=plan, stdin=b"x" * MAX_MANIFEST_BYTES)

    assert "x" * 64 not in str(caught.value)


@pytest.mark.skipif(sys.platform != "linux", reason="the inline guest candidate requires Linux")
@pytest.mark.parametrize(
    ("manifest", "code"),
    [
        (b'{"version":1,"version":1}', FailureCode.INVALID),
        (b"x" * (MAX_MANIFEST_BYTES + 1), FailureCode.OVERSIZED),
    ],
)
def test_actual_guest_revalidates_untrusted_manifest(manifest: bytes, code: FailureCode, plan: IdentityPlan) -> None:
    prepared = prepare_inline_candidate(Command(["/bin/true"]), plan=plan)
    invocation = (*prepared.invocation.argv[:-1], NONCE)

    completed = subprocess.run(invocation, input=manifest, capture_output=True, timeout=10, check=True)
    frames: list[Frame] = []
    reader = FrameReader(NONCE, frames.append)
    reader.try_write(memoryview(completed.stdout))
    reader.finish()

    assert completed.stderr == b""
    assert reader.error is None
    assert [frame.kind for frame in frames] == [FrameKind.FAILED, FrameKind.FINISHED]
    failure = parse_failure(frames[0].body)
    assert failure.phase is FailurePhase.REQUEST
    assert failure.code is code
