"""Native REST evidence and refusal boundaries, without provider connections."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from agentworks.errors import ValidationError
from agentworks.execution.carrier import (
    Capture,
    CarrierIO,
    Deadline,
    Discard,
    Dispatch,
    ExitStatus,
    Failure,
    FiniteInput,
    PreparedInvocation,
    Provenance,
    Retention,
)
from agentworks.execution.carriers._proxmox_http import _NoRedirect, _request, main
from agentworks.execution.carriers.proxmox import (
    ProxmoxCarrier,
    ProxmoxConnection,
    _ProxmoxWire,
    _WireFailure,
)


def connection() -> ProxmoxConnection:
    return ProxmoxConnection("https://pve.example:8006", "node1", 123, "user@pve!token", "secret-canary")


def worker_payload() -> dict[str, Any]:
    return {"connection": asdict(connection()), "method": "POST", "suffix": "exec", "body": "{}", "timeout": 2.5}


def stub_process(monkeypatch: pytest.MonkeyPatch, body: bytes) -> MagicMock:
    process = MagicMock()
    process.communicate.return_value = (body, None)
    process.returncode = 0
    monkeypatch.setattr(subprocess, "Popen", MagicMock(return_value=process))
    return process


def test_connection_does_not_discover_or_expose_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    build = MagicMock()
    monkeypatch.setattr(urllib.request, "build_opener", build)
    value = connection()
    assert "secret-canary" not in repr(value)
    _ProxmoxWire(value)
    build.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        "http://pve.example",
        "https://user:password@pve.example",
        "https://pve.example/api",
        "https://pve.example/?token=a",
    ],
)
def test_connection_rejects_unsafe_origin(url: str) -> None:
    with pytest.raises(ValidationError):
        ProxmoxConnection(url, "node1", 123, "token", "secret")


@pytest.mark.parametrize(
    "url",
    [
        "https://user:synthetic-secret@pve.example\uff1a8006",
        "https://pve.example:synthetic-secret",
        "https://pve.example:65536",
        "https://pve.example:0",
    ],
)
def test_invalid_origin_discards_parser_exception_graph(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    network = MagicMock()
    worker = MagicMock()
    monkeypatch.setattr(urllib.request, "build_opener", network)
    monkeypatch.setattr(subprocess, "Popen", worker)
    with pytest.raises(ValidationError) as raised:
        ProxmoxConnection(url, "node1", 123, "token", "synthetic-secret")
    assert "synthetic-secret" not in str(raised.value)
    assert "synthetic-secret" not in repr(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    network.assert_not_called()
    worker.assert_not_called()


@pytest.mark.parametrize("node,vmid", [("../node", 123), ("node/other", 123), ("node1", True), ("node1", 0)])
def test_connection_rejects_ambiguous_vm_address(node: str, vmid: int) -> None:
    with pytest.raises(ValidationError):
        ProxmoxConnection("https://pve.example", node, vmid, "token", "secret")


def test_wire_sends_json_and_uses_explicit_network_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    response = io.BytesIO(b'{"data":{"pid":42}}')
    opener = MagicMock()
    opener.open.return_value = response
    build = MagicMock(return_value=opener)
    monkeypatch.setattr(urllib.request, "build_opener", build)
    body = json.dumps({"command": ["/bin/true"], "input-data": ""}).encode()
    assert _request({**worker_payload(), "body": body.decode()}) == b'{"data":{"pid":42}}'
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://pve.example:8006/api2/json/nodes/node1/qemu/123/agent/exec"
    assert request.data == body
    assert request.get_header("Content-type") == "application/json"
    assert opener.open.call_args.kwargs == {"timeout": 2.5}
    handlers = build.call_args.args
    assert any(isinstance(handler, _NoRedirect) for handler in handlers)
    assert any(
        isinstance(handler, urllib.request.ProxyHandler) and vars(handler)["proxies"] == {} for handler in handlers
    )
    assert response.closed


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 401, 500])
def test_http_failure_is_not_replayed_or_exposed(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    response = io.BytesIO(b"secret-canary")
    error = urllib.error.HTTPError("https://pve.example", status, "secret-canary", {}, response)
    opener = MagicMock()
    opener.open.side_effect = error
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: opener)
    source = io.BytesIO(json.dumps(worker_payload()).encode())
    sink = io.BytesIO()
    with monkeypatch.context() as context:
        context.setattr(sys, "stdin", SimpleNamespace(buffer=source))
        context.setattr(sys, "stdout", SimpleNamespace(buffer=sink))
        assert main() == 1
    assert not sink.getvalue()
    assert opener.open.call_count == 1
    assert response.closed


@pytest.mark.parametrize("body", [b"invalid", b"[]", b'{"data":null}', b'{"data":[]}'])
def test_malformed_wire_envelope_is_not_execution_evidence(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    stub_process(monkeypatch, body)
    with pytest.raises(_WireFailure):
        _ProxmoxWire(connection()).request("GET", "exec-status?pid=42", timeout=1)


def test_wire_bounds_response_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentworks.execution.carriers._proxmox_http._MAX_RESPONSE_BYTES", 10)
    response = io.BytesIO(b'{"data":{"pid":42}}')
    opener = MagicMock()
    opener.open.return_value = response
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: opener)
    with pytest.raises(ValueError):
        _request(worker_payload())
    assert response.closed


def test_redirect_handler_never_follows() -> None:
    request = urllib.request.Request("https://pve.example", data=b"{}")
    assert _NoRedirect().redirect_request(request, None, 307, "", {}, "https://other.example") is None


def test_worker_credentials_use_only_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    process = stub_process(monkeypatch, b'{"data":{"pid":42}}')
    spawn = MagicMock(return_value=process)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    assert _ProxmoxWire(connection()).request("POST", "exec", body=b"{}", timeout=1) == {"pid": 42}
    payload = json.loads(process.communicate.call_args.args[0])
    assert payload["connection"]["token_secret"] == "secret-canary"
    assert "secret-canary" not in repr(spawn.call_args)
    assert spawn.call_args.kwargs["stderr"] == subprocess.DEVNULL


def test_ca_bundle_path_serializes_only_for_worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    process = stub_process(monkeypatch, b'{"data":{"pid":42}}')
    bundle = tmp_path / "cluster CA.pem"
    value = replace(connection(), ca_bundle=bundle)
    assert value.ca_bundle == bundle
    assert _ProxmoxWire(value).request("POST", "exec", body=b"{}", timeout=1) == {"pid": 42}
    assert json.loads(process.communicate.call_args.args[0])["connection"]["ca_bundle"] == str(bundle)


@pytest.mark.windows
def test_owned_http_worker_is_killed_and_reaped_at_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    original = subprocess.Popen
    children = []

    def spawn(argv, **kwargs):
        child = original([sys.executable, "-c", "import sys,time; sys.stdin.buffer.read(); time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", spawn)
    with pytest.raises(subprocess.TimeoutExpired):
        _ProxmoxWire(connection()).request("POST", "exec", body=b"{}", timeout=0.05)
    assert len(children) == 1
    assert children[0].poll() is not None
    assert children[0].stdin is not None and children[0].stdout is not None
    assert children[0].stdin.closed and children[0].stdout.closed


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_worker_interrupt_reaps_before_propagating(monkeypatch: pytest.MonkeyPatch, interruption: type) -> None:
    process = stub_process(monkeypatch, b"")
    process.communicate.side_effect = [interruption(), (b"", None)]
    process.poll.return_value = None
    with pytest.raises(interruption):
        _ProxmoxWire(connection()).request("POST", "exec", body=b"{}", timeout=1)
    process.kill.assert_called_once()
    assert process.communicate.call_count == 2


def test_failed_worker_never_exposes_its_output(monkeypatch: pytest.MonkeyPatch) -> None:
    process = stub_process(monkeypatch, b"secret-canary")
    process.returncode = 1
    with pytest.raises(_WireFailure) as raised:
        _ProxmoxWire(connection()).request("POST", "exec", body=b"{}", timeout=1)
    assert "secret-canary" not in str(raised.value)


@pytest.mark.windows
def test_worker_entry_point_rejects_invalid_input_without_diagnostics() -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-m", "agentworks.execution.carriers._proxmox_http"],
        input=b"invalid-secret-canary",
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert result.stdout == result.stderr == b""


@pytest.mark.windows
def test_native_import_and_construction_do_not_load_retirement_modules() -> None:
    script = """
import sys
class Blocker:
    def find_spec(self, fullname, path=None, target=None):
        blocked = ('agentworks.ssh', 'agentworks.transports', 'agentworks.remote_exec',
                   'agentworks.harness_setup', 'agentworks.native_files', 'agentworks.plugins')
        if any(fullname == name or fullname.startswith(name + '.') for name in blocked):
            raise AssertionError(fullname)
sys.meta_path.insert(0, Blocker())
from agentworks.execution.carriers.proxmox import ProxmoxCarrier, ProxmoxConnection
from agentworks.execution.carriers import _proxmox_http
ProxmoxCarrier(ProxmoxConnection('https://pve.example', 'node1', 123, 'token', 'secret'))
"""
    subprocess.run([sys.executable, "-I", "-c", script], check=True, capture_output=True, timeout=10)


@dataclass
class Clock:
    now: float = 100.0

    def sleep(self, duration: float) -> None:
        self.now += duration

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    value = Clock()
    monkeypatch.setattr("agentworks.execution.carriers.proxmox.time.monotonic", value.monotonic)
    monkeypatch.setattr("agentworks.execution.carriers.proxmox.time.sleep", value.sleep)
    return value


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    request = MagicMock(side_effect=[{"pid": 42}, {"exited": 1, "exitcode": 0}])
    monkeypatch.setattr(_ProxmoxWire, "request", request)
    return request


def execute(io: CarrierIO | None = None, deadline: Deadline | None = None):
    return ProxmoxCarrier(connection()).execute(
        PreparedInvocation(("/bin/true",)), io=io or CarrierIO(), deadline=deadline or Deadline(None)
    )


@pytest.mark.parametrize("code", [0, 1, 255])
def test_observed_bootstrap_exit_is_not_a_delivery_failure(wire: MagicMock, code: int) -> None:
    wire.side_effect = [{"pid": 42}, {"exited": 1, "exitcode": code, "out-data": "a\r\n\0", "err-data": "b\n\n"}]
    report = execute()
    assert report.dispatch == Dispatch.SENT
    assert report.completion == ExitStatus(code=code)
    assert report.failure is None
    assert report.local_status is None
    assert report.stdout.data == b"a\r\n\0"
    assert report.stderr.data == b"b\n\n"
    assert report.stdout.complete and report.stderr.complete
    assert report.stdout.provenance == Provenance.CARRIER_STDOUT
    assert report.stderr.provenance == Provenance.MIXED_STDERR


def test_finite_input_and_eof_are_explicit(wire: MagicMock) -> None:
    execute(CarrierIO(input=FiniteInput(b"encoded payload\n")))
    assert json.loads(wire.call_args_list[0].kwargs["body"]) == {
        "command": ["/bin/true"],
        "input-data": "encoded payload\n",
    }
    wire.reset_mock(side_effect=True)
    wire.side_effect = [{"pid": 42}, {"exited": 1, "exitcode": 0}]
    execute()
    assert json.loads(wire.call_args_list[0].kwargs["body"])["input-data"] == ""


@pytest.mark.parametrize(
    "io", [CarrierIO(sensitive=True), CarrierIO(input=FiniteInput(b"secret-canary", sensitive=True))]
)
def test_sensitive_capture_is_suppressed_without_losing_completion(wire: MagicMock, io: CarrierIO) -> None:
    wire.side_effect = [
        {"pid": 42},
        {"exited": True, "exitcode": 1, "out-data": "secret-canary", "err-data": "secret-canary"},
    ]
    report = execute(io)
    assert report.completion == ExitStatus(code=1)
    assert not report.stdout.data and not report.stderr.data
    assert report.stdout.retention == report.stderr.retention == Retention.SUPPRESSED
    assert "secret-canary" not in repr(report)


def test_discard_remains_distinct_from_suppression(wire: MagicMock) -> None:
    report = execute(CarrierIO(output=Discard()))
    assert report.stdout.retention == report.stderr.retention == Retention.DISCARDED
    assert report.completion == ExitStatus(code=0)


@pytest.mark.parametrize("field", ["out-truncated", "err-truncated"])
def test_provider_truncation_preserves_exit_and_partial_output(wire: MagicMock, field: str) -> None:
    wire.side_effect = [{"pid": 42}, {"exited": 1, "exitcode": 0, "out-data": "out", "err-data": "err", field: 1}]
    report = execute()
    assert report.completion == ExitStatus(code=0)
    assert report.failure == Failure.OUTPUT_LIMIT
    assert report.stdout.data == b"out" and report.stderr.data == b"err"
    assert report.stdout.complete is (field != "out-truncated")
    assert report.stderr.complete is (field != "err-truncated")


def test_capture_limit_is_explicit_without_losing_exit(wire: MagicMock) -> None:
    wire.side_effect = [{"pid": 42}, {"exited": 1, "exitcode": 0, "out-data": "1234"}]
    report = execute(CarrierIO(output=Capture(2)))
    assert report.stdout.data == b"12"
    assert not report.stdout.complete
    assert report.stderr.complete
    assert report.failure == Failure.OUTPUT_LIMIT
    assert report.completion == ExitStatus(code=0)


@pytest.mark.parametrize("fields", [{"out-data": "\u00ff"}, {"err-data": []}, {"out-truncated": "0"}])
def test_invalid_output_does_not_erase_independent_completion(wire: MagicMock, fields: dict) -> None:
    wire.side_effect = [{"pid": 42}, {"exited": 1, "exitcode": 255, **fields}]
    report = execute()
    assert report.completion == ExitStatus(code=255)
    assert report.failure == Failure.INVALID_RESPONSE


@pytest.mark.parametrize(
    "status",
    [
        {},
        {"exited": "1"},
        {"exited": 0, "out-data": "early"},
        {"exited": 1},
        {"exited": 1, "exitcode": True},
        {"exited": 1, "exitcode": -1},
        {"exited": 1, "exitcode": 0, "signal": 9},
        {"exited": 1, "signal": 0},
    ],
)
def test_invalid_status_never_becomes_completion(wire: MagicMock, status: dict) -> None:
    wire.side_effect = [{"pid": 42}, status]
    report = execute()
    assert report.dispatch == Dispatch.SENT
    assert report.completion is None
    assert report.failure == Failure.INVALID_RESPONSE
    assert wire.call_count == 2


def test_signal_is_not_fabricated_exit_code(wire: MagicMock) -> None:
    wire.side_effect = [{"pid": 42}, {"exited": 1, "signal": 15}]
    assert execute().completion == ExitStatus(signal=15)


@pytest.mark.parametrize("response", [{}, {"pid": True}, {"pid": 0}, {"pid": "42"}])
def test_lost_or_invalid_acknowledgement_has_unknown_dispatch(wire: MagicMock, response: dict) -> None:
    wire.side_effect = [response]
    report = execute()
    assert report.dispatch == Dispatch.UNKNOWN
    assert report.failure == Failure.INVALID_RESPONSE
    assert wire.call_count == 1


@pytest.mark.parametrize("phase", ["dispatch", "status"])
def test_failed_observation_never_replays_or_leaks_errors(wire: MagicMock, phase: str) -> None:
    wire.side_effect = ([{"pid": 42}] if phase == "status" else []) + [OSError("secret-canary")]
    report = execute(CarrierIO(sensitive=True))
    assert report.dispatch == (Dispatch.SENT if phase == "status" else Dispatch.UNKNOWN)
    assert report.failure == (Failure.OBSERVATION if phase == "status" else Failure.DISPATCH)
    assert report.completion is None
    assert "secret-canary" not in repr(report)
    assert wire.call_count == (2 if phase == "status" else 1)


def test_expired_deadline_refuses_before_dispatch(wire: MagicMock, clock: Clock) -> None:
    report = execute(deadline=Deadline(clock.now))
    assert report.dispatch == Dispatch.NOT_SENT
    assert report.failure == Failure.DEADLINE
    wire.assert_not_called()


def test_polling_consumes_one_budget_without_cancelling_guest(wire: MagicMock, clock: Clock) -> None:
    wire.side_effect = [{"pid": 42}, {"exited": 0}, {"exited": 0}]
    report = execute(deadline=Deadline(clock.now + 0.15))
    assert report.dispatch == Dispatch.SENT
    assert report.completion is None
    assert report.failure == Failure.DEADLINE
    assert [call.args[0] for call in wire.call_args_list] == ["POST", "GET", "GET"]
    timeouts = [call.kwargs["timeout"] for call in wire.call_args_list]
    assert timeouts[0] == timeouts[1]
    assert timeouts[2] < timeouts[1]


def test_completion_arriving_after_deadline_remains_evidence(wire: MagicMock, clock: Clock) -> None:
    def request(method, suffix, **kwargs):
        if method == "POST":
            return {"pid": 42}
        clock.now += 2
        return {"exited": 1, "exitcode": 1}

    wire.side_effect = request
    report = execute(deadline=Deadline(clock.now + 1))
    assert report.completion == ExitStatus(code=1)
    assert report.failure == Failure.DEADLINE


@pytest.mark.parametrize("phase", ["dispatch", "status"])
@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_interrupt_propagates_to_operation_without_replay(wire: MagicMock, phase: str, interruption: type) -> None:
    wire.side_effect = ([{"pid": 42}] if phase == "status" else []) + [interruption()]
    with pytest.raises(interruption):
        execute()
    assert wire.call_count == (2 if phase == "status" else 1)


@pytest.mark.parametrize("payload", [b"\xff", b"x" * 65_537])
def test_unprepared_or_oversized_input_refused_before_effect(wire: MagicMock, payload: bytes) -> None:
    with pytest.raises(ValidationError):
        execute(CarrierIO(input=FiniteInput(payload)))
    wire.assert_not_called()


@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_complete_http_body_limit_includes_bootstrap_and_framing(wire: MagicMock, extra_bytes: int) -> None:
    argv = ("/usr/bin/python3", "-c", 'print("fixed bootstrap")')
    overhead = len(json.dumps({"command": argv, "input-data": ""}).encode("ascii"))
    payload = b"x" * (65_536 - overhead + extra_bytes)
    carrier = ProxmoxCarrier(connection())
    invocation = PreparedInvocation(argv)
    carrier_io = CarrierIO(input=FiniteInput(payload))
    if extra_bytes:
        with pytest.raises(ValidationError):
            carrier.execute(invocation, io=carrier_io, deadline=Deadline(None))
        wire.assert_not_called()
    else:
        report = carrier.execute(invocation, io=carrier_io, deadline=Deadline(None))
        assert report.completion == ExitStatus(code=0)
        body = wire.call_args_list[0].kwargs["body"]
        assert len(body) == 65_536
        assert json.loads(body)["input-data"].encode("ascii") == payload


@pytest.mark.parametrize(
    "argv,payload",
    [
        (("/bin/true",), b"x" * 65_536),
        (("/bin/true",), b"\n" * 40_000),
        (("/bin/true", "x" * 65_536), b""),
        (("/bin/true", '"' * 40_000), b""),
    ],
)
def test_http_limit_refuses_oversized_argv_or_escaped_input(
    wire: MagicMock, argv: tuple[str, ...], payload: bytes
) -> None:
    with pytest.raises(ValidationError):
        ProxmoxCarrier(connection()).execute(
            PreparedInvocation(argv), io=CarrierIO(input=FiniteInput(payload)), deadline=Deadline(None)
        )
    wire.assert_not_called()


def test_optional_features_are_passively_absent(wire: MagicMock) -> None:
    carrier = ProxmoxCarrier(connection())
    assert not carrier.features.live_stdio
    assert not carrier.features.terminal
    wire.assert_not_called()
