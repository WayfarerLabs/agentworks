"""Local behavioral proof of Linux preparation, without SSH or a live VM."""

from __future__ import annotations

import base64
import os
import select
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.execution.carrier import CapturedOutput, FiniteInput, Provenance, Retention
from agentworks.execution.preparation import (
    MAX_ENVELOPE_BYTES,
    Command,
    DecodedOutput,
    PreparedExecution,
    Script,
    Shell,
    decode_output,
    prepare,
)


def _run(
    prepared: PreparedExecution,
    *,
    env: dict[str, str] | None = None,
    restore_signals: bool = True,
) -> tuple[int, DecodedOutput, bytes]:
    """Launch only local fixture payloads, with a bounded observation budget."""
    if not sys.platform.startswith("linux"):
        pytest.skip("The bootstrap proof targets Linux /dev/fd and GNU tools")
    assert isinstance(prepared.io.input, FiniteInput)
    result = subprocess.run(
        prepared.invocation.argv,
        input=prepared.io.input.data,
        capture_output=True,
        timeout=10,
        env=env,
        restore_signals=restore_signals,
        check=False,
    )
    decoded = decode_output(prepared, CapturedOutput(result.stdout, True, Provenance.CARRIER_STDOUT))
    assert result.stderr == b""
    return result.returncode, decoded, result.stdout


def test_literal_arguments_preserve_boundaries_and_have_no_shell_interpretation() -> None:
    arguments = ("", "two words", "'single'", '"double"', "$(printf BAD)", "; exit 9", "*", "a\nb\n", "雪")
    prepared = prepare(Command(("/usr/bin/printf", "%s\\0", *arguments)))
    status, decoded, _ = _run(prepared)
    assert status == 0
    assert decoded.stdout == b"".join(value.encode() + b"\0" for value in arguments)
    assert decoded.stderr == b""
    assert decoded.stdout_complete and decoded.stderr_complete


@pytest.mark.parametrize("shell", [Shell.fixed("sh"), Shell.fixed("bash")])
def test_script_source_and_finite_binary_input_are_separate(shell: Shell) -> None:
    payload = bytes(range(256)) * 256 + b"\r\n\n"
    prepared = prepare(
        Script("printf 'prefix\\n'; /usr/bin/tee /dev/stderr; printf 'suffix\\n\\n'", shell), stdin=payload
    )
    status, decoded, _ = _run(prepared)
    assert status == 0
    assert decoded.stdout == b"prefix\n" + payload + b"suffix\n\n"
    assert decoded.stderr == payload
    assert decoded.stdout_complete and decoded.stderr_complete


def test_absent_input_is_eof_and_empty_script_succeeds() -> None:
    status, decoded, _ = _run(prepare(Script("cat; printf done", Shell.fixed("sh"))))
    assert status == 0
    assert decoded.stdout == b"done"
    status, decoded, _ = _run(prepare(Script("", Shell.fixed("bash"))))
    assert status == 0
    assert decoded.stdout == decoded.stderr == b""
    assert decoded.stdout_complete and decoded.stderr_complete


def test_helper_pipeline_failure_option_does_not_change_application_shell() -> None:
    status, decoded, _ = _run(prepare(Script("false | true", Shell.fixed("bash"))))
    assert status == 0
    assert decoded.stdout_complete and decoded.stderr_complete


@pytest.mark.parametrize("restore_signals", [True, False])
@pytest.mark.parametrize("consume", [False, True])
def test_payload_may_close_large_stdin_early(restore_signals: bool, consume: bool) -> None:
    command = Command(("/usr/bin/head", "-c", "1")) if consume else Command(("/usr/bin/true",))
    prepared = prepare(command, stdin=b"x" * 150_000)
    status, decoded, _ = _run(prepared, restore_signals=restore_signals)
    assert status == 0
    assert decoded.stdout == (b"x" if consume else b"")
    assert decoded.stdout_complete and decoded.stderr_complete
    assert not decoded.bootstrap_failed


def test_script_may_exit_before_consuming_all_source() -> None:
    prepared = prepare(Script("exit 0\n#" + "x" * 150_000, Shell.fixed("sh")))
    status, decoded, _ = _run(prepared)
    assert status == 0
    assert decoded.stdout_complete and decoded.stderr_complete


def test_sensitive_payload_may_close_large_stdin_early() -> None:
    prepared = prepare(Command(("/usr/bin/true",)), stdin=b"x" * 150_000, sensitive=True)
    status, decoded, _ = _run(prepared)
    assert status == 0
    assert decoded.suppressed


def _active_decoder(root_pid: int) -> int | None:
    """Find only a decoder descended from this test's owned bootstrap."""
    children_file = Path(f"/proc/{root_pid}/task/{root_pid}/children")
    try:
        children = children_file.read_text().split()
    except FileNotFoundError:
        return None
    for child in children:
        child_pid = int(child)
        try:
            argv = Path(f"/proc/{child_pid}/cmdline").read_bytes().split(b"\0")
        except FileNotFoundError:
            continue
        if argv[:2] == [b"/usr/bin/base64", b"--decode"]:
            return child_pid
        descendant = _active_decoder(child_pid)
        if descendant is not None:
            return descendant
    return None


@pytest.mark.parametrize("producer", ["stdin", "source"])
def test_killed_producer_cannot_report_successful_partial_delivery(producer: str) -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("Decoder fault injection requires Linux /proc")
    source = "/bin/sleep 2; /bin/cat" if producer == "stdin" else "/bin/sleep 2\n#" + "x" * 150_000
    prepared = prepare(Script(source, Shell.fixed("sh")), stdin=b"x" * 150_000 if producer == "stdin" else b"")
    assert isinstance(prepared.io.input, FiniteInput)
    with subprocess.Popen(
        prepared.invocation.argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as process:
        try:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(prepared.io.input.data)
            process.stdin.close()
            process.stdin = None
            assert select.select([process.stdout], [], [], 2)[0]
            prefix = process.stdout.readline()
            assert prefix == prepared.token.encode() + b" B\n"
            deadline = time.monotonic() + 2
            decoder = None
            while decoder is None and time.monotonic() < deadline:
                decoder = _active_decoder(process.pid)
                if decoder is None:
                    time.sleep(0.005)
            assert decoder is not None
            os.kill(decoder, signal.SIGTERM)
            stdout, _ = process.communicate(timeout=5)
            decoded = decode_output(prepared, CapturedOutput(prefix + stdout, True, Provenance.CARRIER_STDOUT))
            assert process.returncode == 125
            assert decoded.bootstrap_failed
            assert not decoded.stdout_complete and not decoded.stderr_complete
            if producer == "stdin":
                assert len(decoded.stdout) < 150_000
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)


@pytest.mark.parametrize("code", [0, 1, 255])
def test_bootstrap_returns_payload_status_without_encoding_a_completion_claim(code: int) -> None:
    status, decoded, _ = _run(prepare(Script(f"printf observed; exit {code}", Shell.fixed("sh"))))
    assert status == code
    assert decoded.stdout == b"observed"
    assert decoded.stdout_complete and decoded.stderr_complete


def test_env_and_cwd_apply_to_payload_without_overwriting_bootstrap_state(tmp_path: Path) -> None:
    keys = ("source", "input", "token", "args", "assignments", "i", "executable", "directory")
    environment = dict.fromkeys(keys, "not-the-bootstrap-value")
    environment["MESSAGE"] = "quotes ' \" and newline\n\n"
    prepared = prepare(
        Script('printf "%s\\0%s\\0%s" "$MESSAGE" "$source" "$PWD"', Shell.fixed("sh")),
        env=environment,
        cwd=str(tmp_path),
    )
    status, decoded, _ = _run(prepared)
    assert status == 0
    assert decoded.stdout == environment["MESSAGE"].encode() + b"\0not-the-bootstrap-value\0" + str(tmp_path).encode()


def test_account_shell_is_resolved_at_destination_not_from_shell_environment() -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("The account lookup proof targets Linux")
    import pwd

    shell = pwd.getpwuid(os.getuid()).pw_shell
    prepared = prepare(
        Script('printf "%s" "$0"', Shell.user_default()),
        env={"SHELL": "/does/not/exist"},
    )
    status, decoded, _ = _run(prepared)
    if shell in {"/bin/bash", "/usr/bin/bash", "/bin/sh", "/usr/bin/sh"}:
        assert status == 0
        assert decoded.stdout == b"/dev/fd/5"
        assert decoded.stdout_complete and decoded.stderr_complete
    else:
        assert status == 125
        assert decoded.bootstrap_failed


@pytest.mark.parametrize("inherited", [None, "", "POSIX"])
def test_internal_locale_does_not_change_payload_environment(inherited: str | None) -> None:
    environment = dict(os.environ)
    environment.pop("LC_ALL", None)
    if inherited is not None:
        environment["LC_ALL"] = inherited
    prepared = prepare(Script('printf "%s:%s" "${LC_ALL+x}" "${LC_ALL-}"', Shell.fixed("sh")))
    status, decoded, _ = _run(prepared, env=environment)
    assert status == 0
    assert decoded.stdout == (b":" if inherited is None else b"x:" + inherited.encode())


def test_explicit_payload_locale_wins_over_inherited_and_internal_locale() -> None:
    environment = {**os.environ, "LC_ALL": "C"}
    prepared = prepare(Script('printf "%s" "$LC_ALL"', Shell.fixed("sh")), env={"LC_ALL": "POSIX"})
    status, decoded, _ = _run(prepared, env=environment)
    assert status == 0
    assert decoded.stdout == b"POSIX"


def test_inherited_exported_variables_keep_their_values_in_payload() -> None:
    values = {name: f"inherited-{name}" for name in ("source", "input", "token", "args", "count", "decoded")}
    prepared = prepare(Command(("/usr/bin/env", "-0")), stdin=b"application-input")
    status, decoded, _ = _run(prepared, env={**os.environ, **values})
    assert status == 0
    entries = decoded.stdout.split(b"\0")
    assert all(key.encode() + b"=" + value.encode() in entries for key, value in values.items())


def test_inherited_reserved_exports_cannot_expose_helper_payload_or_functions() -> None:
    reserved = (
        "source",
        "input",
        "token",
        "encoded",
        "decoded",
        "value",
        "inherited_lc_all",
        "inherited_lc_all_set",
        "assignment",
    )
    environment = {**os.environ, **dict.fromkeys((f"_agw_{name}" for name in reserved), "inherited-value")}
    environment["BASH_FUNC__agw_fail%%"] = "() { printf inherited-function; }"
    canary = b"synthetic-input-canary"
    prepared = prepare(Script("/usr/bin/env -0", Shell.fixed("bash")), stdin=canary)
    status, decoded, _ = _run(prepared, env=environment)
    assert status == 0
    assert canary not in decoded.stdout
    assert base64.b64encode(canary) not in decoded.stdout
    assert not any(entry.startswith((b"_agw_", b"BASH_FUNC__agw_")) for entry in decoded.stdout.split(b"\0"))


def test_no_staging_and_inherited_bash_env_does_not_run(tmp_path: Path) -> None:
    hook = tmp_path / "hook.sh"
    marker = tmp_path / "hook-ran"
    hook.write_text(f"touch '{marker}'\n", encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()
    environment = {**os.environ, "BASH_ENV": str(hook), "ENV": str(hook), "TMPDIR": str(staging)}
    prepared = prepare(Script("printf ready", Shell.fixed("bash")), cwd=str(staging))
    status, decoded, _ = _run(prepared, env=environment)
    assert status == 0
    assert decoded.stdout == b"ready"
    assert not marker.exists()
    assert list(staging.iterdir()) == []


def test_sensitive_source_environment_and_input_are_not_arguments_or_retained_output() -> None:
    secret = "sensitive-canary-26c8e2"
    prepared = prepare(
        Script(f'printf "%s" "{secret}"; printf "%s" "$SECRET" >&2; cat', Shell.fixed("bash")),
        stdin=secret.encode(),
        env={"SECRET": secret},
        sensitive=True,
    )
    assert secret not in repr(prepared)
    assert all(secret not in argument for argument in prepared.invocation.argv)
    status, decoded, raw = _run(prepared)
    assert status == 0
    assert secret.encode() not in raw
    assert base64.b64encode(secret.encode()) not in raw
    assert decoded.suppressed
    assert not decoded.stdout_complete and not decoded.stderr_complete
    assert decoded.stdout == decoded.stderr == b""


def test_sensitive_script_trace_is_suppressed_on_the_guest() -> None:
    prepared = prepare(Script("set -x; printf sensitive-trace", Shell.fixed("bash")), sensitive=True)
    status, decoded, raw = _run(prepared)
    assert status == 0
    assert b"sensitive-trace" not in raw
    assert decoded.suppressed


def test_setup_failure_is_not_complete_guest_output(tmp_path: Path) -> None:
    prepared = prepare(Command(("/usr/bin/true",)), cwd=str(tmp_path / "absent"))
    status, decoded, _ = _run(prepared)
    assert status == 125
    assert decoded.bootstrap_failed
    assert not decoded.stdout_complete and not decoded.stderr_complete


def test_readonly_shell_environment_assignment_failure_is_recorded() -> None:
    prepared = prepare(Command(("/usr/bin/true",)), env={"UID": "something"})
    status, decoded, _ = _run(prepared)
    assert status == 125
    assert decoded.bootstrap_failed


def test_malformed_input_fails_before_application_execution() -> None:
    prepared = prepare(Script("printf must-not-run", Shell.fixed("sh")))
    assert isinstance(prepared.io.input, FiniteInput)
    lines = prepared.io.input.data.split(b"\n")
    lines[-2] = b"!not-base64!"
    corrupted = replace(prepared, io=replace(prepared.io, input=FiniteInput(b"\n".join(lines))))
    assert isinstance(corrupted.io.input, FiniteInput)
    if not sys.platform.startswith("linux"):
        pytest.skip("The bootstrap proof targets Linux")
    result = subprocess.run(
        corrupted.invocation.argv, input=corrupted.io.input.data, capture_output=True, timeout=10, check=False
    )
    decoded = decode_output(corrupted, CapturedOutput(result.stdout, True, Provenance.CARRIER_STDOUT))
    assert result.returncode == 125
    assert decoded.bootstrap_failed
    assert not decoded.stdout_complete and not decoded.stderr_complete
    assert b"must-not-run" not in result.stdout


@pytest.mark.parametrize("login,interactive", [(True, False), (False, True), (True, True)])
def test_unproven_startup_modes_are_refused(login: bool, interactive: bool) -> None:
    with pytest.raises(ValidationError):
        prepare(Script("exit 0", Shell.fixed("bash", login=login, interactive=interactive)))


@pytest.mark.parametrize("key", ["BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "BASH_XTRACEFD", "_agw_assignment"])
def test_environment_cannot_undo_bootstrap_isolation(key: str) -> None:
    with pytest.raises(ValidationError):
        prepare(Command(("/usr/bin/true",)), env={key: "value"})


def test_input_envelope_is_bounded_and_payload_has_no_diagnostic_representation() -> None:
    with pytest.raises(ValidationError):
        prepare(Command(("/usr/bin/cat",)), stdin=b"x" * MAX_ENVELOPE_BYTES)
    assert "canary" not in repr(Command(("canary",)))
    assert "canary" not in repr(Script("canary", Shell.fixed("sh")))


@pytest.mark.parametrize("argument", ["contains\0nul", "invalid\ud800unicode"])
def test_invalid_text_is_refused_without_exposing_payload(argument: str) -> None:
    with pytest.raises(ValidationError) as error:
        prepare(Command(("/usr/bin/printf", argument)))
    assert argument not in str(error.value)


def _records(prepared: PreparedExecution, *records: bytes) -> CapturedOutput:
    prefix = prepared.token.encode() + b" "
    return CapturedOutput(b"".join(prefix + record + b"\n" for record in records), True, Provenance.CARRIER_STDOUT)


def test_decoder_preserves_valid_partial_bytes_but_does_not_claim_complete_output() -> None:
    prepared = prepare(Command(("/usr/bin/true",)))
    output = _records(prepared, b"B", b"O YWJj", b"E eHl6", b"O !", b"E !")
    decoded = decode_output(prepared, replace(output, complete=False))
    assert decoded.stdout == b"abc"
    assert decoded.stderr == b"xyz"
    assert not decoded.stdout_complete and not decoded.stderr_complete


@pytest.mark.parametrize("bad_record", [b"O !!!", b"X YWJj", b"O", b"B", b"E " + b"a" * 80])
def test_malformed_records_do_not_become_guest_output(bad_record: bytes) -> None:
    prepared = prepare(Command(("/usr/bin/true",)))
    decoded = decode_output(prepared, _records(prepared, b"B", b"O YWJj", bad_record, b"O !", b"E !"))
    assert decoded.stdout == b"abc"
    assert decoded.stderr == b""
    assert decoded.framing_error
    assert not decoded.stdout_complete and not decoded.stderr_complete


def test_wrong_token_raw_stderr_and_data_after_stream_end_are_not_trusted() -> None:
    prepared = prepare(Command(("/usr/bin/true",)))
    valid = _records(prepared, b"B", b"O !", b"E !")
    assert decode_output(prepared, replace(valid, provenance=Provenance.MIXED_STDERR)).framing_error
    assert decode_output(
        prepared, replace(valid, data=valid.data.replace(prepared.token.encode(), b"wrong"))
    ).framing_error
    assert decode_output(prepared, _records(prepared, b"B", b"O !", b"O YWJj", b"E !")).framing_error


def test_suppressed_carrier_output_never_becomes_a_framing_completion_claim() -> None:
    prepared = prepare(Command(("/usr/bin/true",)))
    decoded = decode_output(prepared, CapturedOutput(retention=Retention.SUPPRESSED))
    assert decoded.suppressed
    assert not decoded.stdout_complete and not decoded.stderr_complete
    assert not decoded.framing_error


def test_unterminated_record_keeps_prior_bytes_and_is_incomplete() -> None:
    prepared = prepare(Command(("/usr/bin/true",)))
    output = _records(prepared, b"B", b"O YWJj")
    decoded = decode_output(prepared, replace(output, data=output.data + prepared.token.encode()))
    assert decoded.stdout == b"abc"
    assert decoded.framing_error
    assert not decoded.stdout_complete and not decoded.stderr_complete
