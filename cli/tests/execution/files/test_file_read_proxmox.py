"""Local helper/carrier composition, not native Proxmox acceptance."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from agentworks.execution._file_read import FileReadObservationState, read_file
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution.carrier import Deadline, Dispatch, ExitStatus
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, ProxmoxConnection
from tests.execution.files._file_read_support import install_fixed_lock_bundle

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="the file-read guest requires Linux")


@pytest.fixture
def fixed_lock_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_fixed_lock_bundle(tmp_path / "lock-root", monkeypatch)


@pytest.mark.parametrize("fault", [None, "truncated", "stdout_noise", "stderr_noise"])
def test_file_read_through_buffered_proxmox_delivery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixed_lock_bundle: None,
    fault: str | None,
) -> None:
    del fixed_lock_bundle
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
        runtime_path=sys.executable,
    )

    assert requests == [("POST", "exec"), ("GET", "exec-status?pid=42")]
    assert result.dispatch is Dispatch.SENT and result.carrier_completion == ExitStatus(code=0)
    assert "file-content-canary" not in repr(result)
    assert "reflected-secret-canary" not in repr(result)
    if fault is None:
        assert result.observation.state is FileReadObservationState.PRESENT
        assert result.observation.snapshot is not None
        assert result.observation.snapshot.data == data
    else:
        assert result.observation.snapshot is None
        assert result.observation.state in {FileReadObservationState.INVALID, FileReadObservationState.INCOMPLETE}
