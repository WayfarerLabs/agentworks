#!/usr/bin/env python3
"""Native Windows experiment for WSL UNC handles as platform holds.

Run only against a disposable distro that is stopped before the probe starts.
This records bounded observations; it cannot prove that a late WSL launch is
impossible after a killed caller.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Never

_DISTRO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_HOLDER_PATH = re.compile(r"\\\\(?:wsl\$|wsl\.localhost)\\[A-Za-z0-9][A-Za-z0-9._-]{0,79}\\etc\\os-release\Z")
_SHARES = {"wsl$": "wsl$", "localhost": "wsl.localhost"}
_MAX_RECORD = 4096
_INVALID_HANDLE = ctypes.c_void_p(-1).value
State = Literal["PASS", "FAIL", "UNKNOWN"]


def _distro(value: str) -> str:
    """Validate operator input before it reaches a Windows path or WSL argv."""
    if _DISTRO.fullmatch(value) is None or value in {".", ".."}:
        raise ValueError("distro names must be 1-80 safe ASCII characters")
    return value


def _unc_path(share: str, distro: str) -> str:
    if share not in _SHARES:
        raise ValueError("unsupported WSL share")
    return "\\\\" + _SHARES[share] + "\\" + _distro(distro) + "\\etc\\os-release"


def _positive(value: float, name: str) -> float:
    if not 0 < value <= 3600:
        raise ValueError(f"{name} must be in (0, 3600] seconds")
    return value


@dataclass(frozen=True)
class Settings:
    target: str
    unrelated: str | None
    idle_seconds: float
    hold_seconds: float
    poll_seconds: float
    open_seconds: float
    release_seconds: float
    late_seconds: float
    race_kill_seconds: float
    race_attempts: int
    budget_seconds: float

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> Settings:
        target = _distro(args.target)
        if args.confirm_target != target:
            raise ValueError("--confirm-target must exactly match --target")
        unrelated = _distro(args.unrelated) if args.unrelated else None
        if unrelated is not None and unrelated.casefold() == target.casefold():
            raise ValueError("unrelated distro must differ from target")
        timing = {
            name: _positive(getattr(args, name), name)
            for name in (
                "idle_seconds",
                "hold_seconds",
                "poll_seconds",
                "open_seconds",
                "release_seconds",
                "late_seconds",
                "race_kill_seconds",
                "budget_seconds",
            )
        }
        if timing["hold_seconds"] < 3 * timing["idle_seconds"]:
            raise ValueError("hold duration must be at least three idle timeouts")
        if timing["poll_seconds"] > min(timing["late_seconds"], timing["release_seconds"]):
            raise ValueError("poll interval exceeds an observation window")
        if timing["race_kill_seconds"] >= timing["open_seconds"]:
            raise ValueError("race kill delay must be shorter than open timeout")
        if not 1 <= args.race_attempts <= 20:
            raise ValueError("race attempts must be in [1, 20]")
        if timing["budget_seconds"] < 30:
            raise ValueError("budget must allow at least 30 seconds including cleanup")
        return cls(target, unrelated, race_attempts=args.race_attempts, **timing)


def _emit(case: str, state: State, **evidence: object) -> None:
    record = {"case": case, "state": state, **evidence}
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded) > _MAX_RECORD:
        raise ValueError("probe record exceeds bound")
    print(encoded, flush=True)


def _decode_wsl(data: bytes) -> set[str]:
    """WSL list output may be UTF-16LE or UTF-8, depending on the host."""
    value = data.decode("utf-16-le") if b"\0" in data else data.decode("utf-8-sig")
    return {line.strip().lstrip("\ufeff") for line in value.splitlines() if line.strip()}


class ProbeError(Exception):
    pass


class BudgetExpired(ProbeError):
    pass


class _JsonParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(message)


class WslProbe:
    def __init__(self, settings: Settings, wsl: str) -> None:
        self.settings = settings
        self.wsl = wsl
        self.deadline = time.monotonic() + settings.budget_seconds
        self.cleanup_reserve = min(10.0, settings.budget_seconds / 5)
        self.holders: list[Holder] = []

    def remaining(self, requested: float) -> float:
        left = self.deadline - self.cleanup_reserve - time.monotonic()
        if left <= 0:
            raise BudgetExpired("observation budget exhausted")
        return min(requested, left)

    def _wsl(self, *argv: str, timeout: float) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                [self.wsl, *argv],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self.remaining(timeout),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProbeError("WSL command timed out; remote delivery may remain pending") from exc
        except OSError as exc:
            raise ProbeError(f"WSL command could not start: {type(exc).__name__}") from exc

    def listed(self, *, running: bool) -> set[str]:
        argv = ("--list", "--running", "--quiet") if running else ("--list", "--quiet")
        result = self._wsl(*argv, timeout=self.settings.open_seconds)
        if result.returncode != 0:
            raise ProbeError(f"WSL list failed with status {result.returncode}")
        try:
            return _decode_wsl(result.stdout)
        except UnicodeError as exc:
            raise ProbeError("WSL list encoding could not be decoded") from exc

    def running(self, distro: str) -> bool:
        return distro in self.listed(running=True)

    def require_stopped(self) -> None:
        if self.running(self.settings.target):
            raise ProbeError("target is running; only a stopped disposable distro is accepted")

    def start(self) -> None:
        result = self._wsl(
            "--distribution", self.settings.target, "--exec", "/bin/true", timeout=self.settings.open_seconds
        )
        if result.returncode != 0 or not self.running(self.settings.target):
            raise ProbeError("fixed target start failed or target did not appear running")

    def terminate(self) -> None:
        result = self._wsl("--terminate", self.settings.target, timeout=self.settings.open_seconds)
        if result.returncode != 0:
            raise ProbeError(f"target termination returned {result.returncode}")

    def reset(self) -> None:
        for holder in self.holders:
            holder.close()
        self.holders.clear()
        if self.running(self.settings.target):
            self.terminate()
        status = self.observe(self.settings.target, False, self.settings.release_seconds)
        if status is not True:
            raise ProbeError("target could not be confirmed stopped after exact termination")

    def cleanup(self) -> tuple[bool, bool]:
        """Reap holders and report the immediate post-termination target state."""
        holder_error = False
        for holder in self.holders:
            try:
                holder.close()
            except (OSError, subprocess.TimeoutExpired):
                holder_error = True
        self.holders.clear()
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise ProbeError("cleanup budget exhausted before target termination")
        try:
            subprocess.run(
                [self.wsl, "--terminate", self.settings.target],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=min(left, self.settings.open_seconds),
                check=False,
            )
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise ProbeError("cleanup budget exhausted before final observation")
            result = subprocess.run(
                [self.wsl, "--list", "--running", "--quiet"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=min(left, self.settings.open_seconds),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProbeError("exact target cleanup or observation timed out") from exc
        except OSError as exc:
            raise ProbeError(f"target cleanup command could not start: {type(exc).__name__}") from exc
        if result.returncode != 0:
            raise ProbeError(f"final target observation returned {result.returncode}")
        try:
            return self.settings.target not in _decode_wsl(result.stdout), not holder_error
        except UnicodeError as exc:
            raise ProbeError("final target observation encoding could not be decoded") from exc

    def observe(self, distro: str, expected: bool, seconds: float, *, throughout: bool = False) -> bool | None:
        end = time.monotonic() + self.remaining(seconds)
        while True:
            try:
                actual = self.running(distro)
            except ProbeError:
                return None
            if throughout and actual is not expected:
                return False
            if not throughout and actual is expected:
                return True
            left = end - time.monotonic()
            if left <= 0:
                return throughout
            time.sleep(min(self.settings.poll_seconds, left))

    def new_holder(self, share: str) -> Holder:
        holder = Holder(_unc_path(share, self.settings.target), self.settings.target)
        self.holders.append(holder)
        return holder


class Holder:
    """One independent Windows process with one read-only file handle."""

    def __init__(self, path: str, target: str) -> None:
        self.events: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()), "_hold", path, target, target],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                event = {"event": "invalid"}
            self.events.put(event)

    def next(self, timeout: float) -> dict[str, Any] | None:
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None

    def opened(self, timeout: float) -> bool | None:
        end = time.monotonic() + timeout
        first = self.next(timeout)
        if first is None or first.get("event") != "calling":
            return None
        second = self.next(max(0, end - time.monotonic()))
        if second is None:
            return None
        if second.get("event") == "opened":
            return True
        if second.get("event") == "open_failed":
            return False
        return None

    def close(self) -> None:
        try:
            if self.process.poll() is not None:
                self.process.wait()
                return
            assert self.process.stdin is not None
            self.process.stdin.write("close\n")
            self.process.stdin.flush()
            self.process.wait(timeout=2)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            self.kill()
        finally:
            if self.process.stdin is not None:
                with contextlib.suppress(OSError):
                    self.process.stdin.close()
            if self.process.stdout is not None:
                self.process.stdout.close()

    def kill(self) -> None:
        if self.process.poll() is None:
            with contextlib.suppress(OSError):
                self.process.kill()
        self.process.wait(timeout=5)


def _holder_main(path: str, target: str, confirmation: str) -> int:
    if sys.platform != "win32":
        return 2
    if target != confirmation or len(path) > 200 or _HOLDER_PATH.fullmatch(path) is None:
        return 2
    try:
        if path not in {_unc_path(share, target) for share in _SHARES}:
            return 2
    except ValueError:
        return 2
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    print('{"event":"calling"}', flush=True)
    handle = kernel.CreateFileW(path, 0x80000000, 0x00000007, None, 3, 0, None)
    if handle == _INVALID_HANDLE:
        print(json.dumps({"event": "open_failed", "winerror": ctypes.get_last_error()}), flush=True)
        return 1
    print('{"event":"opened"}', flush=True)
    try:
        sys.stdin.readline()
    finally:
        kernel.CloseHandle(handle)
    return 0


def _cold(probe: WslProbe, share: str) -> None:
    case = f"cold_{share}"
    probe.require_stopped()
    holder = probe.new_holder(share)
    opened = holder.opened(probe.remaining(probe.settings.open_seconds))
    holder.close()
    late = probe.observe(probe.settings.target, True, probe.settings.late_seconds)
    if opened is True or late is True:
        state: State = "FAIL"
    elif opened is False and late is False:
        state = "PASS"
    else:
        state = "UNKNOWN"
    _emit(case, state, opened=opened, started_within_window=late)


def _hold_and_close(probe: WslProbe) -> None:
    probe.start()
    holder = probe.new_holder("wsl$")
    opened = holder.opened(probe.remaining(probe.settings.open_seconds))
    if opened is not True:
        _emit("hold_and_close", "UNKNOWN", opened=opened)
        return
    held = probe.observe(probe.settings.target, True, probe.settings.hold_seconds, throughout=True)
    holder.close()
    released = probe.observe(probe.settings.target, False, probe.settings.release_seconds)
    state: State = "FAIL" if held is False else "PASS" if held is True and released is True else "UNKNOWN"
    _emit("hold_and_close", state, held_past_idle=held, stopped_after_close=released)


def _two_holders(probe: WslProbe) -> None:
    probe.start()
    first = probe.new_holder("wsl$")
    second = probe.new_holder("wsl$")
    opened = (
        first.opened(probe.remaining(probe.settings.open_seconds)),
        second.opened(probe.remaining(probe.settings.open_seconds)),
    )
    if opened != (True, True):
        _emit("two_holders", "UNKNOWN", opened=opened)
        return
    first.close()
    one_left = probe.observe(probe.settings.target, True, probe.settings.hold_seconds, throughout=True)
    second.close()
    released = probe.observe(probe.settings.target, False, probe.settings.release_seconds)
    state: State = "FAIL" if one_left is False else "PASS" if one_left is True and released is True else "UNKNOWN"
    _emit("two_holders", state, held_by_second=one_left, stopped_after_last_close=released)


def _abrupt(probe: WslProbe) -> None:
    probe.start()
    holder = probe.new_holder("wsl$")
    opened = holder.opened(probe.remaining(probe.settings.open_seconds))
    if opened is not True:
        _emit("abrupt_holder_death", "UNKNOWN", opened=opened)
        return
    holder.kill()
    released = probe.observe(probe.settings.target, False, probe.settings.release_seconds)
    _emit("abrupt_holder_death", "PASS" if released is True else "UNKNOWN", stopped_after_death=released)


def _forced(probe: WslProbe) -> None:
    probe.start()
    holder = probe.new_holder("wsl$")
    opened = holder.opened(probe.remaining(probe.settings.open_seconds))
    if opened is not True:
        _emit("forced_terminate", "UNKNOWN", opened=opened)
        if probe.settings.unrelated is not None:
            _emit("unrelated_survival", "UNKNOWN", forced_termination_performed=False)
        return
    unrelated_before = probe.running(probe.settings.unrelated) if probe.settings.unrelated is not None else None
    probe.terminate()
    stopped = probe.observe(probe.settings.target, False, probe.settings.release_seconds)
    _emit("forced_terminate", "PASS" if stopped is True else "FAIL" if stopped is False else "UNKNOWN", stopped=stopped)
    if probe.settings.unrelated is not None:
        try:
            after = probe.running(probe.settings.unrelated)
        except ProbeError:
            after = None
        state: State = (
            "PASS"
            if unrelated_before is True and after is True
            else "FAIL"
            if unrelated_before is True and after is False
            else "UNKNOWN"
        )
        _emit("unrelated_survival", state, running_before=unrelated_before, running_after=after)


def _race(probe: WslProbe, share: str, attempt: int) -> None:
    probe.require_stopped()
    holder = probe.new_holder(share)
    first = holder.next(probe.remaining(probe.settings.open_seconds))
    if first is None or first.get("event") != "calling":
        holder.kill()
        _emit(f"race_{share}_{attempt}", "UNKNOWN", killed_before_return=None)
        return
    time.sleep(probe.remaining(probe.settings.race_kill_seconds))
    return_observed = holder.next(0) is not None or holder.process.poll() is not None
    holder.kill()
    running_after_kill = probe.running(probe.settings.target)
    late = probe.observe(probe.settings.target, True, probe.settings.late_seconds)
    # A stopped sample after death followed by a running sample establishes a late start.
    late_start = late is True and not running_after_kill
    state: State = "FAIL" if late_start else "UNKNOWN"
    _emit(
        f"race_{share}_{attempt}",
        state,
        return_observed_before_kill=return_observed,
        running_after_kill=running_after_kill,
        started_within_window=late,
        sampled_late_start=late_start,
        strict_dispatch_drain="FAIL" if late_start else "UNKNOWN",
    )


def _parser() -> argparse.ArgumentParser:
    parser = _JsonParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--confirm-target", required=True)
    parser.add_argument("--unrelated")
    for name in ("idle", "hold", "poll", "open", "release", "late", "race-kill", "budget"):
        parser.add_argument(f"--{name}-seconds", required=True, type=float)
    parser.add_argument("--race-attempts", required=True, type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "_hold":
        return _holder_main(args[1], args[2], args[3]) if len(args) == 4 else 2
    if sys.platform != "win32":
        _emit("preflight", "UNKNOWN", reason="native Windows required")
        return 2
    try:
        settings = Settings.from_args(_parser().parse_args(args))
    except ValueError as exc:
        _emit("preflight", "UNKNOWN", reason=str(exc))
        return 2
    wsl = shutil.which("wsl.exe")
    if wsl is None:
        _emit("preflight", "UNKNOWN", reason="wsl.exe unavailable")
        return 2
    probe = WslProbe(settings, wsl)
    target_confirmed_stopped = False
    try:
        installed = probe.listed(running=False)
        if settings.target not in installed or (settings.unrelated is not None and settings.unrelated not in installed):
            raise ProbeError("target or unrelated distro is not installed")
        probe.require_stopped()
        target_confirmed_stopped = True
        unrelated_before = probe.running(settings.unrelated) if settings.unrelated else None
        _emit(
            "preflight",
            "PASS",
            target=settings.target,
            unrelated=settings.unrelated,
            unrelated_running=unrelated_before,
            idle_seconds=settings.idle_seconds,
            hold_seconds=settings.hold_seconds,
            poll_seconds=settings.poll_seconds,
            strict_dispatch_drain="UNKNOWN",
        )
        cases = [
            ("cold_wsl$", lambda: _cold(probe, "wsl$")),
            ("cold_localhost", lambda: _cold(probe, "localhost")),
            ("hold_and_close", lambda: _hold_and_close(probe)),
            ("two_holders", lambda: _two_holders(probe)),
            ("abrupt_holder_death", lambda: _abrupt(probe)),
            ("forced_terminate", lambda: _forced(probe)),
        ]
        cases.extend(
            (f"race_{share}_{attempt}", lambda share=share, attempt=attempt: _race(probe, share, attempt))
            for share in _SHARES
            for attempt in range(settings.race_attempts)
        )
        for case, run in cases:
            try:
                probe.reset()
                run()
            except (ProbeError, OSError) as exc:
                _emit(case, "UNKNOWN", reason=str(exc)[:240])
                if isinstance(exc, BudgetExpired):
                    break
        _emit("conclusion", "UNKNOWN", strict_dispatch_drain="not established by finite trials")
        return 0
    except (ProbeError, OSError) as exc:
        _emit("preflight", "UNKNOWN", reason=str(exc)[:240])
        return 2
    finally:
        if target_confirmed_stopped:
            try:
                stopped, reaped = probe.cleanup()
                _emit(
                    "cleanup",
                    "PASS" if stopped and reaped else "UNKNOWN",
                    stopped_immediately=stopped,
                    holders_reaped=reaped,
                )
            except ProbeError as exc:
                _emit("cleanup", "UNKNOWN", reason=str(exc)[:240])


if __name__ == "__main__":
    raise SystemExit(main())
