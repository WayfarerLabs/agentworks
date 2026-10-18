"""Immutable v2 preparation and one fixed-width late lease control."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from agentworks.execution import _managed_job_request as jobs
from agentworks.execution import _managed_job_wire as wire
from agentworks.execution import _managed_lease_wire as leases
from agentworks.execution import _managed_start_protocol as start
from agentworks.execution._helper_bundle import build_helper_modules
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_start_bundle import FIXED_BUNDLE
from agentworks.execution._runtime_prerequisite import (
    RuntimeSelection,
    RuntimeTargetOS,
    build_runtime_identity_helper_argv,
)
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity
from agentworks.execution.carrier import CarrierIO, FiniteInput, PreparedInvocation
from agentworks.execution.carriers.proxmox import ProxmoxCarrier

from .test_managed_job_request import _request as _job

NONCE = "b" * 32
ROOT = IdentityExpectation(0, 0, (0,))
GUEST = VMGuestIdentity("d" * 32, "00000000-0000-4000-8000-000000000001", 1234)
SAMPLES = (0, 1, 999999999, 1000000000, 10**18, leases.MAX_CLOCK_NS - leases.WINDOW_NS)


def _request(*, operation: bool = True, sample: int | None = None) -> start.ManagedStartRequest:
    job = _job()
    if operation:
        launch = wire.decode_fact(job.launch)
        launch.update(owner={"kind": "operation", "owner_id": "c" * 32}, lifetime="operation")
        job = replace(job, launch=wire.encode_fact(launch))
    if sample is not None:
        job = replace(job, operation_lease=leases.sampled_lease(job.launch, sample))
    return start.ManagedStartRequest(NONCE, ROOT, job, GUEST)


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _control(body: bytes) -> dict[str, object]:
    return cast("dict[str, object]", json.loads(base64.b64decode(json.loads(body)["assets"][1])))


def _replace_control(body: bytes, control: dict[str, object]) -> bytes:
    header = json.loads(body)
    header["assets"][1] = base64.b64encode(_json(control)).decode("ascii")
    return _json(header)


def test_body_stays_identical_across_fresh_samples_and_body_only_preparation() -> None:
    preparation = _request()
    body = start.encode_body(preparation)
    assert json.loads(body)["version"] == 2
    assert "operation_lease" not in _control(body)
    for sample in SAMPLES:
        request = _request(sample=sample)
        data = start.encode_request(request)
        assert data[: -leases.MAX_LEASE_BYTES] == body
        assert start.decode_request(data) == request
        assert len(data) == len(body) + leases.MAX_LEASE_BYTES
        with pytest.raises(start.ManagedStartError):
            start.encode_body(request)
    with pytest.raises(start.ManagedStartError):
        start.encode_request(preparation)
    with pytest.raises(start.ManagedStartError):
        start.decode_request(body)


def test_tail_is_existing_lease_json_with_ascii_space_padding() -> None:
    for sample in SAMPLES:
        lease = _request(sample=sample).job.operation_lease
        assert lease is not None
        tail = start.encode_tail(lease)
        encoded = leases.encode_lease(lease)
        assert tail == encoded + b" " * (leases.MAX_LEASE_BYTES - len(encoded))
        assert len(tail) == leases.MAX_LEASE_BYTES
    assert start.encode_tail(None) == b"null" + b" " * (leases.MAX_LEASE_BYTES - 4)


def test_complete_proxmox_body_length_stays_fixed_across_clock_digit_changes() -> None:
    argv, _, _ = build_runtime_identity_helper_argv(
        IdentityPlan(ROOT, IdentityMode.SUDO_ROOT),
        selection=RuntimeSelection(RuntimeTargetOS.LINUX),
        fixed_source=FIXED_BUNDLE.bootstrap,
        nonce=NONCE,
    )
    sizes = set()
    for sample in SAMPLES:
        data = start.encode_request(_request(sample=sample))
        body = ProxmoxCarrier._request_body(
            PreparedInvocation(argv), CarrierIO(input=FiniteInput(FIXED_BUNDLE.prefix + data))
        )
        sizes.add(len(body))
    assert len(sizes) == 1


def test_independent_body_and_null_tail_roundtrip() -> None:
    request = _request(operation=False)
    data = start.encode_request(request)
    assert data == start.encode_body(request) + start.encode_tail(None)
    assert start.decode_request(data) == request
    assert jobs.decode_control(base64.b64decode(json.loads(data[:-512])["assets"][1]))["version"] == 1


def test_binary_stdin_and_utf8_script_remain_distinct_assets() -> None:
    request = _request(operation=False)
    script = _job(kind="script", source="printf 'é'\n".encode())
    request = replace(request, job=replace(script, stdin=bytes(range(256)) * 100))
    decoded = start.decode_request(start.encode_request(request))
    assert decoded.job.source == script.source
    assert decoded.job.stdin == bytes(range(256)) * 100
    assert decoded.job.source != decoded.job.stdin


def test_body_reserves_the_existing_full_control_headroom_before_effects() -> None:
    request = _request()
    argv = ("/bin/true",) + ("x" * 4000,) * 7 + ("",)
    request = replace(request, job=replace(request.job, argv=argv))
    size = len(jobs._encode_request_assets(request.job, body_only=True)["request-control"])
    remaining = jobs.MAX_CONTROL_BYTES - jobs.OPERATION_LEASE_CONTROL_HEADROOM - size
    assert 0 < remaining <= jobs.MAX_TEXT_BYTES
    exact = replace(request, job=replace(request.job, argv=argv[:-1] + ("x" * remaining,)))
    body = start.encode_body(exact)
    assert len(_json(_control(body))) + jobs.OPERATION_LEASE_CONTROL_HEADROOM == jobs.MAX_CONTROL_BYTES
    lease = leases.sampled_lease(exact.job.launch, SAMPLES[-1])
    assert start.decode_request(body + start.encode_tail(lease)).job == replace(exact.job, operation_lease=lease)
    over = replace(exact, job=replace(exact.job, argv=argv[:-1] + ("x" * (remaining + 1),)))
    # This complete sample fits, but cannot weaken the existing preparation reserve.
    jobs.encode_request(replace(over.job, operation_lease=lease))
    with pytest.raises(start.ManagedStartError):
        start.encode_body(over)
    with pytest.raises(start.ManagedStartError):
        start.encode_request(replace(over, job=replace(over.job, operation_lease=lease)))
    control = _control(body)
    control["argv"] = list(over.job.argv)
    with pytest.raises(start.ManagedStartError):
        start.decode_request(_replace_control(body, control) + start.encode_tail(lease))


@pytest.mark.parametrize(
    "variant",
    [
        "missing",
        "short",
        "long",
        "newline",
        "tab",
        "leading",
        "interior",
        "extra",
        "noncanonical",
        "duplicate",
        "old-version",
    ],
)
def test_malformed_body_or_fixed_tail_refuses(variant: str) -> None:
    request = _request(sample=1000)
    data = start.encode_request(request)
    body, tail = data[:-512], data[-512:]
    if variant == "missing":
        data = body
    elif variant == "short":
        data = data[:-1]
    elif variant == "long":
        data += b" "
    elif variant == "newline":
        data = data[:-1] + b"\n"
    elif variant == "tab":
        data = data[:-1] + b"\t"
    elif variant == "leading":
        data = body + b" " + tail[:-1]
    elif variant == "interior":
        data = body + tail.replace(b'"version":1', b'"version": 1')[:-1]
    elif variant == "extra":
        data = body + (tail.rstrip() + b"null").ljust(512, b" ")
    elif variant == "noncanonical":
        data = b" " + data
    elif variant == "duplicate":
        data = body.replace(b'"version":2', b'"version":2,"version":2') + tail
    else:
        data = body.replace(b'"version":2', b'"version":1') + tail
    with pytest.raises(start.ManagedStartError):
        start.decode_request(data)


@pytest.mark.parametrize(
    "variant",
    [
        "missing-op-lease",
        "independent-lease",
        "wrong-run",
        "wrong-hash",
        "wrong-boot",
        "duplicate-home",
        "invalid-expiry",
    ],
)
def test_tail_must_bind_exact_lifetime_and_launch(variant: str) -> None:
    request = _request(sample=1000)
    lease = request.job.operation_lease
    assert lease is not None
    body = start.encode_body(replace(request, job=replace(request.job, operation_lease=None)))
    tail = start.encode_tail(lease)
    if variant == "missing-op-lease":
        tail = start.encode_tail(None)
    elif variant == "independent-lease":
        body = start.encode_body(_request(operation=False))
    elif variant == "wrong-run":
        tail = start.encode_tail(replace(lease, run_id="e" * 32))
    elif variant == "wrong-hash":
        tail = start.encode_tail(replace(lease, receipt_sha256="f" * 64))
    elif variant == "wrong-boot":
        tail = start.encode_tail(replace(lease, boot_id="00000000-0000-4000-8000-000000000002"))
    elif variant == "duplicate-home":
        control = _control(body)
        control["operation_lease"] = json.loads(leases.encode_lease(lease))
        body = _replace_control(body, control)
    else:
        value = json.loads(leases.encode_lease(lease))
        value["expires_ns"] += 1
        tail = _json(value).ljust(512, b" ")
    with pytest.raises(start.ManagedStartError):
        start.decode_request(body + tail)


@pytest.mark.parametrize("value", [False, {}, b"null", "lease", 1])
def test_tail_encoding_rejects_nonlease_types(value: object) -> None:
    with pytest.raises(start.ManagedStartError):
        start.encode_tail(value)


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
def test_python311_packed_body_tail_codec_and_complete_hash(interpreter: str, tmp_path: Path) -> None:
    if not Path(interpreter).is_file():
        pytest.skip("interpreter unavailable")
    request = _request(sample=SAMPLES[-1])
    request = replace(request, job=replace(request.job, stdin=bytes(range(256)) * 8))
    data = start.encode_request(request)
    source = build_helper_modules(
        "_tail_parity",
        (
            "_helper_identity",
            "_managed_job_wire",
            "_managed_lease_wire",
            "_managed_job_request",
            "_managed_job_store",
            "_file_wire",
            "_vm_guest_identity_protocol",
            "_managed_start_protocol",
        ),
    )
    source += (
        "import hashlib\n"
        "p=sys.modules['_tail_parity._managed_start_protocol']\n"
        "data=bytes.fromhex(sys.argv[1])\n"
        "request=p.decode_request(data)\n"
        "assert request.job.stdin==bytes(range(256))*8\n"
        "assert p.encode_request(request)==data\n"
        "bulk=data[:-512]\n"
        "tail=p.encode_tail(request.job.operation_lease)\n"
        "digest=hashlib.sha256(bulk)\n"
        "digest.update(tail)\n"
        "assert digest.digest()==hashlib.sha256(data).digest()\n"
    )
    result = subprocess.run(
        [interpreter, "-I", "-S", "-B", "-c", source, data.hex()],
        cwd=tmp_path,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == b""
    assert (
        hashlib.sha256(data[:-512] + start.encode_tail(request.job.operation_lease)).digest()
        == hashlib.sha256(data).digest()
    )
