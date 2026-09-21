"""Linux end-to-end checks for the fixed private inline helper candidate."""

from __future__ import annotations

import os
import shlex
import signal
import sys
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._inline import (
    InlineCandidateResult,
    PreparedInlineCandidate,
    execute_inline_candidate,
    prepare_inline_candidate,
)
from agentworks.execution._inline_control import FailureCode, FailurePhase, StreamRetention, WaitFact, WaitKind
from agentworks.execution._inline_observer import InlineObservation
from agentworks.execution._runtime_prerequisite import (
    RuntimePrerequisiteState,
    RuntimeSelection,
    RuntimeTargetOS,
)
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
def plan() -> IdentityPlan:
    return IdentityPlan(
        IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()}))),
        IdentityMode.DIRECT,
    )


def _run(
    request: Command | Script,
    plan: IdentityPlan,
    *,
    helper_runtime: str = sys.executable,
    carrier: LocalCarrier | None = None,
    **options: object,
) -> tuple[PreparedInlineCandidate, InlineCandidateResult, LocalCarrier]:
    selected_carrier = carrier or LocalCarrier()
    prepared = prepare_inline_candidate(
        request,
        plan=plan,
        runtime_selection=RuntimeSelection(RuntimeTargetOS.LINUX, helper_runtime),
        **options,
    )
    result = execute_inline_candidate(selected_carrier, prepared, deadline=Deadline.after(15))
    assert selected_carrier.calls == 1
    return prepared, result, selected_carrier


def _observation(result: InlineCandidateResult) -> InlineObservation:
    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    assert result.observation is not None
    return result.observation


@pytest.mark.parametrize("status", range(256))
def test_literal_command_preserves_every_normal_exit(status: int, plan: IdentityPlan) -> None:
    _, result, _ = _run(
        Command(["/usr/bin/python3", "-I", "-S", "-B", "-c", f"raise SystemExit({status})"]),
        plan,
    )
    observation = _observation(result)

    assert observation.trusted_terminal
    assert observation.wait is not None
    assert observation.wait.kind is WaitKind.EXIT
    assert observation.wait.value == status
    assert observation.failure is None
    assert result.carrier_completion == ExitStatus(code=0)


def test_signaled_command_keeps_exact_signal_separate(plan: IdentityPlan) -> None:
    _, result, _ = _run(
        Command(["/usr/bin/python3", "-c", "import os,signal; os.kill(os.getpid(), signal.SIGTERM)"]),
        plan,
    )
    observation = _observation(result)

    assert observation.wait is not None
    assert observation.wait.kind is WaitKind.SIGNAL
    assert observation.wait.value == signal.SIGTERM
    assert observation.trusted_terminal


def test_literal_arguments_are_not_reparsed(plan: IdentityPlan) -> None:
    arguments = ["space value", "'\"$()`;", "line1\nline2", "snowman-\u2603"]
    code = f"import sys;assert sys.argv[1:]=={arguments!r}"

    _, result, _ = _run(Command(["/usr/bin/python3", "-I", "-S", "-B", "-c", code, *arguments]), plan)
    observation = _observation(result)

    assert observation.wait == WaitFact(WaitKind.EXIT, 0)
    assert observation.trusted_terminal


@pytest.mark.parametrize("shell", [Shell.SH, Shell.BASH])
def test_memfd_script_separates_source_binary_input_and_streams(
    shell: Shell,
    plan: IdentityPlan,
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
        plan,
        helper_runtime=helper_runtime,
        stdin=stdin,
    )
    observation = _observation(result)

    assert observation.wait is not None and observation.wait.value == 255
    assert observation.stdout is not None and observation.stdout.data == stdout
    assert observation.stderr is not None and observation.stderr.data == stderr
    assert observation.trusted_terminal
    assert source not in prepared.invocation.argv


def test_payload_environment_and_absolute_cwd_replace_helper_environment(
    plan: IdentityPlan,
    tmp_path: Path,
) -> None:
    _, env_result, _ = _run(
        Command(["/usr/bin/env", "-0"]),
        plan,
        env={"ONLY_PAYLOAD": "present"},
        cwd=str(tmp_path),
    )
    _, cwd_result, _ = _run(Command(["/bin/pwd"]), plan, cwd=str(tmp_path))
    env_observation = _observation(env_result)
    cwd_observation = _observation(cwd_result)

    assert env_observation.stdout is not None
    assert env_observation.stdout.data == b"ONLY_PAYLOAD=present\0"
    assert cwd_observation.stdout is not None
    assert cwd_observation.stdout.data == f"{tmp_path}\n".encode()


def test_identity_mismatch_refuses_before_launch(plan: IdentityPlan) -> None:
    expected = plan.expected
    mismatched = IdentityPlan(
        IdentityExpectation(expected.euid + 100_000, expected.egid, expected.groups),
        IdentityMode.DIRECT,
    )
    _, result, _ = _run(Command(["/bin/true"]), mismatched)

    observation = _observation(result)
    assert observation.trusted_terminal
    assert not observation.launching
    assert observation.wait is None
    assert observation.stdout is None and observation.stderr is None
    assert observation.failure is not None
    assert observation.failure.phase is FailurePhase.IDENTITY
    assert observation.failure.code is FailureCode.MISMATCH


def test_unsupported_user_default_shell_refuses_without_fallback(plan: IdentityPlan) -> None:
    import pwd

    configured = pwd.getpwuid(plan.expected.euid).pw_shell
    if configured in {"/bin/sh", "/usr/bin/sh", "/bin/bash", "/usr/bin/bash"}:
        pytest.skip("The local account has a supported default shell")

    _, result, _ = _run(Script("exit 0\n", Shell.USER_DEFAULT), plan)
    observation = _observation(result)

    assert observation.failure is not None
    assert observation.failure.phase is FailurePhase.IDENTITY
    assert observation.failure.code is FailureCode.SHELL
    assert not observation.launching


def test_capture_overflow_retains_limit_and_reports_truncation(plan: IdentityPlan) -> None:
    code = "import sys;sys.stdout.buffer.write(b'o'*6000);sys.stderr.buffer.write(b'e'*5000)"
    _, result, _ = _run(
        Command(["/usr/bin/python3", "-c", code]),
        plan,
        capture_limit=4_096,
    )
    observation = _observation(result)

    assert observation.failure is None
    for stream, expected in ((observation.stdout, b"o"), (observation.stderr, b"e")):
        assert stream is not None
        assert stream.data == expected * 4_096
        assert stream.retained == 4_096
        assert stream.truncated and not stream.complete
        assert stream.retention is StreamRetention.CAPTURED
    assert observation.trusted_terminal


def test_sensitive_output_and_raw_reports_retain_no_canary(plan: IdentityPlan) -> None:
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
        plan,
        stdin=canary.encode(),
        env={"SECRET": canary},
        sensitive=True,
    )
    observation = _observation(result)

    assert canary not in repr(prepared)
    assert canary not in repr(result)
    assert canary not in prepared.invocation.argv
    assert observation.wait is not None and observation.wait.value == 37
    assert observation.stdout is not None and observation.stdout.data == b""
    assert observation.stderr is not None and observation.stderr.data == b""
    assert observation.stdout.retention is StreamRetention.SUPPRESSED
    assert carrier.last_report is not None
    assert carrier.last_report.stdout.retention is carrier.last_report.stderr.retention is Retention.DELIVERED
    assert carrier.last_report.stdout.data == carrier.last_report.stderr.data == b""
    assert canary not in repr(carrier.last_report)
    with pytest.raises(ValidationError) as caught:
        execute_inline_candidate(carrier, prepared, deadline=Deadline.after(1))
    assert carrier.calls == 1
    assert canary not in str(caught.value) and canary not in repr(caught.value)


def test_discard_reports_safe_stream_facts_without_data(plan: IdentityPlan) -> None:
    _, result, _ = _run(
        Command(["/usr/bin/python3", "-c", "import sys;print('discarded');print('error',file=sys.stderr)"]),
        plan,
        capture_limit=None,
    )
    observation = _observation(result)

    for stream in (observation.stdout, observation.stderr):
        assert stream is not None
        assert stream.data == b"" and stream.retained == 0
        assert stream.complete and not stream.truncated
        assert stream.retention is StreamRetention.DISCARDED


def test_script_helper_creates_no_directory_entry(plan: IdentityPlan, tmp_path: Path) -> None:
    before = set(tmp_path.iterdir())
    _, result, _ = _run(
        Script("printf no-file\n", Shell.SH),
        plan,
        cwd=str(tmp_path),
    )
    observation = _observation(result)

    assert observation.trusted_terminal
    assert set(tmp_path.iterdir()) == before
