"""Protected live direction metadata, independent of endpoint or host wiring."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from agentworks.execution import _managed_job_request as requests
from agentworks.execution import _managed_job_wire as wire
from agentworks.execution import _managed_lease_wire as leases
from agentworks.execution._helper_bundle import build_helper_modules

from .test_managed_job_request import _request

NONCE = "0123456789abcdef" * 2


def _operation() -> requests.ManagedJobRequest:
    value = wire.decode_fact(_request().launch)
    value.update(owner={"kind": "operation", "owner_id": "c" * 32}, lifetime="operation")
    launch = wire.encode_fact(value)
    return replace(_request(), launch=launch, operation_lease=leases.sampled_lease(launch, 123))


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _live(*, input: bool = True, output: bool = False) -> requests.ManagedJobRequest:
    return replace(
        _operation(),
        live_io=requests.ManagedLiveIO(NONCE, input, output),
        stdin=b"" if input else b"\x00finite\xff",
        output_mode="discard" if output else "capture",
        capture_prefix_bytes=None if output else 4096,
    )


@pytest.mark.parametrize(("input", "output"), [(True, False), (False, True), (True, True)])
def test_canonical_live_round_trip_preserves_independent_directions(input: bool, output: bool) -> None:
    request = _live(input=input, output=output)
    assets = requests.encode_request(request)
    control = requests.decode_control(assets["request-control"])
    assert control["live_io"] == {"nonce": NONCE, "input": input, "output": output}
    assert assets["request-control"] == _json(control)
    assert set(assets) == set(requests.encode_request(_request()))
    assert requests.decode_request(assets) == request


@pytest.mark.parametrize("mode", ["capture", "discard", "sensitivity-suppressed"])
def test_live_input_accepts_each_durable_output_policy(mode: str) -> None:
    request = replace(_live(), output_mode=mode, capture_prefix_bytes=0 if mode == "capture" else None)
    assert requests.decode_request(requests.encode_request(request)) == request


def test_live_metadata_is_frozen_and_representation_safe() -> None:
    live_io = requests.ManagedLiveIO(NONCE, True, False)
    with pytest.raises(FrozenInstanceError):
        live_io.output = True  # type: ignore[misc]
    assert NONCE not in repr(live_io)
    assert NONCE not in repr(_live())


@pytest.mark.parametrize("nonce", [None, 1, b"a" * 32, "", "a" * 31, "a" * 33, "A" * 32, "g" * 32, "a" * 32 + "\n"])
def test_live_nonce_boundary_refuses(nonce: object) -> None:
    with pytest.raises(requests.RequestError):
        requests.ManagedLiveIO(nonce, True, False)


@pytest.mark.parametrize(("input", "output"), [(False, False), (1, False), (True, 0), (None, True), ("yes", True)])
def test_live_direction_boundary_refuses(input: object, output: object) -> None:
    with pytest.raises(requests.RequestError):
        requests.ManagedLiveIO(NONCE, input, output)


@pytest.mark.parametrize("value", [True, {}, {"nonce": NONCE, "input": True, "output": False}, "live"])
def test_caller_requires_live_value_type(value: object) -> None:
    with pytest.raises(requests.RequestError):
        requests.encode_request(replace(_live(), live_io=value))


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"nonce": NONCE, "input": True},
        {"nonce": NONCE, "input": True, "output": False, "endpoint": "/tmp/socket"},
        {"nonce": NONCE, "input": False, "output": False},
        {"nonce": NONCE, "input": 1, "output": False},
        {"nonce": "A" * 32, "input": True, "output": False},
    ],
)
def test_external_live_shape_refuses(value: object) -> None:
    assets = requests.encode_request(_live())
    control = requests.decode_control(assets["request-control"])
    control["live_io"] = value
    with pytest.raises(requests.RequestError):
        requests.decode_control(_json(control))
    with pytest.raises(requests.RequestError):
        requests.decode_request({**assets, "request-control": _json(control)})


@pytest.mark.parametrize("mutation", ["duplicate", "spacing", "order"])
def test_noncanonical_live_control_refuses(mutation: str) -> None:
    assets = requests.encode_request(_live())
    original = assets["request-control"]
    if mutation == "duplicate":
        control = original.replace(b'"input":true', b'"input":true,"input":true')
    elif mutation == "spacing":
        control = original.replace(b'"input":true', b'"input": true')
    else:
        control = original.replace(
            b'"input":true,"nonce":"' + NONCE.encode() + b'"', b'"nonce":"' + NONCE.encode() + b'","input":true'
        )
    assert control != original
    with pytest.raises(requests.RequestError):
        requests.decode_control(control)


@pytest.mark.parametrize(
    "change",
    [
        {"stdin": b"finite"},
        {"launch": _request().launch, "operation_lease": None},
        {"operation_lease": None},
        {"operation_lease": False},
        {"operation_lease": replace(leases.sampled_lease(_operation().launch, 123), run_id="b" * 32)},
        {"operation_lease": replace(leases.sampled_lease(_operation().launch, 123), expires_ns=0)},
    ],
)
def test_live_caller_binding_refuses(change: dict[str, object]) -> None:
    with pytest.raises(requests.RequestError):
        requests.encode_request(replace(_live(), **change))


@pytest.mark.parametrize(("mode", "prefix"), [("capture", 100), ("sensitivity-suppressed", None), ("discard", 0)])
def test_live_output_refuses_retention_conflict(mode: str, prefix: int | None) -> None:
    with pytest.raises(requests.RequestError):
        requests.encode_request(replace(_live(output=True), output_mode=mode, capture_prefix_bytes=prefix))


@pytest.mark.parametrize("mutation", ["stdin", "output", "independent", "lease", "missing-lease"])
def test_decoded_live_binding_refuses(mutation: str) -> None:
    assets = requests.encode_request(_live(output=True))
    control = requests.decode_control(assets["request-control"])
    if mutation == "stdin":
        finite = requests.encode_request(replace(_operation(), stdin=b"finite"))
        assets["request-stdin"] = finite["request-stdin"]
        control["stdin"] = requests.decode_control(finite["request-control"])["stdin"]
    elif mutation == "output":
        control["output"] = {"mode": "capture", "prefix_bytes": 1}
    elif mutation == "independent":
        assets["request-launch"] = _request().launch
        del control["operation_lease"]
    elif mutation == "missing-lease":
        del control["operation_lease"]
    else:
        control["operation_lease"] = json.loads(
            leases.encode_lease(replace(leases.sampled_lease(_operation().launch, 123), run_id="b" * 32))
        )
    assets["request-control"] = _json(control)
    if mutation in ("stdin", "output"):
        with pytest.raises(requests.RequestError):
            requests.decode_control(assets["request-control"])
    else:
        requests.decode_control(assets["request-control"])
    with pytest.raises(requests.RequestError):
        requests.decode_request(assets)


def test_live_body_preparation_allows_only_missing_lease() -> None:
    body = replace(_live(), operation_lease=None)
    assets = requests._encode_request_assets(body, body_only=True)  # noqa: SLF001
    assert "operation_lease" not in requests.decode_control(assets["request-control"])
    with pytest.raises(requests.RequestError):
        requests.decode_request(assets)
    for change in ({"launch": _request().launch}, {"stdin": b"finite"}, {"operation_lease": False}):
        with pytest.raises(requests.RequestError):
            requests._encode_request_assets(replace(body, **change), body_only=True)  # noqa: SLF001


def test_nonlive_control_omits_optional_metadata() -> None:
    for request in (_request(), _operation()):
        assets = requests.encode_request(request)
        assert "live_io" not in requests.decode_control(assets["request-control"])
        assert requests.decode_request(assets) == request
        assert requests.decode_request(assets).live_io is None


def test_live_control_bound_accounts_for_metadata() -> None:
    request = replace(_live(output=True), argv=("/bin/true",) + ("x" * 4000,) * 7 + ("",))
    remaining = requests.MAX_CONTROL_BYTES - len(requests.encode_request(request)["request-control"])
    assert 0 < remaining <= requests.MAX_TEXT_BYTES
    exact = replace(request, argv=request.argv[:-1] + ("x" * remaining,))
    assets = requests.encode_request(exact)
    assert len(assets["request-control"]) == requests.MAX_CONTROL_BYTES
    assert requests.decode_request(assets) == exact
    with pytest.raises(requests.RequestError):
        requests.encode_request(replace(exact, argv=request.argv[:-1] + ("x" * (remaining + 1),)))


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
def test_packed_live_codec_round_trip_without_installed_package(interpreter: str, tmp_path: Path) -> None:
    if not Path(interpreter).exists():
        pytest.skip("interpreter unavailable")
    source = build_helper_modules(
        "_agw_live_request",
        ("_helper_identity", "_managed_job_wire", "_managed_lease_wire", "_managed_job_request"),
    )
    source += (
        "m=sys.modules['_agw_live_request._managed_job_request']\n"
        "assets={k:bytes.fromhex(v) for k,v in json.loads(sys.argv[1]).items()}\n"
        "request=m.decode_request(assets)\n"
        "assert request.live_io.input and request.live_io.output\n"
        "assert m.encode_request(request)==assets\n"
    )
    data = json.dumps({name: data.hex() for name, data in requests.encode_request(_live(output=True)).items()})
    result = subprocess.run(
        [interpreter, "-I", "-S", "-B", "-c", "import json\n" + source, data],
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout == b""
