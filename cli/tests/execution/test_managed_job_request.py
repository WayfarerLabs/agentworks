"""Canonical request bytes and protected fixed asset publication."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agentworks.execution import _managed_job_request as request_wire
from agentworks.execution import _managed_job_wire as wire
from agentworks.execution._managed_job_store import ManagedJobStore, RequestAsset, StoreError

from .test_managed_job_store import RUN, _launch


def _request(*, kind: str = "command", source: bytes = b"") -> request_wire.ManagedJobRequest:
    launch = _launch()
    if kind == "script":
        value = wire.decode_fact(launch)
        value["shell"] = {"requested": "sh", "resolved_executable": "/bin/sh", "login": True, "interactive": False}
        launch = wire.encode_fact(value)
    return request_wire.ManagedJobRequest(
        launch,
        kind,
        ("/usr/bin/printf", "", "hello") if kind == "command" else (),
        "/tmp",
        "capture",
        4096,
        (("LANG", "C.UTF-8"),),
        source,
        b"\x00stdin",
    )


def test_round_trip_command_and_login_script() -> None:
    for request in (_request(), _request(kind="script", source="é\n".encode())):
        assets = request_wire.encode_request(request)
        assert set(assets) == {item.value for item in RequestAsset}
        assert request_wire.decode_request(assets) == request
        assert all(data == request_wire.encode_request(request)[name] for name, data in assets.items())


def test_request_representation_does_not_expose_caller_material() -> None:
    secret = "secret-canary-71"
    request = replace(
        _request(kind="script", source=secret.encode()),
        environment=(("SECRET", secret),),
        stdin=secret.encode(),
    )
    assert secret not in repr(request)


def test_invalid_source_suppresses_low_level_exception() -> None:
    with pytest.raises(request_wire.RequestError) as caught:
        request_wire.encode_request(_request(kind="script", source=b"\xff"))
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "change",
    [
        {"argv": ()},
        {"argv": ("",)},
        {"argv": ("/bin/true", "\0")},
        {"argv": ("/bin/true", "\ud800")},
        {"cwd": "relative"},
        {"cwd": "/a/../b"},
        {"output_mode": "terminal"},
        {"capture_prefix_bytes": True},
        {"capture_prefix_bytes": request_wire.MAX_CAPTURE_PREFIX_BYTES + 1},
        {"environment": (("_agw_internal", "x"),)},
        {"environment": (("A", "\ud800"),)},
        {"source": b"x"},
        {"stdin": b"x" * (request_wire.MAX_STDIN_BYTES + 1)},
    ],
)
def test_reject_invalid_request_shapes(change: dict[str, object]) -> None:
    with pytest.raises(request_wire.RequestError):
        request_wire.encode_request(replace(_request(), **change))


@pytest.mark.parametrize("source", [b"\xff", b"a\0b", b"\xed\xa0\x80"])
def test_script_source_is_valid_text(source: bytes) -> None:
    with pytest.raises(request_wire.RequestError):
        request_wire.encode_request(_request(kind="script", source=source))


def test_source_larger_than_one_mib_is_accepted() -> None:
    request = _request(kind="script", source=b"x" * (1048576 + 1))
    assert request_wire.decode_request(request_wire.encode_request(request)) == request


def test_launch_refuses_operation_and_interactive() -> None:
    for change in (
        {"owner": {"kind": "operation", "owner_id": RUN}, "lifetime": "operation"},
        {"shell": {"requested": "sh", "resolved_executable": "/bin/sh", "login": False, "interactive": True}},
    ):
        launch = wire.decode_fact(_request(kind="script").launch)
        launch.update(change)
        with pytest.raises(request_wire.RequestError):
            request_wire.encode_request(replace(_request(kind="script"), launch=wire.encode_fact(launch)))


def test_request_bindings_and_canonical_bytes_refuse() -> None:
    assets = request_wire.encode_request(_request())
    mutations = [
        {**assets, "request-stdin": b"different"},
        {**assets, "request-environment": b' {"values":{},"version":1}'},
        {**assets, "request-control": assets["request-control"] + b" "},
        {**assets, "request-source": b"x" * (request_wire.MAX_SOURCE_BYTES + 1)},
    ]
    for changed in mutations:
        with pytest.raises(request_wire.RequestError):
            request_wire.decode_request(changed)
    control = request_wire.decode_control(assets["request-control"])
    control["stdin"] = {"bytes": len(assets["request-stdin"]), "sha256": hashlib.sha256(b"wrong").hexdigest()}
    with pytest.raises(request_wire.RequestError):
        request_wire.decode_request({**assets, "request-control": request_wire._json(control)})  # noqa: SLF001
    with pytest.raises(request_wire.RequestError):
        request_wire.decode_request({"request-launch": assets["request-launch"]})


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": "2" * 32},
        {"version": 2},
        {"output": {"mode": "terminal", "prefix_bytes": None}},
        {"output": {"mode": "discard", "prefix_bytes": 1}},
        {"argv": ["/bin/true", "\0"]},
        {"cwd": "/tmp/../other"},
        {"source": {"bytes": 1, "sha256": hashlib.sha256(b"").hexdigest()}},
    ],
)
def test_control_mismatch_refuses(change: dict[str, object]) -> None:
    assets = request_wire.encode_request(_request())
    control = request_wire.decode_control(assets["request-control"])
    control.update(change)
    with pytest.raises(request_wire.RequestError):
        request_wire.decode_request({**assets, "request-control": request_wire._json(control)})  # noqa: SLF001


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", 123),
        ("kind", "future"),
        ("argv", "not-a-list"),
        ("argv", []),
        ("argv", ["/bin/true"] * (request_wire.MAX_ARGV + 1)),
        ("argv", ["/bin/true", "\0"]),
        ("argv", ["/bin/true", "x" * (request_wire.MAX_TEXT_BYTES + 1)]),
        ("cwd", "relative"),
        ("cwd", "/a/../b"),
        ("output", {"mode": "discard"}),
        ("output", {"mode": "discard", "prefix_bytes": None, "extra": 1}),
        ("output", {"mode": "terminal", "prefix_bytes": None}),
        ("output", {"mode": "capture", "prefix_bytes": True}),
        ("output", {"mode": "capture", "prefix_bytes": request_wire.MAX_CAPTURE_PREFIX_BYTES + 1}),
        ("output", {"mode": "discard", "prefix_bytes": 1}),
        ("environment", {"bytes": True, "sha256": "a" * 64}),
        ("environment", {"bytes": request_wire.MAX_ENVIRONMENT_BYTES + 1, "sha256": "a" * 64}),
        ("source", {"bytes": -1, "sha256": "a" * 64}),
        ("source", {"bytes": request_wire.MAX_SOURCE_BYTES + 1, "sha256": "a" * 64}),
        ("source", {"bytes": 1, "sha256": hashlib.sha256(b"x").hexdigest()}),
        ("stdin", {"bytes": request_wire.MAX_STDIN_BYTES + 1, "sha256": "a" * 64}),
        ("stdin", {"bytes": 0, "sha256": "A" * 64}),
        ("stdin", {"bytes": 0, "sha256": "a" * 63}),
        ("stdin", {"bytes": 0, "sha256": "a" * 64, "extra": 1}),
    ],
)
def test_standalone_control_rejects_canonical_semantic_fault(field: str, value: object) -> None:
    assets = request_wire.encode_request(_request())
    control = request_wire.decode_control(assets["request-control"])
    control[field] = value
    with pytest.raises(request_wire.RequestError):
        request_wire.decode_control(request_wire._json(control))  # noqa: SLF001


def test_standalone_control_accepts_valid_script_shape() -> None:
    assets = request_wire.encode_request(_request(kind="script", source=b"echo ok"))
    assert request_wire.decode_control(assets["request-control"])["kind"] == "script"
    control = request_wire.decode_control(assets["request-control"])
    control["argv"] = ["/bin/sh"]
    with pytest.raises(request_wire.RequestError):
        request_wire.decode_control(request_wire._json(control))  # noqa: SLF001


@pytest.mark.parametrize("python", [sys.executable, "/usr/bin/python3.11"])
def test_exact_source_is_portable_without_installed_package(tmp_path: Path, python: str) -> None:
    if not Path(python).exists():
        pytest.skip(f"{python} unavailable")
    package = tmp_path / "portable"
    package.mkdir()
    (package / "__init__.py").touch()
    source = Path(request_wire.__file__).parent
    for module in ("_managed_job_request", "_managed_job_wire", "_helper_identity"):
        shutil.copyfile(source / f"{module}.py", package / f"{module}.py")
    control = request_wire.encode_request(_request())["request-control"]
    code = (
        "from portable._managed_job_request import *; "
        "assert decode_environment(encode_environment((('A', 'é'),))) == (('A', 'é'),); "
        "assert MAX_SOURCE_BYTES == 16777216; "
        f"assert decode_control(bytes.fromhex({control.hex()!r}))['kind'] == 'command'"
    )
    subprocess.run([python, "-I", "-c", f"import sys; sys.path.insert(0, {str(tmp_path)!r}); {code}"], check=True)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux protected store")
def test_fixed_store_partial_idempotence_and_conflict(tmp_path: Path) -> None:
    os.chmod(tmp_path, 0o700)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with ManagedJobStore(RUN, _namespace="managed-runs-v1", _owner_uid=os.getuid(), _anchor_fd=fd) as store:
            assert store.read_request() is None
            assets = request_wire.encode_request(_request())
            store.publish_request_asset(RequestAsset.LAUNCH, assets[RequestAsset.LAUNCH.value])
            with pytest.raises(StoreError):
                store.read_request()
            store.publish_request(_request())
            store.publish_request(_request())
            assert store.read_request() == _request()
            path = tmp_path / "managed-runs-v1" / RUN
            assert all(stat.S_IMODE((path / name.value).stat().st_mode) == 0o400 for name in RequestAsset)
            with pytest.raises(StoreError):
                store.publish_request_asset(RequestAsset.STDIN, b"other")
    finally:
        os.close(fd)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux protected store")
def test_fixed_store_reconciles_private_stage_link_window(tmp_path: Path) -> None:
    os.chmod(tmp_path, 0o700)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with ManagedJobStore(RUN, _namespace="managed-runs-v1", _owner_uid=os.getuid(), _anchor_fd=fd) as store:
            request = _request()
            store.publish_request(request)
            path = tmp_path / "managed-runs-v1" / RUN / RequestAsset.STDIN.value
            stage = path.with_name(".request-stage-" + "a" * 32)
            os.link(path, stage)

            assert path.stat().st_nlink == 2
            assert store.read_request_asset(RequestAsset.STDIN) == request.stdin
            store.publish_request_asset(RequestAsset.STDIN, request.stdin)

            stage.unlink()
            assert store.read_request_asset(RequestAsset.STDIN) == request.stdin
    finally:
        os.close(fd)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux protected store")
@pytest.mark.parametrize("strange", ["symlink", "hardlink", "directory", "fifo", "mode"])
def test_fixed_store_refuses_unsafe_leaf(tmp_path: Path, strange: str) -> None:
    os.chmod(tmp_path, 0o700)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with ManagedJobStore(RUN, _namespace="managed-runs-v1", _owner_uid=os.getuid(), _anchor_fd=fd) as store:
            store.publish_request_asset(RequestAsset.LAUNCH, _request().launch)
            path = tmp_path / "managed-runs-v1" / RUN / RequestAsset.STDIN.value
            if strange == "symlink":
                path.symlink_to(RequestAsset.LAUNCH.value)
            elif strange == "directory":
                path.mkdir()
            elif strange == "fifo":
                os.mkfifo(path)
            else:
                path.write_bytes(b"x")
                os.chmod(path, 0o400 if strange == "hardlink" else 0o600)
                if strange == "hardlink":
                    os.link(path, path.with_name("other"))
            with pytest.raises(StoreError):
                store.read_request_asset(RequestAsset.STDIN)
    finally:
        os.close(fd)
