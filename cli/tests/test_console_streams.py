"""Tests for the entrypoint's stdout/stderr hardening (``_reconfigure_std_streams``).

The failing environment is a Windows console whose stdout is a legacy-codepage
text stream (observed cp1252) with CRLF newline translation. The suite runs on
POSIX, so these tests rebuild that stream shape directly: an
``io.TextIOWrapper`` over ``BytesIO`` with ``encoding="cp1252"``,
``errors="strict"``, and ``newline="\\r\\n"`` reproduces both field failures
(``UnicodeEncodeError`` on U+26A0, and CRLF riding into consumers that iterate
machine-readable output).
"""

from __future__ import annotations

import io
import sys

import pytest
import typer

from agentworks.cli._entry import _reconfigure_std_streams

# Release-please's generated breaking-changes heading marker, the field
# report's offender: outside cp1252, so a strict legacy-codepage stream
# cannot encode it.
BREAKING_MARKER = "⚠"


def _cp1252_console() -> tuple[io.BytesIO, io.TextIOWrapper]:
    """A Windows-style legacy console stream: cp1252, strict, CRLF."""
    raw = io.BytesIO()
    return raw, io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="\r\n")


def test_simulated_console_reproduces_the_field_crash() -> None:
    """Sanity check on the simulation itself: without the entrypoint fix,
    writing the marker raises the exact error class from the field report."""
    _, stream = _cp1252_console()
    with pytest.raises(UnicodeEncodeError):
        stream.write(BREAKING_MARKER)


def test_stdout_degrades_unencodable_characters_and_emits_bare_lf(monkeypatch: pytest.MonkeyPatch) -> None:
    """After reconfigure, stdout keeps its legacy encoding (no forced UTF-8
    mojibake), degrades unencodable characters to a replacement instead of
    crashing, and writes bare-LF newlines for machine-readable output."""
    raw, stream = _cp1252_console()
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", _cp1252_console()[1])

    _reconfigure_std_streams()

    sys.stdout.write(f"{BREAKING_MARKER} BREAKING CHANGES\n")
    sys.stdout.flush()
    assert raw.getvalue() == b"? BREAKING CHANGES\n"
    assert sys.stdout.encoding == "cp1252"


def test_stderr_degrades_unencodable_characters_but_keeps_platform_newlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stderr gets the same crash-proofing; its newline translation is left
    alone because it is human-facing only (a deliberate asymmetry with
    stdout, which programs iterate)."""
    raw, stream = _cp1252_console()
    monkeypatch.setattr(sys, "stdout", _cp1252_console()[1])
    monkeypatch.setattr(sys, "stderr", stream)

    _reconfigure_std_streams()

    sys.stderr.write(f"{BREAKING_MARKER} warning\n")
    sys.stderr.flush()
    assert raw.getvalue() == b"? warning\r\n"


def test_non_reconfigurable_streams_are_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests and embedders replace the std streams with StringIO-like objects
    without ``reconfigure()``; the entrypoint must leave those alone."""
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    _reconfigure_std_streams()

    sys.stdout.write("ok\n")
    assert out.getvalue() == "ok\n"


def test_main_covers_the_typer_echo_path_on_a_legacy_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive ``main()`` end to end with a command that echoes the marker.

    This proves the reconfigure reaches the stream ``typer.echo`` actually
    resolves (typer's vendored click caches its own stdout lookup), i.e.
    that the ``guide show`` crash path is covered, not just the helper."""
    from agentworks import cli as cli_mod

    test_app = typer.Typer()

    @test_app.callback()
    def _cb() -> None:
        pass

    @test_app.command("emit")
    def emit() -> None:
        typer.echo(f"{BREAKING_MARKER} BREAKING CHANGES")

    raw_out, out = _cp1252_console()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", _cp1252_console()[1])
    monkeypatch.setattr(cli_mod, "app", test_app)
    monkeypatch.setattr(sys, "argv", ["agentworks", "emit"])
    monkeypatch.setenv("AGW_DEBUG", "")

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main()
    out.flush()

    assert exc_info.value.code == 0
    assert raw_out.getvalue() == b"? BREAKING CHANGES\n"


def test_guide_list_emits_bare_lf_on_a_legacy_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """The report's second finding: iterating ``agw guide list`` in Git Bash
    rode Windows CRLF into the topic arguments. Through the real entrypoint
    the listing must be one LF-terminated name per line, no carriage
    returns, on a Windows-style stream."""
    from agentworks import cli as cli_mod

    raw_out, out = _cp1252_console()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "argv", ["agentworks", "guide", "list"])
    monkeypatch.setenv("AGW_DEBUG", "")

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main()
    out.flush()
    payload = raw_out.getvalue()

    assert exc_info.value.code == 0
    assert payload
    assert b"\r" not in payload
    assert payload.endswith(b"\n")
