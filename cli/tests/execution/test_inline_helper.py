"""Linux end-to-end checks for the fixed private inline helper candidate."""

from __future__ import annotations

import os
import shlex
import signal
import sys
from pathlib import Path

import pytest

from agentworks.execution._inline import (
    InlineCandidateResult,
    PreparedInlineCandidate,
    execute_inline_candidate,
    prepare_inline_candidate,
)
from agentworks.execution._inline_control import FailureCode, FailurePhase, StreamRetention, WaitFact, WaitKind
from agentworks.execution._inline_request import IdentityExpectation
from agentworks.execution.carrier import (
    CarrierIO,
    CarrierReport,
    ChannelFeatures,
    Deadline,
    Dispatch,
    ExitStatus,
    PreparedInvocation,
    Retention,
)
from agentworks.execution.carriers._subprocess import run_process
from agentworks.execution.models import Command, Script, Shell

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the inline guest candidate requires Linux")
PYTHON_311 = Path("/usr/bin/python3.11")


class LocalCarrier:
    def __init__(self) -> None:
        self.calls = 0
        self.last_report: CarrierReport | None = None

    @property
    def features(self) -> ChannelFeatures:
        return ChannelFeatures()

    def execute(self, invocation: PreparedInvocation, *, io: CarrierIO, deadline: Deadline) -> CarrierReport:
        self.calls += 1
        result = run_process(list(invocation.argv), io=io, deadline=deadline)
        completion = None
        if result.exit_status is not None:
            completion = (
                ExitStatus(signal=-result.exit_status)
                if result.exit_status < 0
                else ExitStatus(code=result.exit_status)
            )
        report = CarrierReport(
            Dispatch.SENT if result.started else Dispatch.NOT_SENT,
            completion,
            result.local_status,
            result.stdout,
            result.stderr,
            result.failure,
        )
        self.last_report = report
        return report


@pytest.fixture(scope="module", params=[Path(sys.executable), PYTHON_311], ids=["current", "distribution-3.11"])
def helper_runtime(request: pytest.FixtureRequest) -> str:
    runtime: Path = request.param
    if not runtime.is_file():
        pytest.skip(f"This host has no compatibility interpreter at {runtime}")
    return str(runtime)


@pytest.fixture
def identity() -> IdentityExpectation:
    return IdentityExpectation(
        os.geteuid(),
        os.getegid(),
        tuple(sorted(set(os.getgroups()) | {os.getegid()})),
    )


def _run(
    request: Command | Script,
    identity: IdentityExpectation,
    *,
    helper_runtime: str = "/usr/bin/python3",
    carrier: LocalCarrier | None = None,
    **options: object,
) -> tuple[PreparedInlineCandidate, InlineCandidateResult, LocalCarrier]:
    selected_carrier = carrier or LocalCarrier()
    prepared = prepare_inline_candidate(request, identity=identity, runtime_path=helper_runtime, **options)
    result = execute_inline_candidate(selected_carrier, prepared, deadline=Deadline.after(15))
    assert selected_carrier.calls == 1
    return prepared, result, selected_carrier


@pytest.mark.parametrize("status", range(256))
def test_literal_command_preserves_every_normal_exit(status: int, identity: IdentityExpectation) -> None:
    _, result, _ = _run(
        Command(["/usr/bin/python3", "-I", "-S", "-B", "-c", f"raise SystemExit({status})"]),
        identity,
    )

    assert result.observation.trusted_terminal
    assert result.observation.wait is not None
    assert result.observation.wait.kind is WaitKind.EXIT
    assert result.observation.wait.value == status
    assert result.observation.failure is None
    assert result.carrier_completion == ExitStatus(code=0)


def test_signaled_command_keeps_exact_signal_separate(identity: IdentityExpectation) -> None:
    _, result, _ = _run(
        Command(["/usr/bin/python3", "-c", "import os,signal; os.kill(os.getpid(), signal.SIGTERM)"]),
        identity,
    )

    assert result.observation.wait is not None
    assert result.observation.wait.kind is WaitKind.SIGNAL
    assert result.observation.wait.value == signal.SIGTERM
    assert result.observation.trusted_terminal


def test_literal_arguments_are_not_reparsed(identity: IdentityExpectation) -> None:
    arguments = ["space value", "'\"$()`;", "line1\nline2", "snowman-\u2603"]
    code = f"import sys;assert sys.argv[1:]=={arguments!r}"

    _, result, _ = _run(Command(["/usr/bin/python3", "-I", "-S", "-B", "-c", code, *arguments]), identity)

    assert result.observation.wait == WaitFact(WaitKind.EXIT, 0)
    assert result.observation.trusted_terminal


@pytest.mark.parametrize("shell", [Shell.SH, Shell.BASH])
def test_memfd_script_separates_source_binary_input_and_streams(
    shell: Shell,
    identity: IdentityExpectation,
    helper_runtime: str,
) -> None:
    stdin = bytes(range(256)) * 40
    stdout = b"\x00stdout\xff\r\n"
    stderr = b"\x80stderr\x00\n"
    child = (
        "import hashlib,sys;"
        "data=sys.stdin.buffer.read();"
        f"assert hashlib.sha256(data).hexdigest()=={__import__('hashlib').sha256(stdin).hexdigest()!r};"
        f"sys.stdout.buffer.write({stdout!r});sys.stderr.buffer.write({stderr!r})"
    )
    source = f"/usr/bin/python3 -I -S -B -c {shlex.quote(child)}\nexit 255\n"

    prepared, result, _ = _run(
        Script(source, shell),
        identity,
        helper_runtime=helper_runtime,
        stdin=stdin,
    )

    assert result.observation.wait is not None and result.observation.wait.value == 255
    assert result.observation.stdout is not None and result.observation.stdout.data == stdout
    assert result.observation.stderr is not None and result.observation.stderr.data == stderr
    assert result.observation.trusted_terminal
    assert source not in prepared.invocation.argv


def test_payload_environment_and_absolute_cwd_replace_helper_environment(
    identity: IdentityExpectation,
    tmp_path: Path,
) -> None:
    _, env_result, _ = _run(
        Command(["/usr/bin/env", "-0"]),
        identity,
        env={"ONLY_PAYLOAD": "present"},
        cwd=str(tmp_path),
    )
    _, cwd_result, _ = _run(Command(["/bin/pwd"]), identity, cwd=str(tmp_path))

    assert env_result.observation.stdout is not None
    assert env_result.observation.stdout.data == b"ONLY_PAYLOAD=present\0"
    assert cwd_result.observation.stdout is not None
    assert cwd_result.observation.stdout.data == f"{tmp_path}\n".encode()


def test_identity_mismatch_refuses_before_launch(identity: IdentityExpectation) -> None:
    mismatched = IdentityExpectation(identity.euid + 100_000, identity.egid, identity.groups)
    _, result, _ = _run(Command(["/bin/true"]), mismatched)

    observation = result.observation
    assert observation.trusted_terminal
    assert not observation.launching
    assert observation.wait is None
    assert observation.stdout is None and observation.stderr is None
    assert observation.failure is not None
    assert observation.failure.phase is FailurePhase.IDENTITY
    assert observation.failure.code is FailureCode.MISMATCH


def test_unsupported_user_default_shell_refuses_without_fallback(identity: IdentityExpectation) -> None:
    import pwd

    configured = pwd.getpwuid(identity.euid).pw_shell
    if configured in {"/bin/sh", "/usr/bin/sh", "/bin/bash", "/usr/bin/bash"}:
        pytest.skip("The local account has a supported default shell")

    _, result, _ = _run(Script("exit 0\n", Shell.USER_DEFAULT), identity)

    assert result.observation.failure is not None
    assert result.observation.failure.phase is FailurePhase.IDENTITY
    assert result.observation.failure.code is FailureCode.SHELL
    assert not result.observation.launching


def test_capture_overflow_retains_limit_and_reports_truncation(identity: IdentityExpectation) -> None:
    code = "import sys;sys.stdout.buffer.write(b'o'*6000);sys.stderr.buffer.write(b'e'*5000)"
    _, result, _ = _run(
        Command(["/usr/bin/python3", "-c", code]),
        identity,
        capture_limit=4_096,
    )

    assert result.observation.failure is None
    for stream, expected in ((result.observation.stdout, b"o"), (result.observation.stderr, b"e")):
        assert stream is not None
        assert stream.data == expected * 4_096
        assert stream.retained == 4_096
        assert stream.truncated and not stream.complete
        assert stream.retention is StreamRetention.CAPTURED
    assert result.observation.trusted_terminal


def test_sensitive_output_and_raw_reports_retain_no_canary(identity: IdentityExpectation) -> None:
    canary = "inline-sensitive-canary-59ae814d"
    command = Command(
        [
            "/usr/bin/python3",
            "-c",
            "import os,sys;data=sys.stdin.buffer.read();"
            "sys.stdout.buffer.write(data+os.environ['SECRET'].encode()+sys.argv[1].encode());"
            "sys.stderr.buffer.write(data);raise SystemExit(37)",
            canary,
        ]
    )

    prepared, result, carrier = _run(
        command,
        identity,
        stdin=canary.encode(),
        env={"SECRET": canary},
        sensitive=True,
    )

    assert canary not in repr(prepared)
    assert canary not in repr(result)
    assert canary not in prepared.invocation.argv
    assert result.observation.wait is not None and result.observation.wait.value == 37
    assert result.observation.stdout is not None and result.observation.stdout.data == b""
    assert result.observation.stderr is not None and result.observation.stderr.data == b""
    assert result.observation.stdout.retention is StreamRetention.SUPPRESSED
    assert carrier.last_report is not None
    assert carrier.last_report.stdout.retention is carrier.last_report.stderr.retention is Retention.DELIVERED
    assert carrier.last_report.stdout.data == carrier.last_report.stderr.data == b""
    assert canary not in repr(carrier.last_report)
    with pytest.raises(Exception) as caught:
        execute_inline_candidate(carrier, prepared, deadline=Deadline.after(1))
    assert canary not in str(caught.value) and canary not in repr(caught.value)


def test_discard_reports_safe_stream_facts_without_data(identity: IdentityExpectation) -> None:
    _, result, _ = _run(
        Command(["/usr/bin/python3", "-c", "import sys;print('discarded');print('error',file=sys.stderr)"]),
        identity,
        capture_limit=None,
    )

    for stream in (result.observation.stdout, result.observation.stderr):
        assert stream is not None
        assert stream.data == b"" and stream.retained == 0
        assert stream.complete and not stream.truncated
        assert stream.retention is StreamRetention.DISCARDED


def test_script_helper_creates_no_directory_entry(identity: IdentityExpectation, tmp_path: Path) -> None:
    before = set(tmp_path.iterdir())
    _, result, _ = _run(
        Script("printf no-file\n", Shell.SH),
        identity,
        cwd=str(tmp_path),
    )

    assert result.observation.trusted_terminal
    assert set(tmp_path.iterdir()) == before


def test_serialized_candidate_sizes_are_measured_without_provider_acceptance(identity: IdentityExpectation) -> None:
    prepared = prepare_inline_candidate(Command(["/bin/true"]), identity=identity)

    assert prepared.manifest_bytes > 0
    assert prepared.helper_source_bytes > 50_000
    assert prepared.helper_argv_bytes > prepared.helper_source_bytes
