"""Delivered service checks with simulated admission, not native root/systemd acceptance."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Any

import pytest

from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._managed_service_bundle import FIXED_SOURCE
from agentworks.execution._managed_start_guest import _service_argv
from agentworks.execution._managed_start_protocol import ManagedStartError
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux managed service admission")
RUN = "a" * 32
ROOT = IdentityExpectation(0, 27, (7, 27, 91))
GUEST = VMGuestIdentity("d" * 32, "00000000-0000-4000-8000-000000000001", 1234)


def _arguments() -> list[str]:
    return ["-c", *_service_argv(RUN, "/usr/bin/python3.11", ROOT, GUEST)[-2:]]


def _execute(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    *,
    observed: VMGuestIdentity = GUEST,
    nonroot: bool = False,
) -> tuple[int, list[object]]:
    events: list[object] = []

    @contextmanager
    def admit(uid: int, gid: int, groups: tuple[int, ...]) -> Iterator[Any]:
        events.append(("admit", uid, gid, groups))
        try:
            yield lambda: b"held init stat"
        finally:
            events.append("closed")

    def trace(frame: FrameType, event: str, _arg: object) -> Any:
        name = frame.f_globals.get("__name__", "")
        if frame.f_code.co_name != "<module>" or not name.startswith("_agw_managed_service"):
            return trace
        if event == "call":
            events.append(("load", name))
        if event == "return":
            if name.endswith("._guest_bootstrap") and not nonroot:
                frame.f_globals["_admit"] = admit
            elif name == "_agw_managed_service._vm_guest_identity_guest":

                def identity() -> VMGuestIdentity:
                    assert frame.f_globals["_INIT_READER"]() == b"held init stat"
                    events.append("guest")
                    return observed

                frame.f_globals["_identity"] = identity
            elif name == "_agw_managed_service._managed_service_guest":

                def controller(run_id: str) -> int:
                    events.append(("controller", run_id, os.environ.get("NOTIFY_SOCKET")))
                    return 23

                frame.f_globals["main"] = controller
        return trace

    monkeypatch.setattr(sys, "argv", arguments)
    if nonroot:
        monkeypatch.setattr(os, "getresuid", lambda: (1001, 1001, 1001))
    previous_trace = sys.gettrace()
    sys.settrace(trace)
    try:
        with pytest.raises(SystemExit) as exit_info:
            exec(compile(FIXED_SOURCE, "<delivered-service>", "exec"), {"__name__": "__main__"})
        assert type(exit_info.value.code) is int
        return exit_info.value.code, events
    finally:
        sys.settrace(previous_trace)
        for name in tuple(sys.modules):
            if name.startswith("_agw_managed_service"):
                sys.modules.pop(name)


def test_delivered_service_admits_exact_root_credentials_and_full_guest_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_SOCKET", "@test-managed-service")
    result, events = _execute(monkeypatch, _arguments())
    assert result == 23
    assert events.count(("admit", 0, 27, (7, 27, 91))) == 1
    assert events.count("guest") == 1
    assert events.count(("controller", RUN, "@test-managed-service")) == 1
    observer = ("load", "_agw_managed_service._vm_guest_identity_guest")
    store = ("load", "_agw_managed_service._managed_job_store")
    assert events.count(observer) == 1
    assert events.index(("admit", 0, 27, (7, 27, 91))) < events.index(observer)
    assert events.index("guest") < events.index(store)
    assert events[-1] == "closed"


@pytest.mark.parametrize(
    "observed",
    [
        VMGuestIdentity("e" * 32, GUEST.boot_id, GUEST.init_start_ticks),
        VMGuestIdentity(GUEST.instance_marker, "00000000-0000-4000-8000-000000000002", GUEST.init_start_ticks),
        VMGuestIdentity(GUEST.instance_marker, GUEST.boot_id, 1235),
    ],
)
def test_delivered_service_wrong_full_guest_refuses_before_controller_modules(
    monkeypatch: pytest.MonkeyPatch, observed: VMGuestIdentity
) -> None:
    result, events = _execute(monkeypatch, _arguments(), observed=observed)
    assert result == 125
    assert "guest" in events
    assert not any(event == ("load", "_agw_managed_service._managed_job_store") for event in events)
    assert not any(event == ("load", "_agw_managed_service._managed_service_guest") for event in events)
    assert events[-1] == "closed"


def test_delivered_service_nonroot_refuses_before_guest_and_controller_load(monkeypatch: pytest.MonkeyPatch) -> None:
    result, events = _execute(monkeypatch, _arguments(), nonroot=True)
    assert result == 125
    assert not any(event == ("load", "_agw_managed_service._vm_guest_identity_guest") for event in events)
    assert not any(event == ("load", "_agw_managed_service._managed_service_guest") for event in events)


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "extra",
        "invalid_json",
        "noncanonical",
        "nonroot",
        "groups",
        "guest",
        "run",
        "oversize",
        "missing_identity",
        "missing_guest",
        "missing_gid",
        "empty_groups",
        "unknown",
    ],
)
def test_delivered_service_malformed_admission_refuses_before_bootstrap(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    arguments = _arguments()
    data = json.loads(arguments[-1])
    if fault == "missing":
        arguments.pop()
    elif fault == "extra":
        arguments.append("untrusted-source")
    elif fault == "invalid_json":
        arguments[-1] = "invalid"
    elif fault == "noncanonical":
        arguments[-1] += " "
    elif fault == "run":
        arguments[-2] = "../untrusted"
    elif fault == "oversize":
        arguments[-1] = " " * 65_537
    else:
        if fault == "nonroot":
            data["identity"]["euid"] = 1001
        elif fault == "groups":
            data["identity"]["groups"] = [91, 7, 27]
        elif fault == "guest":
            data["guest"][2] = True
        elif fault == "missing_identity":
            del data["identity"]
        elif fault == "missing_guest":
            del data["guest"]
        elif fault == "missing_gid":
            del data["identity"]["egid"]
        elif fault == "empty_groups":
            data["identity"]["groups"] = []
        elif fault == "unknown":
            data["source"] = "untrusted-source"
        arguments[-1] = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    result, events = _execute(monkeypatch, arguments)
    assert result == 125
    assert not any(isinstance(event, tuple) and event[0] == "admit" for event in events)
    assert not any(event == ("load", "_agw_managed_service._managed_job_store") for event in events)


def test_service_argv_refuses_oversized_admission_data() -> None:
    root = IdentityExpectation(0, 0, tuple(range(65_536)))
    with pytest.raises(ManagedStartError):
        _service_argv(RUN, "/usr/bin/python3.11", root, GUEST)


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
def test_delivered_service_isolated_runtime_refuses_missing_data(interpreter: str, tmp_path: Path) -> None:
    if not Path(interpreter).is_file():
        pytest.skip("interpreter unavailable")
    completed = subprocess.run(
        [interpreter, "-I", "-S", "-B", "-c", FIXED_SOURCE, RUN],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        cwd=tmp_path,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 125
    assert completed.stdout == completed.stderr == b""
