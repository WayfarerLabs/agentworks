"""Local helper/carrier composition, not native Proxmox acceptance."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import urllib.request
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from agentworks.errors import ValidationError
from agentworks.execution._file_read import FileReadObservationState, read_file
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import RuntimePrerequisiteState
from agentworks.execution.carrier import CarrierIO, Deadline, Dispatch, ExitStatus, PreparedInvocation, SinkOutput
from agentworks.execution.carriers._proxmox_http import _MAX_RESPONSE_BYTES
from agentworks.execution.carriers._proxmox_http import _request as _http_request
from agentworks.execution.carriers.proxmox import (
    _MAX_COMPLETE_STDOUT_BYTES,
    ProxmoxCarrier,
    ProxmoxConnection,
)
from tests.execution.files._runtime_support import runtime_selection

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the file-read guest requires Linux")


class _Sink:
    def try_write(self, data: memoryview) -> int:
        return len(data)


def test_proxmox_capacity_budget_rejects_before_provider_dispatch() -> None:
    carrier = ProxmoxCarrier(ProxmoxConnection("https://pve.invalid", "node1", 101, "token", "synthetic"))
    invocation = PreparedInvocation(("/bin/true",))
    sink = _Sink()
    carrier.validate(
        invocation,
        io=CarrierIO(output=SinkOutput(sink, sink, required_complete_stdout_bytes=_MAX_COMPLETE_STDOUT_BYTES)),
    )
    with pytest.raises(ValidationError):
        carrier.validate(
            invocation,
            io=CarrierIO(output=SinkOutput(sink, sink, required_complete_stdout_bytes=_MAX_COMPLETE_STDOUT_BYTES + 1)),
        )
    worst_case_status = json.dumps(
        {"data": {"exited": True, "exitcode": 0, "out-data": "\0" * _MAX_COMPLETE_STDOUT_BYTES, "err-data": ""}}
    ).encode("ascii")
    assert len(worst_case_status) + 1_048_576 < _MAX_RESPONSE_BYTES


def test_file_read_rejects_unfit_proxmox_response_before_post(monkeypatch: pytest.MonkeyPatch) -> None:
    carrier = ProxmoxCarrier(ProxmoxConnection("https://pve.invalid", "node1", 101, "token", "synthetic"))

    def unexpected_request(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("provider request after failed capacity preflight")

    monkeypatch.setattr(carrier._wire, "request", unexpected_request)
    with pytest.raises(ValidationError):
        read_file(
            carrier,
            trusted_root_path="/tmp",
            relative_path="file",
            max_bytes=1_048_576,
            plan=IdentityPlan(IdentityExpectation(1001, 1001, (1001,)), IdentityMode.DIRECT),
            deadline=Deadline.after(10),
            runtime_selection=runtime_selection(sys.executable),
        )


@pytest.mark.parametrize("fault", [None, "truncated", "stdout_noise", "stderr_noise"])
def test_file_read_through_buffered_proxmox_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str | None,
) -> None:
    data = b"file-content-canary\x00\xff\r\n" * 1_024
    (tmp_path / "file-path-canary").write_bytes(data)
    carrier = ProxmoxCarrier(ProxmoxConnection("https://pve.invalid", "node1", 101, "token", "synthetic"))
    requests: list[tuple[str, str]] = []
    status: dict[str, object] = {}

    def request(method: str, suffix: str, *, body: bytes | None = None, timeout: float | None) -> dict[str, object]:
        requests.append((method, suffix))
        if method == "POST":
            assert suffix == "exec" and body is not None and body.isascii()
            assert len(body) <= 65_536
            envelope = json.loads(body)
            assert "file-path-canary" not in " ".join(envelope["command"])
            result = subprocess.run(
                envelope["command"],
                input=envelope["input-data"].encode("ascii"),
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            assert result.returncode == 0 and not result.stderr
            assert result.stdout.isascii()
            status.update(
                exited=True,
                exitcode=result.returncode,
                **{
                    "out-data": result.stdout.decode("ascii"),
                    "err-data": "",
                    "out-truncated": False,
                    "err-truncated": False,
                },
            )
            if fault == "truncated":
                # Even complete-looking frames cannot overrule provider truncation.
                status["out-truncated"] = True
            elif fault == "stdout_noise":
                status["out-data"] = "reflected-secret-canary\n" + str(status["out-data"])
            elif fault == "stderr_noise":
                status["err-data"] = "reflected-secret-canary\n"
            return {"pid": 42}
        assert method == "GET" and suffix == "exec-status?pid=42" and body is None
        return status

    monkeypatch.setattr(carrier._wire, "request", request)
    result = read_file(
        carrier,
        trusted_root_path=str(tmp_path),
        relative_path="file-path-canary",
        max_bytes=len(data),
        plan=IdentityPlan(
            IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()}))),
            IdentityMode.DIRECT,
        ),
        deadline=Deadline.after(15),
        runtime_selection=runtime_selection(sys.executable),
    )

    assert requests == [("POST", "exec"), ("GET", "exec-status?pid=42")]
    assert result.dispatch is Dispatch.SENT and result.carrier_completion == ExitStatus(code=0)
    assert "file-content-canary" not in repr(result)
    assert "reflected-secret-canary" not in repr(result)
    if fault is None:
        assert result.runtime_prerequisite.state is RuntimePrerequisiteState.READY
        assert result.observation is not None
        assert result.observation.state is FileReadObservationState.PRESENT
        assert result.observation.snapshot is not None
        assert result.observation.snapshot.data == data
    elif fault == "stdout_noise":
        assert result.runtime_prerequisite.state is RuntimePrerequisiteState.UNKNOWN
        assert result.observation is None
    else:
        assert result.runtime_prerequisite.state is RuntimePrerequisiteState.READY
        assert result.observation is not None
        assert result.observation.snapshot is None
        assert result.observation.state in {FileReadObservationState.INVALID, FileReadObservationState.INCOMPLETE}


def test_large_file_read_crosses_http_response_reader_without_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = bytes(range(256)) * 2_900
    (tmp_path / "large-file").write_bytes(data)
    carrier = ProxmoxCarrier(ProxmoxConnection("https://pve.invalid", "node1", 101, "token", "synthetic"))
    status: dict[str, object] = {}
    http_response = io.BytesIO()
    opener = MagicMock()
    opener.open.return_value = http_response
    monkeypatch.setattr(urllib.request, "build_opener", MagicMock(return_value=opener))

    def request(method: str, suffix: str, *, body: bytes | None = None, timeout: float | None) -> dict[str, object]:
        if method == "POST":
            assert suffix == "exec" and body is not None and body.isascii()
            envelope = json.loads(body)
            completed = subprocess.run(
                envelope["command"],
                input=envelope["input-data"].encode("ascii"),
                capture_output=True,
                timeout=timeout,
                check=False,
            )
            assert completed.returncode == 0 and not completed.stderr
            assert completed.stdout.isascii()
            status.update(
                exited=True,
                exitcode=0,
                **{
                    "out-data": completed.stdout.decode("ascii"),
                    "err-data": "",
                    "out-truncated": False,
                    "err-truncated": False,
                },
            )
            return {"pid": 42}

        assert method == "GET" and suffix == "exec-status?pid=42" and body is None
        http_response.write(json.dumps({"data": status}, ensure_ascii=True).encode("ascii"))
        http_response.seek(0)
        encoded = _http_request(
            {
                "connection": {
                    "api_url": "https://pve.invalid",
                    "node": "node1",
                    "vmid": 101,
                    "token_id": "token",
                    "token_secret": "synthetic",
                    "ca_bundle": None,
                },
                "method": "GET",
                "suffix": suffix,
                "body": None,
                "timeout": 2.5,
            }
        )
        parsed = json.loads(encoded)
        assert type(parsed) is dict and type(parsed.get("data")) is dict
        return dict(parsed["data"])

    monkeypatch.setattr(carrier._wire, "request", request)
    result = read_file(
        carrier,
        trusted_root_path=str(tmp_path),
        relative_path="large-file",
        max_bytes=len(data),
        plan=IdentityPlan(
            IdentityExpectation(os.geteuid(), os.getegid(), tuple(sorted(set(os.getgroups()) | {os.getegid()}))),
            IdentityMode.DIRECT,
        ),
        deadline=Deadline.after(30),
        runtime_selection=runtime_selection(sys.executable),
    )

    assert opener.open.call_count == 1
    assert http_response.closed
    delivered_stdout_size = len(str(status["out-data"]).encode("ascii"))
    response_size = len(json.dumps({"data": status}, ensure_ascii=True).encode("ascii"))
    assert 950_000 < delivered_stdout_size < _MAX_COMPLETE_STDOUT_BYTES
    assert _MAX_COMPLETE_STDOUT_BYTES < _MAX_RESPONSE_BYTES
    assert delivered_stdout_size < response_size < _MAX_RESPONSE_BYTES
    assert result.dispatch is Dispatch.SENT and result.carrier_completion == ExitStatus(code=0)
    assert result.runtime_prerequisite.state is RuntimePrerequisiteState.READY
    assert result.observation is not None and result.observation.state is FileReadObservationState.PRESENT
    assert result.observation.snapshot is not None and result.observation.snapshot.data == data
