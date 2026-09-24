#!/usr/bin/env python3
"""Native Windows experiment for WSL UNC handles as platform holds.

Run only against a disposable distro that is stopped before the probe starts.
This records bounded observations; it cannot prove WSL server-side dispatch drain.
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
from typing import Literal, Never

_DISTRO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_SHARES = {"wsl$": "wsl$", "localhost": "wsl.localhost"}
_MAX_RECORD = 4096
_INVALID_HANDLE = ctypes.c_void_p(-1).value
State = Literal["PASS", "FAIL", "UNKNOWN"]


def _distro(value: str) -> str:
    """Validate operator input before it reaches a Windows path or WSL argv."""
    if _DISTRO.fullmatch(value) is None:
        raise ValueError("distro names must be 1-80 safe ASCII characters")
    return value


def _unc_path(share: str, distro: str) -> str:
    if share not in _SHARES:
        raise ValueError("unsupported WSL share")
    return "\\\\" + _SHARES[share] + "\\" + _distro(distro) + "\\etc\\os-release"


@dataclass(frozen=True)
class Settings:
    target: str
    unrelated: str | None
    idle_seconds: float
    budget_seconds: float

    @property
    def hold_seconds(self) -> float:
        return 3 * self.idle_seconds

    @property
    def poll_seconds(self) -> float:
        return min(1.0, max(0.1, self.idle_seconds / 10))

    @property
    def open_seconds(self) -> float:
        return min(30.0, max(5.0, self.idle_seconds))

    @property
    def release_seconds(self) -> float:
        return max(10.0, 3 * self.idle_seconds)

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> Settings:
        target = _distro(args.target)
        if args.confirm_target != target:
            raise ValueError("--confirm-target must exactly match --target")
        unrelated = _distro(args.unrelated) if args.unrelated else None
        if unrelated is not None and unrelated.casefold() == target.casefold():
            raise ValueError("unrelated distro must differ from target")
        idle, budget = args.idle_seconds, args.budget_seconds
        if not 0 < idle <= 3600 or not 30 <= budget <= 86400:
            raise ValueError("idle must be in (0, 3600] and budget in [30, 86400] seconds")
        return cls(target, unrelated, idle, budget)


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


class _JsonParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(message)


@dataclass(frozen=True)
class Observation:
    matched: bool | None
    elapsed_seconds: float


class WslProbe:
    def __init__(self, settings: Settings, wsl: str) -> None:
        self.settings = settings
        self.wsl = wsl
        self.deadline = time.monotonic() + settings.budget_seconds
        self.cleanup_reserve = min(10.0, settings.budget_seconds / 5)
        self.holders: list[Holder] = []
        self.unrelated_before_force: bool | None = None
        self.unrelated_after_force: bool | None = None

    def remaining(self, requested: float, *, cleanup: bool = False) -> float:
        left = self.deadline - (0 if cleanup else self.cleanup_reserve) - time.monotonic()
        if left <= 0:
            raise ProbeError("observation budget exhausted")
        return min(requested, left)

    def _wsl(self, *argv: str, timeout: float, cleanup: bool = False) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                [self.wsl, *argv],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self.remaining(timeout, cleanup=cleanup),
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
            holder.close(self.settings.open_seconds)
        self.holders.clear()
        if self.running(self.settings.target):
            self.terminate()
        status = self.observe(self.settings.target, False, self.settings.release_seconds)
        if status.matched is not True:
            raise ProbeError("target could not be confirmed stopped after exact termination")

    def cleanup(self) -> tuple[bool | None, bool, bool | None, KeyboardInterrupt | None, str | None]:
        """Reap holders and report the immediate post-termination target state."""
        holder_error = False
        interrupted: KeyboardInterrupt | None = None
        for holder in self.holders:
            try:
                holder.close(self.settings.open_seconds)
            except KeyboardInterrupt as exc:
                holder_error = True
                if interrupted is None:
                    interrupted = exc
            except (ProbeError, OSError, subprocess.TimeoutExpired):
                holder_error = True
        self.holders.clear()
        error: str | None = None
        try:
            terminated = self._wsl(
                "--terminate", self.settings.target, timeout=self.settings.open_seconds, cleanup=True
            )
            if terminated.returncode != 0:
                error = f"target termination returned {terminated.returncode}"
        except ProbeError as exc:
            error = str(exc)[:240]
        try:
            result = self._wsl("--list", "--running", "--quiet", timeout=self.settings.open_seconds, cleanup=True)
            if result.returncode != 0:
                raise ProbeError(f"final target observation returned {result.returncode}")
            running = _decode_wsl(result.stdout)
            unrelated = self.settings.unrelated in running if self.settings.unrelated is not None else None
            return self.settings.target not in running, not holder_error, unrelated, interrupted, error
        except (ProbeError, UnicodeError) as exc:
            return None, not holder_error, None, interrupted, error or str(exc)[:240]

    def observe(self, distro: str, expected: bool, seconds: float, *, throughout: bool = False) -> Observation:
        start = time.monotonic()
        if self.deadline - self.cleanup_reserve - start < seconds + self.settings.open_seconds:
            return Observation(None, 0.0)
        end = start + seconds
        while True:
            try:
                actual = self.running(distro)
            except ProbeError:
                return Observation(None, time.monotonic() - start)
            if throughout and actual is not expected:
                return Observation(False, time.monotonic() - start)
            if not throughout and actual is expected:
                return Observation(True, time.monotonic() - start)
            left = end - time.monotonic()
            if left <= 0:
                return Observation(throughout, time.monotonic() - start)
            time.sleep(min(self.settings.poll_seconds, left))

    def new_holder(self, share: str) -> Holder:
        holder = Holder(share, self.settings.target)
        self.holders.append(holder)
        return holder


class Holder:
    """One independent Windows process with one read-only file handle."""

    def __init__(self, share: str, target: str) -> None:
        self.events: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()), "_hold", share, target],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        self.events.put(self.process.stdout.readline())

    def opened(self, timeout: float) -> bool | None:
        try:
            event = self.events.get(timeout=timeout)
        except queue.Empty:
            return None
        if event == "opened\n":
            return True
        if event.startswith("open_failed:"):
            return False
        return None

    def _force(self, deadline: float) -> None:
        if self.process.poll() is None:
            with contextlib.suppress(OSError):
                self.process.kill()
        try:
            self.process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise ProbeError("holder process did not exit after kill") from exc

    def _finish(self, deadline: float) -> None:
        if self.process.poll() is None:
            raise ProbeError("holder process is still alive")
        self.reader.join(timeout=max(0.0, deadline - time.monotonic()))
        if self.reader.is_alive():
            raise ProbeError("holder reader did not settle after process exit")
        if self.process.stdin is not None:
            with contextlib.suppress(OSError):
                self.process.stdin.close()
        if self.process.stdout is not None:
            self.process.stdout.close()

    def _settle(self, *, graceful: bool, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        interrupted: BaseException | None = None
        if graceful and self.process.poll() is None:
            try:
                assert self.process.stdin is not None
                self.process.stdin.write("close\n")
                self.process.stdin.flush()
                self.process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except BaseException as exc:
                interrupted = exc
        if self.process.poll() is None:
            self._force(deadline)
        self._finish(deadline)
        if interrupted is not None and not isinstance(interrupted, (OSError, subprocess.TimeoutExpired)):
            raise interrupted

    def close(self, timeout: float = 5.0) -> None:
        self._settle(graceful=True, timeout=timeout)

    def kill(self, timeout: float = 5.0) -> None:
        self._settle(graceful=False, timeout=timeout)


def _holder_main(share: str, target: str) -> int:
    if sys.platform != "win32":
        return 2
    try:
        path = _unc_path(share, target)
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
    handle = kernel.CreateFileW(path, 0x80000000, 0x00000007, None, 3, 0, None)
    if handle == _INVALID_HANDLE:
        print(f"open_failed:{ctypes.get_last_error()}", flush=True)
        return 1
    print("opened", flush=True)
    try:
        sys.stdin.readline()
    finally:
        kernel.CloseHandle(handle)
    return 0


def _cold(probe: WslProbe, share: str) -> State:
    case = f"cold_{share}"
    probe.require_stopped()
    holder = probe.new_holder(share)
    opened = holder.opened(probe.remaining(probe.settings.open_seconds))
    holder.close(probe.settings.open_seconds)
    stopped = probe.observe(probe.settings.target, False, probe.settings.release_seconds, throughout=True)
    if share == "wsl$" and (opened is True or stopped.matched is False):
        state: State = "FAIL"
    elif opened is not None and stopped.matched is not None:
        state = "PASS"
    else:
        state = "UNKNOWN"
    _emit(case, state, opened=opened, stayed_stopped=stopped.matched, observed_seconds=stopped.elapsed_seconds)
    return state


def _baseline(probe: WslProbe) -> Observation:
    probe.start()
    stopped = probe.observe(probe.settings.target, False, probe.settings.release_seconds)
    state: State = "PASS" if stopped.matched is True else "UNKNOWN"
    _emit("baseline_no_handle", state, stopped=stopped.matched, observed_seconds=stopped.elapsed_seconds)
    return stopped


def _retention(probe: WslProbe, *, two: bool, seconds: float) -> None:
    case = "two_holders" if two else "hold_and_close"
    probe.start()
    holders = [probe.new_holder("wsl$") for _ in range(2 if two else 1)]
    opened = [holder.opened(probe.remaining(probe.settings.open_seconds)) for holder in holders]
    if not all(value is True for value in opened):
        _emit(case, "UNKNOWN", opened=opened, selected_hold_seconds=seconds)
        return
    if two:
        holders[0].close(probe.settings.open_seconds)
    held = probe.observe(probe.settings.target, True, seconds, throughout=True)
    holders[-1].close(probe.settings.open_seconds)
    released = probe.observe(probe.settings.target, False, probe.settings.release_seconds)
    state: State = (
        "FAIL" if held.matched is False else "PASS" if held.matched is True and released.matched is True else "UNKNOWN"
    )
    _emit(
        case,
        state,
        selected_hold_seconds=seconds,
        held_past_idle=held.matched,
        hold_observed_seconds=held.elapsed_seconds,
        stopped_after_close=released.matched,
        release_observed_seconds=released.elapsed_seconds,
    )


def _abrupt(probe: WslProbe) -> None:
    probe.start()
    holder = probe.new_holder("wsl$")
    opened = holder.opened(probe.remaining(probe.settings.open_seconds))
    if opened is not True:
        _emit("abrupt_holder_death", "UNKNOWN", opened=opened)
        return
    holder.kill(probe.settings.open_seconds)
    released = probe.observe(probe.settings.target, False, probe.settings.release_seconds)
    _emit(
        "abrupt_holder_death",
        "PASS" if released.matched is True else "UNKNOWN",
        stopped_after_death=released.matched,
        observed_seconds=released.elapsed_seconds,
    )


def _forced(probe: WslProbe) -> None:
    if probe.settings.unrelated is not None:
        probe.unrelated_before_force = probe.running(probe.settings.unrelated)
        if probe.unrelated_before_force is not True:
            _emit("forced_terminate", "UNKNOWN", unrelated_running_before=False)
            return
    probe.start()
    holder = probe.new_holder("wsl$")
    opened = holder.opened(probe.remaining(probe.settings.open_seconds))
    if opened is not True:
        _emit("forced_terminate", "UNKNOWN", opened=opened)
        return
    probe.terminate()
    stopped = probe.observe(probe.settings.target, False, probe.settings.release_seconds)
    _emit(
        "forced_terminate",
        "PASS" if stopped.matched is True else "FAIL" if stopped.matched is False else "UNKNOWN",
        target_stopped=stopped.matched,
        observed_seconds=stopped.elapsed_seconds,
    )
    if probe.settings.unrelated is not None:
        with contextlib.suppress(ProbeError):
            probe.unrelated_after_force = probe.running(probe.settings.unrelated)


def _parser() -> argparse.ArgumentParser:
    parser = _JsonParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--confirm-target", required=True)
    parser.add_argument("--unrelated")
    for name in ("idle", "budget"):
        parser.add_argument(f"--{name}-seconds", required=True, type=float)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "_hold":
        return _holder_main(args[1], args[2]) if len(args) == 3 else 2
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
    phase = "preflight"
    try:
        installed = probe.listed(running=False)
        if settings.target not in installed or (settings.unrelated is not None and settings.unrelated not in installed):
            raise ProbeError("target or unrelated distro is not installed")
        probe.require_stopped()
        unrelated_running = probe.running(settings.unrelated) if settings.unrelated else None
        if settings.unrelated is not None and unrelated_running is not True:
            raise ProbeError("supplied unrelated distro must already be running")
        target_confirmed_stopped = True
        _emit(
            "preflight",
            "PASS",
            target=settings.target,
            unrelated=settings.unrelated,
            unrelated_running=unrelated_running,
            idle_seconds=settings.idle_seconds,
            minimum_hold_seconds=settings.hold_seconds,
            poll_seconds=settings.poll_seconds,
            release_seconds=settings.release_seconds,
        )

        phase = "cold_wsl$"
        probe.reset()
        guard = _cold(probe, "wsl$")
        phase = "cold_localhost"
        probe.reset()
        _cold(probe, "localhost")
        if guard != "PASS":
            _emit("hold_matrix", "UNKNOWN", reason="cold wsl$ guard did not pass")
            return 0

        phase = "baseline_no_handle"
        probe.reset()
        baseline = _baseline(probe)
        if baseline.matched is not True:
            _emit("hold_matrix", "UNKNOWN", reason="no-handle baseline did not stop")
            return 0
        retention_seconds = max(settings.hold_seconds, 3 * baseline.elapsed_seconds)

        cases = (
            ("hold_and_close", lambda runner: _retention(runner, two=False, seconds=retention_seconds)),
            ("two_holders", lambda runner: _retention(runner, two=True, seconds=retention_seconds)),
            ("abrupt_holder_death", _abrupt),
            ("forced_terminate", _forced),
        )
        for case, run in cases:
            phase = case
            probe.reset()
            run(probe)
        return 0
    except (ProbeError, OSError) as exc:
        _emit(phase, "UNKNOWN", reason=str(exc)[:240])
        return 2
    finally:
        cleanup_interrupt: KeyboardInterrupt | None = None
        if target_confirmed_stopped:
            try:
                stopped, reaped, unrelated_after_cleanup, cleanup_interrupt, cleanup_error = probe.cleanup()
                _emit(
                    "cleanup",
                    "PASS" if stopped is True and reaped and cleanup_error is None else "UNKNOWN",
                    stopped_immediately=stopped,
                    holders_reaped=reaped,
                    interrupted=cleanup_interrupt is not None,
                    reason=cleanup_error,
                )
                if settings.unrelated is not None:
                    before = probe.unrelated_before_force
                    after_force = probe.unrelated_after_force
                    state: State = (
                        "PASS"
                        if before is True and after_force is True and unrelated_after_cleanup is True
                        else "FAIL"
                        if before is True and (after_force is False or unrelated_after_cleanup is False)
                        else "UNKNOWN"
                    )
                    _emit(
                        "unrelated_survival",
                        state,
                        running_before_force=before,
                        running_after_force=after_force,
                        running_after_cleanup=unrelated_after_cleanup,
                    )
            except ProbeError as exc:
                _emit("cleanup", "UNKNOWN", reason=str(exc)[:240])
                if settings.unrelated is not None:
                    _emit("unrelated_survival", "UNKNOWN", cleanup_observation=False)
        _emit("conclusion", "UNKNOWN", strict_dispatch_drain="not established by UNC handle observations")
        if cleanup_interrupt is not None:
            raise cleanup_interrupt


if __name__ == "__main__":
    raise SystemExit(main())
