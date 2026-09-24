"""Local safety and classification checks for the native WSL UNC experiment."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from tests.execution import wsl_unc_hold_probe as probe


def _args() -> list[str]:
    return [
        "--target",
        "AgwScratch",
        "--confirm-target",
        "AgwScratch",
        "--idle-seconds",
        "1",
        "--budget-seconds",
        "300",
    ]


def _settings() -> probe.Settings:
    return probe.Settings.from_args(probe._parser().parse_args(_args()))


def test_validation_precedes_effects_and_derives_timing() -> None:
    settings = _settings()
    assert settings.target == "AgwScratch"
    assert settings.hold_seconds == 3 * settings.idle_seconds
    assert settings.open_seconds > 0
    assert settings.release_seconds > 0
    args = probe._parser().parse_args(_args())
    args.confirm_target = "Other"
    with pytest.raises(ValueError):
        probe.Settings.from_args(args)
    args.target = "AgwScratch\\Other"
    args.confirm_target = args.target
    with pytest.raises(ValueError):
        probe.Settings.from_args(args)
    args.target = "AgwScratch"
    args.confirm_target = args.target
    args.unrelated = "agwscratch"
    with pytest.raises(ValueError):
        probe.Settings.from_args(args)


def test_holder_derives_only_fixed_read_only_guest_path() -> None:
    assert probe._unc_path("wsl$", "AgwScratch") == r"\\wsl$\AgwScratch\etc\os-release"
    assert probe._unc_path("localhost", "AgwScratch") == r"\\wsl.localhost\AgwScratch\etc\os-release"
    with pytest.raises(ValueError):
        probe._unc_path("other", "AgwScratch")
    with pytest.raises(ValueError):
        probe._unc_path("wsl$", "AgwScratch\\Other")


def test_wsl_list_decodes_both_native_encodings() -> None:
    expected = {"AgwScratch", "Other"}
    assert probe._decode_wsl(b"AgwScratch\r\nOther\r\n") == expected
    assert probe._decode_wsl("\ufeffAgwScratch\r\nOther\r\n".encode("utf-16-le")) == expected


def test_off_native_windows_refuses_before_discovery_or_child_effects(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda _: pytest.fail("discovery ran"))
    assert probe.main(_args()) == 2
    assert json.loads(capsys.readouterr().out)["state"] == "UNKNOWN"


def test_observe_refuses_short_window_without_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = probe.WslProbe(_settings(), "wsl.exe")
    runner.deadline = time.monotonic() + 1
    runner.cleanup_reserve = 0
    monkeypatch.setattr(runner, "running", lambda _: pytest.fail("truncated observation polled"))
    observation = runner.observe("AgwScratch", True, 5, throughout=True)
    assert observation == probe.Observation(None, 0.0)


def test_full_hold_window_reports_measured_elapsed(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = probe.WslProbe(_settings(), "wsl.exe")
    monkeypatch.setattr(runner, "running", lambda _: True)
    observation = runner.observe("AgwScratch", True, 0.02, throughout=True)
    assert observation.matched is True
    assert observation.elapsed_seconds >= 0.02


@pytest.mark.parametrize(
    ("share", "opened", "stayed_stopped", "expected"),
    [
        ("wsl$", False, True, "PASS"),
        ("wsl$", True, False, "FAIL"),
        ("wsl$", None, None, "UNKNOWN"),
        ("localhost", True, False, "PASS"),
    ],
)
def test_cold_alias_records_guard_and_contrast(
    share: str, opened: bool | None, stayed_stopped: bool | None, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = SimpleNamespace(
        settings=SimpleNamespace(target="AgwScratch", open_seconds=5.0, release_seconds=10.0),
        require_stopped=lambda: None,
        new_holder=lambda _: SimpleNamespace(opened=lambda _: opened, close=lambda _: None),
        remaining=lambda seconds: seconds,
        observe=lambda *args, **kwargs: probe.Observation(stayed_stopped, 10.0),
    )
    assert probe._cold(fake, share) == expected  # type: ignore[arg-type]
    record = json.loads(capsys.readouterr().out)
    assert record["state"] == expected
    assert record["observed_seconds"] == 10.0


def _fake_holder(*, settle: bool) -> tuple[probe.Holder, dict[str, int | bool], Mock, Mock]:
    state: dict[str, int | bool] = {"killed": False, "waits": 0}
    stdin, stdout, reader = Mock(), Mock(), Mock()
    reader.is_alive.return_value = False

    def poll() -> int | None:
        return 1 if state["killed"] and settle else None

    def wait(*, timeout: float) -> int:
        state["waits"] = int(state["waits"]) + 1
        if state["waits"] == 1:
            raise KeyboardInterrupt
        if not settle:
            raise subprocess.TimeoutExpired("holder", timeout)
        return 1

    def kill() -> None:
        state["killed"] = True

    process = SimpleNamespace(stdin=stdin, stdout=stdout, poll=poll, wait=wait, kill=kill)
    holder = object.__new__(probe.Holder)
    holder.process = cast(Any, process)
    holder.reader = cast(Any, reader)
    return holder, state, stdout, reader


def test_interrupted_close_reaps_before_closing_reader_pipe() -> None:
    holder, state, stdout, reader = _fake_holder(settle=True)
    with pytest.raises(KeyboardInterrupt):
        holder.close(0.1)
    assert state["killed"]
    reader.join.assert_called_once()
    stdout.close.assert_called_once()


def test_failed_reap_never_closes_live_reader_pipe() -> None:
    holder, state, stdout, _ = _fake_holder(settle=False)
    with pytest.raises(probe.ProbeError):
        holder.close(0.01)
    assert state["killed"]
    stdout.close.assert_not_called()


@pytest.mark.parametrize(
    ("failure", "termination_status"),
    [(probe.ProbeError, 0), (KeyboardInterrupt, 1)],
)
def test_cleanup_continues_after_holder_failure(
    monkeypatch: pytest.MonkeyPatch, failure: type[BaseException], termination_status: int
) -> None:
    runner = probe.WslProbe(_settings(), "wsl.exe")

    class FailedHolder:
        def close(self, _: float) -> None:
            raise failure()

    runner.holders.append(FailedHolder())  # type: ignore[arg-type]
    commands: list[list[str]] = []

    def fake_run(argv: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        commands.append(argv)
        return subprocess.CompletedProcess(argv, termination_status if "--terminate" in argv else 0, stdout=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    stopped, reaped, unrelated, interrupted, error = runner.cleanup()
    assert stopped and not reaped and unrelated is None
    assert isinstance(interrupted, KeyboardInterrupt) is (failure is KeyboardInterrupt)
    assert (error is not None) is (termination_status != 0)
    assert commands == [
        ["wsl.exe", "--terminate", "AgwScratch"],
        ["wsl.exe", "--list", "--running", "--quiet"],
    ]


def test_confirmation_precedes_any_wsl_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", lambda _: pytest.fail("WSL discovery ran"))
    args = _args()
    args[3] = "Wrong"
    assert probe.main(args) == 2
    assert json.loads(capsys.readouterr().out)["state"] == "UNKNOWN"


def test_running_target_is_not_terminated_by_refused_preflight(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class AlreadyRunning:
        def __init__(self, *_: Any) -> None:
            pass

        def listed(self, *, running: bool) -> set[str]:
            assert not running
            return {"AgwScratch"}

        def require_stopped(self) -> None:
            raise probe.ProbeError("running")

        def cleanup(self) -> None:
            pytest.fail("preexisting target was terminated")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", lambda _: "C:\\Windows\\System32\\wsl.exe")
    monkeypatch.setattr(probe, "WslProbe", AlreadyRunning)
    assert probe.main(_args()) == 2
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[0]["state"] == "UNKNOWN"
    assert records[-1]["case"] == "conclusion"


class _MainProbe:
    def __init__(self, settings: probe.Settings, _: str) -> None:
        self.settings = settings
        self.unrelated_before_force = None
        self.unrelated_after_force = None

    def listed(self, *, running: bool) -> set[str]:
        assert not running
        return {self.settings.target}

    def require_stopped(self) -> None:
        pass

    def reset(self) -> None:
        pass

    def cleanup(self) -> tuple[bool, bool, None, KeyboardInterrupt | None, str | None]:
        return True, True, None, None, None


def test_retention_window_uses_measured_eight_second_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    selected: list[float] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", lambda _: "wsl.exe")
    monkeypatch.setattr(probe, "WslProbe", _MainProbe)
    monkeypatch.setattr(probe, "_cold", lambda *_: "PASS")
    monkeypatch.setattr(probe, "_baseline", lambda _: probe.Observation(True, 8.0))
    monkeypatch.setattr(probe, "_retention", lambda _, *, two, seconds: selected.append(seconds))
    monkeypatch.setattr(probe, "_abrupt", lambda _: None)
    monkeypatch.setattr(probe, "_forced", lambda _: None)
    assert probe.main(_args()) == 0
    assert selected == [24.0, 24.0]
    assert all(seconds > 8.0 for seconds in selected)


def test_main_emits_cleanup_evidence_before_propagating_interrupt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class InterruptedProbe(_MainProbe):
        def cleanup(self) -> tuple[bool, bool, None, KeyboardInterrupt, str]:
            return True, False, None, KeyboardInterrupt(), "target termination returned 1"

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", lambda _: "wsl.exe")
    monkeypatch.setattr(probe, "WslProbe", InterruptedProbe)
    monkeypatch.setattr(probe, "_cold", lambda *_: "FAIL")
    with pytest.raises(KeyboardInterrupt):
        probe.main(_args())
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[-2]["case"] == "cleanup" and records[-2]["interrupted"] is True
    assert records[-2]["state"] == "UNKNOWN"
    assert records[-1]["case"] == "conclusion"
