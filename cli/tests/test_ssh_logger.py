"""Tests for ``SSHLogger`` close-time exception capture.

When ``close()`` is called from inside an ``except`` block (the pattern
used by every operation-level handler in ``agentworks.vms.initializer`` and
elsewhere), the in-flight exception's traceback should be appended to
the per-op log. That keeps operation-level errors out of the shared
``error.log`` at the top of the config dir.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.vm_platform import BootstrapProgress
from agentworks.ssh import SSHLogger, SSHResult

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any


@pytest.fixture
def logger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SSHLogger]:
    """Build a fresh ``SSHLogger`` rooted in ``tmp_path`` (not the user's
    real config dir) so the test doesn't pollute on-disk state."""
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    yield SSHLogger("vm1", "test-op")


def test_close_appends_traceback_when_called_inside_except(logger: SSHLogger) -> None:
    """``close()`` introspects ``sys.exc_info()``. An exception in flight
    at close time gets its traceback appended to the log file before the
    footer. This is what routes operation-level errors away from the
    central ``error.log`` and into the per-op log instead."""
    try:
        raise RuntimeError("synthetic operation failure")
    except RuntimeError:
        logger.close()

    text = logger.path.read_text()
    assert "EXCEPTION:" in text
    assert "RuntimeError: synthetic operation failure" in text
    # Footer still emits after the traceback.
    assert "# Finished:" in text


def test_close_without_exception_omits_traceback_block(logger: SSHLogger) -> None:
    """A bare ``close()`` (no exception in flight) writes the footer
    only -- the traceback block is gated on ``sys.exc_info()``."""
    logger.close()

    text = logger.path.read_text()
    assert "EXCEPTION:" not in text
    assert "# Finished:" in text


def test_ssh_logger_structurally_satisfies_bootstrap_progress(logger: SSHLogger) -> None:
    progress: BootstrapProgress = logger

    progress.step("bootstrap step")
    progress.output("bootstrap output")
    progress.warning("bootstrap warning")
    progress.log_error("bootstrap error")
    logger.close()

    text = logger.path.read_text()
    assert "bootstrap step" in text
    assert "bootstrap output" in text
    assert "bootstrap warning" in text
    assert "bootstrap error" in text


@pytest.mark.windows
def test_path_retargeting_moves_subsequent_records(
    logger: SSHLogger,
    tmp_path: Path,
) -> None:
    """Backup logging changes the public path after construction; the active
    handler must follow it without copying prior records."""
    original_path = logger.path
    retargeted = tmp_path / "backup" / "backup.log"
    logger.path = retargeted

    logger.output("backup payload")
    logger.close()

    assert "backup payload" not in original_path.read_text()
    assert "backup payload" in retargeted.read_text()
    assert "# Finished:" in retargeted.read_text()


@pytest.mark.windows
def test_write_error_propagates_and_closes_handler(
    logger: SSHLogger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The logging adapter must not swallow filesystem failures, and its
    transient descriptor must close on the exceptional path."""
    from agentworks import ssh

    captured: list[tuple[Any, Any]] = []

    def failing_emit(handler: object, record: object) -> None:
        captured.append((handler, handler.stream))  # type: ignore[attr-defined]
        try:
            raise OSError("disk full")
        except OSError:
            handler.handleError(record)  # type: ignore[attr-defined]

    monkeypatch.setattr(ssh._PropagatingFileHandler, "emit", failing_emit)

    with pytest.raises(OSError, match="disk full"):
        logger.output("payload")

    [(handler, stream)] = captured
    assert handler.stream is None
    assert stream.closed is True


def test_write_sink_sanitizes_every_persistent_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sink is the single sanitizing choke point: a registered
    redaction never reaches the file even when a caller hands raw,
    unsanitized text to any surface, including ``step`` and ``_write``
    itself (no public method pre-sanitizes; correctness must not
    depend on caller discipline)."""
    secret = "tskey-auth-supersecret-12345"
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    logger = SSHLogger("vm1", "test-op", redactions=(secret,))

    logger.step(f"joining with {secret}")
    logger.output(f"raw output {secret}")
    logger.warning(f"warn {secret}")
    logger.log_error(f"error {secret}")
    logger.log_timeout(f"tailscale up --auth-key {secret}", 1, 2)
    logger.log_command(
        f"tailscale up --auth-key {secret}",
        SSHResult(returncode=1, stdout=f"out {secret}", stderr=f"err {secret}"),
    )
    logger._write(f"caller bypassed every public surface: {secret}\n")
    logger.close()

    text = logger.path.read_text()
    assert secret not in text
    assert text.count("[REDACTED]") >= 8


def test_warning_text_is_sanitized_before_durable_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Durable warnings retain their diagnosis without leaking known secrets."""
    secret = "ghp-supersecret-token"
    warning = f"credential rejected: {secret}"
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    logger = SSHLogger("vm1", "test-op", redactions=(secret,))
    logger.warning(warning)

    initial_text = logger.path.read_text()
    assert warning not in initial_text
    assert secret not in initial_text
    assert "WARNING: credential rejected: [REDACTED]" in initial_text
    assert logger.warnings == [warning]

    logger.close()

    text = logger.path.read_text()
    assert warning not in text
    assert secret not in text
    assert "WARNING: credential rejected: [REDACTED]" in text
    assert "# Warnings: 1" in text
    assert logger.warnings == [warning]


def test_redactions_cannot_be_registered_after_writes(logger: SSHLogger) -> None:
    """Incremental logs persist their header during construction, so accepting
    later redactions would make the no-cleartext guarantee timing-dependent."""
    assert not hasattr(logger, "add_redaction")


def test_shell_quoted_secret_is_redacted_from_command(logger: SSHLogger) -> None:
    """Shell quoting rewrites embedded single quotes, so the transformed
    representation must be registered alongside the raw secret."""
    import shlex

    secret = "password-with-'quote"
    logger = SSHLogger("vm1", "test-op", redactions=(secret,))
    quoted = shlex.quote(secret)

    logger.log_command(
        f"example --password {quoted}",
        SSHResult(returncode=1, stdout="", stderr=""),
    )
    logger.close()

    text = logger.path.read_text()
    assert secret not in text
    assert quoted not in text
    assert "example --password [REDACTED]" in text


def test_close_redacts_secrets_from_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Secrets supplied at construction must not leak into the appended
    traceback (the operator's Tailscale auth key or git PAT
    might appear in a ``str(exc)`` payload, which would otherwise hit
    the per-op log as plaintext)."""
    secret = "tskey-auth-supersecret-12345"
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    logger = SSHLogger("vm1", "test-op", redactions=(secret,))
    try:
        raise RuntimeError(f"tailscale up failed with key={secret}")
    except RuntimeError:
        logger.close()

    text = logger.path.read_text()
    assert secret not in text
    assert "[REDACTED]" in text


# -- The record of an agentworks.execution request -------------------------------------


def test_record_redacts_a_registered_secret_in_the_argv_the_streams_and_the_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shlex

    from agentworks.execution.requests import Command
    from agentworks.execution.results import Completed, Result

    secret = "tskey-auth with space"
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    logger = SSHLogger("vm1", "test-op", redactions=(secret,))

    logger.record(
        Command(("tailscale", "up", f"--auth-key={secret}")),
        Result(Completed(1), f"out {secret}".encode(), shlex.quote(secret).encode(), f"ssh: {secret}"),
    )

    text = logger.path.read_text()
    assert secret not in text
    assert shlex.quote(secret) not in text
    assert text.count("[REDACTED]") == 4


def test_record_redacts_a_secret_ending_in_line_breaks_at_the_end_of_a_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.execution.requests import Command
    from agentworks.execution.results import Completed, Result

    secret = "first-line-4c\nsecond-line-4c\n\n"
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    logger = SSHLogger("vm1", "test-op", redactions=(secret,))

    # The secret is the last of stdout, as `printf %s "$TOKEN"` leaves it, and stderr follows it in the record.
    logger.record(Command(("printenv", "TOKEN")), Result(Completed(1), f"TOKEN={secret}".encode(), b"warning\n"))

    text = logger.path.read_text()
    assert "first-line-4c" not in text
    assert "second-line-4c" not in text


@pytest.mark.parametrize(
    ("secret", "shown"),
    [
        # A message that quotes the value, or a command substitution, drops its line breaks.
        pytest.param("tok-abc-123\n", "error: token tok-abc-123 rejected", id="trailing-newline"),
        pytest.param("\r\ntok-abc-123\r\n", "token=tok-abc-123", id="surrounding-crlf"),
    ],
)
def test_record_redacts_a_secret_shown_without_its_line_breaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secret: str, shown: str
) -> None:
    from agentworks.execution.requests import Command
    from agentworks.execution.results import Completed, Result

    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    logger = SSHLogger("vm1", "test-op", redactions=(secret,))

    logger.record(Command(("tool",)), Result(Completed(1), b"", shown.encode()))

    assert "tok-abc-123" not in logger.path.read_text()


@pytest.mark.parametrize(
    "secret",
    [
        pytest.param("line-one-7e\r\n\tline-two-7e 'q' \\ \x01\x1b\x7f", id="controls-quotes-backslash"),
        pytest.param("line-one-7e caf\u00e9\nline-two-7e", id="non-ascii"),
    ],
)
def test_sanitize_redacts_bash_ansi_c_quoting_of_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secret: str
) -> None:
    """``printf %q`` and ``declare -p`` spell such a value ``$'...'``; the spelling is bash's, so bash is the oracle."""
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not installed")
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    logger = SSHLogger("vm1", "test-op", redactions=(secret,))
    script = 'printf "%q\\n" "$1"; declare -p X'
    shown = subprocess.run(
        [bash, "-c", script, "bash", secret],
        env={"LC_ALL": "C", "X": secret},
        capture_output=True,
        check=True,
    ).stdout.decode("utf-8", "surrogateescape")

    sanitized = logger.sanitize(shown)

    assert "line-one-7e" not in sanitized
    assert "line-two-7e" not in sanitized


def test_record_labels_a_request_with_the_latest_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentworks.execution.requests import Command
    from agentworks.execution.results import Completed, Result

    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    logger = SSHLogger("vm1", "test-op")

    logger.step("first-step")
    logger.step("second-step")
    logger.record(Command(("true",)), Result(Completed(0)))

    (record_line,) = [line for line in logger.path.read_text().splitlines() if "request (" in line]
    assert "second-step" in record_line
    assert "first-step" not in record_line


def test_a_failed_request_is_recorded_before_its_error_is_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentworks.execution.access import Admin, VMAccess
    from agentworks.execution.errors import ExecutionIncomplete
    from agentworks.execution.requests import Command
    from agentworks.execution.results import Result, Unknown, UnknownReason

    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    logger = SSHLogger("vm1", "test-op")
    access = VMAccess(
        vm_name="vm1",
        host="100.64.0.9",
        user="agw",
        identity=Admin(),
        identity_file=tmp_path / "key",
        log=logger.record,
    )
    monkeypatch.setattr(VMAccess, "_execute", lambda self, *a, **k: Result(Unknown(UnknownReason.DEADLINE)))
    monkeypatch.setattr(VMAccess, "passphrase_hint", lambda self: None)

    with pytest.raises(ExecutionIncomplete):
        access.run(Command(("true",)), check=True)

    assert [line for line in logger.path.read_text().splitlines() if "request (" in line] != []


def test_an_operation_log_given_a_path_writes_all_of_it_there(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Backup keeps its log beside the archive: the header, every record and the footer, none under the log dir."""
    from agentworks.vms.manager import operation_log

    log_dir, destination = tmp_path / "logs", tmp_path / "backup" / "backup.log"
    monkeypatch.setattr("agentworks.ssh.LOG_DIR", log_dir)

    with operation_log("vm1", "vm-backup", path=destination) as log:
        log.step("Archive")

    assert destination.stat().st_size > 0
    assert not log_dir.exists() or list(log_dir.iterdir()) == []


def test_an_operation_log_shared_by_concurrent_threads_keeps_every_record_and_closes_every_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop-all's concurrent teardown plans share one log per VM: every record lands once, no write leaks its file."""
    import gc
    import sys
    import threading
    import warnings

    from agentworks.execution.requests import Command
    from agentworks.execution.results import Completed, Result
    from agentworks.vms.manager import operation_log

    monkeypatch.setattr("agentworks.ssh.LOG_DIR", tmp_path)
    threads, writes = 8, 200
    start = threading.Barrier(threads)
    previous = sys.getswitchinterval()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        # Switch threads as often as the interpreter allows, so unguarded writes would interleave.
        sys.setswitchinterval(1e-6)
        try:
            with operation_log("vm1", "session-stop-all") as log:

                def write(worker: int) -> None:
                    start.wait()
                    for index in range(writes):
                        log.record(Command(("probe", f"w{worker}-{index}")), Result(Completed(0)))

                workers = [threading.Thread(target=write, args=(number,)) for number in range(threads)]
                for thread in workers:
                    thread.start()
                for thread in workers:
                    thread.join()
        finally:
            sys.setswitchinterval(previous)
        gc.collect()

    # Only this test's own files count: from Python 3.13 an unclosed sqlite connection another test
    # leaked warns too, whenever the garbage collector happens to reach it during this body.
    leaked = [
        warning
        for warning in caught
        if issubclass(warning.category, ResourceWarning) and str(tmp_path) in str(warning.message)
    ]
    assert leaked == []
    text = Path(log.display_path).expanduser().read_text()
    for worker in range(threads):
        for index in range(writes):
            assert text.count(f"argv probe w{worker}-{index},") == 1
