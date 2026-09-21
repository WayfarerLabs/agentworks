"""Production runtime selector and prefix observation checks."""

from __future__ import annotations

import os
import shlex
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agentworks.execution import _runtime_prerequisite
from agentworks.execution._runtime_prerequisite import (
    RuntimePrefixSink,
    RuntimePrerequisiteObservation,
    RuntimePrerequisiteState,
    RuntimeSelection,
    RuntimeTargetOS,
    build_runtime_helper_argv,
)
from agentworks.execution.carrier import CarrierIO, Deadline, Failure, FiniteInput, SinkOutput
from agentworks.execution.carriers._subprocess import ProcessResult, run_process

_NONCE = "0123456789abcdef0123456789abcdef"
_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX shell checks")


@dataclass
class _Sink:
    limit: int | None = None
    stalls: int = 0
    data: bytearray = field(default_factory=bytearray)
    calls: int = 0

    def try_write(self, data: memoryview) -> int | None:
        self.calls += 1
        if self.stalls:
            self.stalls -= 1
            return None
        count = len(data) if self.limit is None else min(self.limit, len(data))
        self.data.extend(data[:count])
        return count


def _executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\nset -eu\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _emulated_runtime(
    path: Path,
    state: RuntimePrerequisiteState,
    *,
    before_record: str = "",
) -> Path:
    return _executable(
        path,
        '[ "$1" = -I ]\n'
        '[ "$2" = -S ]\n'
        '[ "$3" = -B ]\n'
        '[ "$4" = -c ]\n' + before_record + f'printf \'AGW_RUNTIME_1:%s:{state.value}:%s\\n\' "$6" "$7"\n',
    )


def _run(
    selection: RuntimeSelection,
    *,
    payload: bytes = b"",
    helper_source: str = "",
    timeout: float = 5,
) -> tuple[RuntimePrerequisiteObservation, _Sink, _Sink, ProcessResult]:
    argv, candidates, system_shim = build_runtime_helper_argv(
        selection=selection,
        fixed_source=helper_source,
        nonce=_NONCE,
    )
    output = _Sink()
    stderr = _Sink()
    prefix = RuntimePrefixSink(_NONCE, candidates, output, system_shim)
    result = run_process(
        list(argv),
        io=CarrierIO(
            input=FiniteInput(payload, sensitive=True),
            output=SinkOutput(prefix, stderr),
            sensitive=True,
        ),
        deadline=Deadline.after(timeout),
    )
    return prefix.observation, output, stderr, result


def _run_trampoline(preamble: str) -> tuple[RuntimePrerequisiteObservation, _Sink, _Sink, ProcessResult]:
    helper = "import os\nos.write(1,b'helper-entered')\n"
    # Exercise Windows text translation on every test host; protocol output is binary.
    harness = (
        "import sys\nsys.stdout.reconfigure(newline='\\r\\n')\n"
        + preamble
        + "\nexec(compile("
        + repr(_runtime_prerequisite._TRAMPOLINE)
        + ",'<runtime-test>','exec'))\n"
    )
    output = _Sink()
    stderr = _Sink()
    prefix = RuntimePrefixSink(_NONCE, (sys.executable,), output)
    result = run_process(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            harness,
            _NONCE,
            "0",
            helper,
        ],
        io=CarrierIO(output=SinkOutput(prefix, stderr), sensitive=True),
        deadline=Deadline.after(5),
    )
    return prefix.observation, output, stderr, result


def _feed(sink: RuntimePrefixSink, content: bytes, chunks: tuple[int, ...]) -> None:
    offset = 0
    chunk_index = 0
    attempts_remaining = max(100, len(content) * 2)
    while offset < len(content):
        size = chunks[chunk_index % len(chunks)]
        chunk_index += 1
        end = min(offset + size, len(content))
        pending = memoryview(content)[offset:end]
        while pending:
            if attempts_remaining == 0:
                pytest.fail("prefix sink did not consume input within the attempt budget")
            attempts_remaining -= 1
            written = sink.try_write(pending)
            if written is None:
                continue
            pending = pending[written:]
            offset += written


@_POSIX_ONLY
def test_actual_python_admits_and_passes_binary_sensitive_input_unchanged() -> None:
    payload = bytes(range(256)) * 17
    helper = (
        "import os\n"
        "b=bytearray()\n"
        "while True:\n"
        " c=os.read(0,65536)\n"
        " if not c:break\n"
        " b.extend(c)\n"
        "os.write(1,bytes(b))\n"
    )

    observation, output, stderr, result = _run(
        RuntimeSelection(RuntimeTargetOS.LINUX, sys.executable),
        payload=payload,
        helper_source=helper,
    )

    assert observation == RuntimePrerequisiteObservation(RuntimePrerequisiteState.READY, sys.executable)
    assert bytes(output.data) == payload
    assert not stderr.data
    assert result.failure is None
    assert result.exit_status == 0


def test_target_os_selects_fixed_candidates_without_host_inference() -> None:
    _, linux_candidates, linux_shim = build_runtime_helper_argv(
        selection=RuntimeSelection(RuntimeTargetOS.LINUX),
        fixed_source="",
        nonce=_NONCE,
    )
    _, darwin_candidates, darwin_shim = build_runtime_helper_argv(
        selection=RuntimeSelection(RuntimeTargetOS.DARWIN),
        fixed_source="",
        nonce=_NONCE,
    )

    assert linux_candidates == ("/usr/bin/python3",)
    assert linux_shim is None
    assert darwin_candidates == ("/opt/homebrew/bin/python3", "/usr/local/bin/python3")
    assert darwin_shim == "/usr/bin/python3"


@pytest.mark.windows
def test_ready_trampoline_preserves_binary_record_before_helper_output() -> None:
    observation, output, stderr, result = _run_trampoline("")

    assert observation == RuntimePrerequisiteObservation(RuntimePrerequisiteState.READY, sys.executable)
    assert bytes(output.data) == b"helper-entered"
    assert not stderr.data
    assert result.failure is None
    assert result.exit_status == 0


@pytest.mark.windows
def test_synthetic_old_version_executes_production_trampoline_and_refuses() -> None:
    observation, output, stderr, result = _run_trampoline("import sys\nsys.version_info=(3,10)")

    assert observation == RuntimePrerequisiteObservation(
        RuntimePrerequisiteState.UNSUPPORTED_VERSION,
        sys.executable,
    )
    assert not output.data
    assert not stderr.data
    assert result.failure is None
    assert result.exit_status == 0


@pytest.mark.windows
def test_synthetic_missing_bz2_executes_production_trampoline_and_refuses() -> None:
    preamble = """import builtins
real_import=builtins.__import__
def blocked_import(name,globals=None,locals=None,fromlist=(),level=0):
 if name=='bz2':raise ImportError
 return real_import(name,globals,locals,fromlist,level)
builtins.__import__=blocked_import
"""

    observation, output, stderr, result = _run_trampoline(preamble)

    assert observation == RuntimePrerequisiteObservation(
        RuntimePrerequisiteState.MISSING_MODULES,
        sys.executable,
    )
    assert not output.data
    assert not stderr.data
    assert result.failure is None
    assert result.exit_status == 0


@pytest.mark.parametrize("kind", ["broken-symlink", "not-executable", "directory"])
@_POSIX_ONLY
def test_first_existing_unusable_entry_is_selected_without_fallback(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    if kind == "broken-symlink":
        first.symlink_to(tmp_path / "absent-target")
    elif kind == "not-executable":
        first.write_text("not executable")
    else:
        first.mkdir()
    fallback_marker = tmp_path / "fallback-ran"
    second = _emulated_runtime(
        tmp_path / "second",
        RuntimePrerequisiteState.READY,
        before_record=f"touch {shlex.quote(os.fspath(fallback_marker))}\n",
    )
    monkeypatch.setattr(_runtime_prerequisite, "_DARWIN_CANDIDATES", (os.fspath(first), os.fspath(second)))
    monkeypatch.setattr(_runtime_prerequisite, "_DARWIN_SYSTEM_SHIM", os.fspath(tmp_path / "absent-shim"))

    observation, _, _, _ = _run(RuntimeSelection(RuntimeTargetOS.DARWIN))

    assert observation == RuntimePrerequisiteObservation(RuntimePrerequisiteState.UNUSABLE, os.fspath(first))
    assert not fallback_marker.exists()


@_POSIX_ONLY
def test_missing_first_fixed_entry_selects_second(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    second = _emulated_runtime(tmp_path / "second", RuntimePrerequisiteState.READY)
    monkeypatch.setattr(
        _runtime_prerequisite,
        "_DARWIN_CANDIDATES",
        (os.fspath(tmp_path / "absent"), os.fspath(second)),
    )
    monkeypatch.setattr(_runtime_prerequisite, "_DARWIN_SYSTEM_SHIM", os.fspath(tmp_path / "absent-shim"))

    observation, _, _, _ = _run(RuntimeSelection(RuntimeTargetOS.DARWIN))

    assert observation == RuntimePrerequisiteObservation(RuntimePrerequisiteState.READY, os.fspath(second))


@_POSIX_ONLY
def test_explicit_absent_path_is_sole_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fallback_marker = tmp_path / "fallback-ran"
    fallback = _emulated_runtime(
        tmp_path / "fallback",
        RuntimePrerequisiteState.READY,
        before_record=f"touch {shlex.quote(os.fspath(fallback_marker))}\n",
    )
    monkeypatch.setattr(_runtime_prerequisite, "_DARWIN_CANDIDATES", (os.fspath(fallback),))
    monkeypatch.setattr(_runtime_prerequisite, "_DARWIN_SYSTEM_SHIM", os.fspath(tmp_path / "absent-shim"))

    observation, _, _, _ = _run(RuntimeSelection(RuntimeTargetOS.DARWIN, os.fspath(tmp_path / "absent-explicit")))

    assert observation == RuntimePrerequisiteObservation(RuntimePrerequisiteState.MISSING, None)
    assert not fallback_marker.exists()


@pytest.mark.parametrize("alias_kind", ["direct", "symlink", "hardlink"])
@_POSIX_ONLY
def test_darwin_system_shim_alias_is_rejected_without_execution(
    alias_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoked = tmp_path / "shim-invoked"
    shim = _executable(tmp_path / "system-python3", f"touch {shlex.quote(os.fspath(invoked))}\n")
    if alias_kind == "direct":
        candidate = shim
    elif alias_kind == "symlink":
        candidate = tmp_path / "candidate-link"
        candidate.symlink_to(shim)
    else:
        candidate = tmp_path / "candidate-hardlink"
        os.link(shim, candidate)
    monkeypatch.setattr(_runtime_prerequisite, "_DARWIN_SYSTEM_SHIM", os.fspath(shim))

    observation, _, _, _ = _run(RuntimeSelection(RuntimeTargetOS.DARWIN, os.fspath(candidate)))

    assert observation == RuntimePrerequisiteObservation(RuntimePrerequisiteState.SHIM, os.fspath(candidate))
    assert not invoked.exists()


@_POSIX_ONLY
def test_darwin_shim_only_state_does_not_execute_system_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoked = tmp_path / "shim-invoked"
    shim = _executable(tmp_path / "system-python3", f"touch {shlex.quote(os.fspath(invoked))}\n")
    monkeypatch.setattr(_runtime_prerequisite, "_DARWIN_CANDIDATES", (os.fspath(tmp_path / "absent"),))
    monkeypatch.setattr(_runtime_prerequisite, "_DARWIN_SYSTEM_SHIM", os.fspath(shim))

    observation, _, _, _ = _run(RuntimeSelection(RuntimeTargetOS.DARWIN))

    assert observation == RuntimePrerequisiteObservation(RuntimePrerequisiteState.SHIM, os.fspath(shim))
    assert not invoked.exists()


@_POSIX_ONLY
def test_linux_does_not_classify_its_fixed_python_as_a_shim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _emulated_runtime(tmp_path / "python3", RuntimePrerequisiteState.READY)
    monkeypatch.setattr(_runtime_prerequisite, "_LINUX_CANDIDATES", (os.fspath(candidate),))
    monkeypatch.setattr(_runtime_prerequisite, "_DARWIN_SYSTEM_SHIM", os.fspath(candidate))

    observation, _, _, _ = _run(RuntimeSelection(RuntimeTargetOS.LINUX))

    assert observation == RuntimePrerequisiteObservation(RuntimePrerequisiteState.READY, os.fspath(candidate))


@pytest.mark.parametrize(
    "state",
    [RuntimePrerequisiteState.UNSUPPORTED_VERSION, RuntimePrerequisiteState.MISSING_MODULES],
)
@_POSIX_ONLY
def test_synthetic_runtime_refusals_are_closed_and_do_not_enter_helper(
    state: RuntimePrerequisiteState,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "helper-ran"
    runtime = _emulated_runtime(tmp_path / "synthetic-runtime", state)

    observation, output, _, _ = _run(
        RuntimeSelection(RuntimeTargetOS.LINUX, os.fspath(runtime)),
        helper_source=f"open({os.fspath(marker)!r},'w').close()",
    )

    assert observation == RuntimePrerequisiteObservation(state, os.fspath(runtime))
    assert not output.data
    assert not marker.exists()


@_POSIX_ONLY
def test_raw_runtime_diagnostic_is_not_retained_in_observation(tmp_path: Path) -> None:
    canary = "raw-runtime-canary-142b"
    runtime = _emulated_runtime(
        tmp_path / "synthetic-runtime",
        RuntimePrerequisiteState.MISSING_MODULES,
        before_record=f"printf %s {shlex.quote(canary)} >&2\n",
    )

    observation, _, _, _ = _run(RuntimeSelection(RuntimeTargetOS.LINUX, os.fspath(runtime)))

    assert observation.state is RuntimePrerequisiteState.MISSING_MODULES
    assert canary not in repr(observation)


def test_prefix_sink_preserves_fragmented_bytes_and_downstream_backpressure() -> None:
    downstream = _Sink(limit=3, stalls=4)
    sink = RuntimePrefixSink(_NONCE, ("/runtime",), downstream)
    payload = bytes(range(256)) * 3
    transcript = f"AGW_RUNTIME_1:{_NONCE}:ready:0\n".encode("ascii") + payload

    _feed(sink, transcript, (1, 2, 17, 5, 64))

    assert sink.observation == RuntimePrerequisiteObservation(RuntimePrerequisiteState.READY, "/runtime")
    assert bytes(downstream.data) == payload
    assert downstream.calls > len(payload) // 3


@pytest.mark.parametrize(
    "transcript",
    [
        b"",
        b"AGW_RUNTIME_1:0123456789abcdef0123456789abcdef:ready:0",
        b"noise\n",
        b"AGW_RUNTIME_1:ffffffffffffffffffffffffffffffff:ready:0\n",
    ],
)
def test_missing_truncated_or_malformed_record_is_unknown(transcript: bytes) -> None:
    downstream = _Sink()
    sink = RuntimePrefixSink(_NONCE, ("/runtime",), downstream)

    if transcript:
        _feed(sink, transcript, (2, 7, 1))

    assert sink.observation == RuntimePrerequisiteObservation(RuntimePrerequisiteState.UNKNOWN, None)
    assert not downstream.data


def test_trailing_bytes_after_refusal_invalidate_transcript() -> None:
    downstream = _Sink()
    sink = RuntimePrefixSink(_NONCE, ("/runtime",), downstream)
    refusal = f"AGW_RUNTIME_1:{_NONCE}:unusable:0\n".encode("ascii")

    _feed(sink, refusal, (len(refusal),))
    assert sink.observation.state is RuntimePrerequisiteState.UNUSABLE
    _feed(sink, b"trailing", (2, 3))

    assert sink.observation == RuntimePrerequisiteObservation(RuntimePrerequisiteState.UNKNOWN, None)
    assert not downstream.data


def test_linux_shim_record_without_bound_shim_is_unknown() -> None:
    downstream = _Sink()
    sink = RuntimePrefixSink(_NONCE, ("/usr/bin/python3",), downstream)
    record = f"AGW_RUNTIME_1:{_NONCE}:shim:0\n".encode("ascii")

    _feed(sink, record, (len(record),))

    assert sink.observation == RuntimePrerequisiteObservation(RuntimePrerequisiteState.UNKNOWN, None)
    assert not downstream.data


@_POSIX_ONLY
def test_early_refusal_with_large_finite_input_remains_bounded_and_truthful(tmp_path: Path) -> None:
    observation, output, stderr, result = _run(
        RuntimeSelection(RuntimeTargetOS.LINUX, os.fspath(tmp_path / "absent")),
        payload=b"sensitive\x00" * 500_000,
    )

    assert observation.state in (RuntimePrerequisiteState.MISSING, RuntimePrerequisiteState.UNKNOWN)
    assert not output.data
    assert not stderr.data
    assert result.failure is Failure.INPUT
