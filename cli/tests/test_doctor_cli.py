"""Tests for `agw doctor`'s CLI-level rendering: status-label and summary-line
colorization (STATUS role, realized via `output.style_status`).

`agentworks/doctor.py` (the service layer) returns a `HealthReport` of bare
`Status` values and does no rendering; these tests exercise the renderer in
`cli/commands/doctor.py`, which maps `Status` to `StatusStyle` and colors the
`[ok]`/`[info]`/`[warn]`/`[FAIL]` labels and the summary counts. See
`tests/test_typer_output.py` for the lower-level `style_status` unit tests
this renderer builds on.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator

import click
import pytest
import typer

from agentworks import output
from agentworks.cli._typer_output import TyperHandler
from agentworks.cli.commands.doctor import doctor
from agentworks.doctor import HealthGroup, HealthReport

# Mirrors tests/test_typer_output.py's _plain / _ANSI_RE.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make stdout report as a color-allowed terminal. Mirrors the sibling
    helper in test_typer_output.py: clearing NO_COLOR keeps the on-TTY
    tests hermetic (they must pass even when a dev/CI has NO_COLOR set)."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)


def _fake_report() -> HealthReport:
    """One group carrying one check per status, so a single render
    exercises every label/color pairing plus a non-zero summary line."""
    report = HealthReport()
    g = HealthGroup("Sample")
    g.ok("thing-ok", "fine")
    g.info("thing-info", "fyi")
    g.warn("thing-warn", "careful")
    g.fail("thing-fail", "broken")
    report.groups.append(g)
    return report


@pytest.fixture
def _stub_run_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the service-layer call so the renderer sees a fixed, synthetic
    report instead of probing the real environment/filesystem/DB."""
    monkeypatch.setattr("agentworks.doctor.run_checks", lambda **kwargs: _fake_report())


@pytest.fixture
def _typer_handler() -> Iterator[None]:
    """Install a real TyperHandler for the duration of the test (doctor's
    renderer calls the module-level `style_status`, which reaches whatever
    handler is currently installed). Restores whatever was installed
    before, mirroring the `captured_output` fixture in conftest.py."""
    previous = output.get_handler()
    output.set_handler(TyperHandler())
    yield
    output.set_handler(previous)


def _run_doctor(capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(typer.Exit) as exc_info:
        doctor()
    assert exc_info.value.exit_code == 1  # the fake report has one FAIL
    return capsys.readouterr().out


@pytest.mark.usefixtures("_stub_run_checks", "_typer_handler")
class TestDoctorColorOnATty:
    def test_ok_label_is_green(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        _tty(monkeypatch)
        out = _run_doctor(capsys)
        assert f"  {click.style('[ok]  ', fg='green')} thing-ok: fine\n" in out

    def test_warn_label_is_yellow(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        _tty(monkeypatch)
        out = _run_doctor(capsys)
        assert f"  {click.style('[warn]', fg='yellow')} thing-warn: careful\n" in out

    def test_fail_label_is_red(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        _tty(monkeypatch)
        out = _run_doctor(capsys)
        assert f"  {click.style('[FAIL]', fg='red')} thing-fail: broken\n" in out

    def test_info_label_is_unstyled(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        _tty(monkeypatch)
        out = _run_doctor(capsys)
        assert "  [info] thing-info: fyi\n" in out

    def test_label_column_width_unaffected_by_ansi(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # style_status is applied AFTER .ljust(6): stripping ANSI must
        # recover exactly the plain-text column alignment.
        _tty(monkeypatch)
        out = _run_doctor(capsys)
        assert "  [ok]   thing-ok: fine\n" in _plain(out)
        assert "  [warn] thing-warn: careful\n" in _plain(out)
        assert "  [FAIL] thing-fail: broken\n" in _plain(out)

    def test_summary_counts_are_colored(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _tty(monkeypatch)
        out = _run_doctor(capsys)
        expected = (
            f"Results: {click.style('1', fg='green')} ok, 1 info, "
            f"{click.style('1', fg='yellow')} warn, {click.style('1', fg='red')} fail\n"
        )
        assert expected in out
        assert "Results: 1 ok, 1 info, 1 warn, 1 fail\n" in _plain(out)


@pytest.mark.usefixtures("_stub_run_checks", "_typer_handler")
class TestDoctorPlainFallback:
    """No ANSI leaks into piped/non-interactive doctor output."""

    def test_non_tty_stdout_is_byte_plain(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        out = _run_doctor(capsys)
        assert _ANSI_RE.search(out) is None
        assert "  [ok]   thing-ok: fine\n" in out
        assert "Results: 1 ok, 1 info, 1 warn, 1 fail\n" in out

    def test_no_color_env_forces_byte_plain_even_on_a_tty(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        monkeypatch.setenv("NO_COLOR", "")
        out = _run_doctor(capsys)
        assert _ANSI_RE.search(out) is None

    def test_non_interactive_forces_byte_plain_even_on_a_tty(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        monkeypatch.delenv("NO_COLOR", raising=False)
        output.set_non_interactive(True)
        try:
            out = _run_doctor(capsys)
        finally:
            output.set_non_interactive(False)
        assert _ANSI_RE.search(out) is None
