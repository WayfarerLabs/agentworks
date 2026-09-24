"""Local safety and classification checks for the native WSL UNC experiment."""

from __future__ import annotations

import json
import shutil
import sys
import time
from types import SimpleNamespace
from typing import Any

import pytest

from tests.execution import wsl_unc_hold_probe as probe


def _args(*extra: str) -> list[str]:
    return [
        "--target",
        "AgwScratch",
        "--confirm-target",
        "AgwScratch",
        "--idle-seconds",
        "1",
        "--hold-seconds",
        "3",
        "--poll-seconds",
        "0.1",
        "--open-seconds",
        "2",
        "--release-seconds",
        "2",
        "--late-seconds",
        "2",
        "--race-kill-seconds",
        "0.1",
        "--race-attempts",
        "1",
        "--budget-seconds",
        "30",
        *extra,
    ]


def test_validation_rejects_unconfirmed_and_unsafe_target_names() -> None:
    args = probe._parser().parse_args(_args())
    assert probe.Settings.from_args(args).target == "AgwScratch"
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hold_seconds", 2.9),
        ("poll_seconds", 3),
        ("race_kill_seconds", 2),
        ("budget_seconds", 29),
        ("idle_seconds", float("nan")),
    ],
)
def test_timing_validation_closes_invalid_budgets(field: str, value: float) -> None:
    args = probe._parser().parse_args(_args())
    setattr(args, field, value)
    with pytest.raises(ValueError):
        probe.Settings.from_args(args)


def test_only_fixed_read_only_guest_path_is_accepted() -> None:
    for share in ("wsl$", "localhost"):
        path = probe._unc_path(share, "AgwScratch")
        assert probe._HOLDER_PATH.fullmatch(path)
        assert path.endswith("\\etc\\os-release")
    with pytest.raises(ValueError):
        probe._unc_path("other", "AgwScratch")
    assert probe._HOLDER_PATH.fullmatch(r"\\wsl$\AgwScratch\etc\passwd") is None


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


def test_invalid_cli_options_remain_machine_readable() -> None:
    with pytest.raises(ValueError):
        probe._parser().parse_args(["--target", "AgwScratch"])


class _FakeHolder:
    def __init__(self, *, returned: bool = False) -> None:
        self.returned = returned
        self.process = SimpleNamespace(poll=lambda: None)
        self.killed = False

    def next(self, _: float) -> dict[str, str] | None:
        if self.killed:
            return None
        if not hasattr(self, "_called"):
            self._called = True
            return {"event": "calling"}
        return {"event": "opened"} if self.returned else None

    def kill(self) -> None:
        self.killed = True


class _FakeProbe:
    def __init__(self, *, running_after_kill: bool, late: bool | None) -> None:
        self.settings = SimpleNamespace(target="AgwScratch", open_seconds=1.0, race_kill_seconds=0.1, late_seconds=1.0)
        self.holder = _FakeHolder()
        self.running_after_kill = running_after_kill
        self.late = late

    def require_stopped(self) -> None:
        return None

    def new_holder(self, _: str) -> _FakeHolder:
        return self.holder

    def remaining(self, seconds: float) -> float:
        return seconds

    def running(self, _: str) -> bool:
        assert self.holder.killed
        return self.running_after_kill

    def observe(self, *_: Any) -> bool | None:
        return self.late


@pytest.mark.parametrize(
    ("running_after_kill", "late", "expected"),
    [(False, True, "FAIL"), (False, False, "UNKNOWN"), (True, True, "UNKNOWN"), (False, None, "UNKNOWN")],
)
def test_race_only_falsifies_on_sampled_late_start(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    running_after_kill: bool,
    late: bool | None,
    expected: str,
) -> None:
    monkeypatch.setattr(time, "sleep", lambda _: None)
    fake = _FakeProbe(running_after_kill=running_after_kill, late=late)
    probe._race(fake, "wsl$", 0)  # type: ignore[arg-type]
    record = json.loads(capsys.readouterr().out)
    assert record["state"] == expected
    assert record["strict_dispatch_drain"] == expected
    assert fake.holder.killed


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
    assert json.loads(capsys.readouterr().out)["state"] == "UNKNOWN"
