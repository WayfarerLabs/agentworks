"""Phase B's step warnings: what a failure shows, and what it never shows."""

from __future__ import annotations

from agentworks.execution.errors import CommandFailed, PathRefusal, PathRefused
from agentworks.execution.results import Completed, Result
from agentworks.vms.initializer.steps import describe_failure, warn_step

SECRET = "first-half-of-the-token\nsecond-half-of-the-token"


def _redact(text: str) -> str:
    return text.replace(SECRET, "[REDACTED]")


def _failed(stderr: bytes, *, streams_logged: bool = True) -> CommandFailed:
    return CommandFailed("box", 1, Result(Completed(1), b"", stderr), streams_logged=streams_logged)


def test_a_secret_spanning_lines_is_removed_before_the_tail_is_cut() -> None:
    # The tail's three lines start inside the secret, so cutting first would show its second half.
    stderr = f"earlier\nprefix {SECRET} suffix\nnext\nlast\n".encode()

    described = describe_failure(_failed(stderr), _redact)

    assert "second-half-of-the-token" not in described
    assert "first-half-of-the-token" not in described
    assert described.splitlines()[1:] == ["prefix [REDACTED] suffix", "next", "last"]


def test_a_control_character_in_stderr_is_neutralized() -> None:
    described = describe_failure(_failed(b"apt: \x1b]0;title\x07\x1b[31mfailed\n"), _redact)

    assert "\x1b" not in described and "\x07" not in described


def test_a_request_that_kept_its_streams_out_of_the_log_shows_none() -> None:
    described = describe_failure(_failed(b"file content\n", streams_logged=False), _redact)

    assert "file content" not in described
    assert described == str(_failed(b"", streams_logged=False))


def test_an_error_with_no_streams_is_its_message() -> None:
    refused = PathRefused("box", "write", "/etc/x", PathRefusal.UNSUITABLE)

    assert describe_failure(refused, _redact) == str(refused)


class _Log:
    def __init__(self) -> None:
        self.warned: list[str] = []

    def warning(self, msg: str) -> None:
        self.warned.append(msg)

    def sanitize(self, text: str) -> str:
        return _redact(text)


def test_a_step_warning_reaches_the_log_redacted() -> None:
    log = _Log()

    warn_step(log, "sysctl baseline failed", _failed(f"x {SECRET} y\n".encode()))  # type: ignore[arg-type]

    assert len(log.warned) == 1
    assert log.warned[0].startswith("sysctl baseline failed: ")
    assert "second-half-of-the-token" not in log.warned[0]
