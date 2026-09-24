"""Filesystem boundaries for the private managed-job target store."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from collections.abc import Generator, Iterator
from pathlib import Path

import pytest

from agentworks.execution import _managed_job_wire as wire
from agentworks.execution._managed_job_store import FactName, ManagedJobStore, StoreError, Stream

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="managed job store requires Linux")

RUN = "1" * 32
OTHER_RUN = "2" * 32
SOURCE = Path(__file__).parents[2] / "agentworks" / "execution"


def _launch(run: str = RUN) -> bytes:
    return wire.encode_fact(
        {
            "version": 1,
            "kind": "launch",
            "run_id": run,
            "unit": f"agw-managed-{run}.service",
            "target": {
                "kind": "vm",
                "name": "vm-one",
                "incarnation": f"v1:{'a' * 64}",
                "boot_id": "00000000-0000-4000-8000-000000000001",
            },
            "workload": {"euid": 1001, "egid": 1001, "groups": [1001]},
            "shell": {"requested": None, "resolved_executable": None, "login": False, "interactive": False},
            "owner": {"kind": "resource", "owner_id": "session-7"},
            "lifetime": "independent",
            "profile_revision": 1,
            "receipt_namespace": "agentworks-managed-runs-v1",
            "receipt_protocol_version": 1,
        }
    )


def _end(
    launch: bytes,
    *,
    stream: str = "stdout",
    data: bytes = b"",
    disposition: str = "complete-capture",
    digest: str | None = None,
    length: int | None = None,
) -> bytes:
    value = wire.decode_fact(launch)
    return wire.encode_fact(
        {
            "version": 1,
            "kind": "stream-end",
            "run_id": value["run_id"],
            "unit": value["unit"],
            "receipt_sha256": hashlib.sha256(launch).hexdigest(),
            "stream": stream,
            "retained_bytes": len(data) if length is None else length,
            "retained_sha256": hashlib.sha256(data).hexdigest() if digest is None else digest,
            "disposition": disposition,
        }
    )


@pytest.fixture
def store(tmp_path: Path) -> Generator[ManagedJobStore, None, None]:
    os.chmod(tmp_path, 0o700)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with ManagedJobStore(RUN, _namespace="managed-runs-v1", _owner_uid=os.getuid(), _anchor_fd=fd) as item:
            yield item
    finally:
        os.close(fd)


def _run_path(store: ManagedJobStore, tmp_path: Path) -> Path:
    return tmp_path / "managed-runs-v1" / store.run_id


def test_create_publish_idempotency_and_conflict(store: ManagedJobStore, tmp_path: Path) -> None:
    launch = _launch()
    assert store.read_fact(FactName.LAUNCH) is None
    store.publish_fact(FactName.LAUNCH, launch)
    store.publish_fact(FactName.LAUNCH, launch)
    path = _run_path(store, tmp_path)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE((path / "launch").stat().st_mode) == 0o400
    assert store.read_fact(FactName.LAUNCH) == launch
    alternate = wire.decode_fact(launch)
    alternate["target"]["name"] = "vm-two"  # type: ignore[index]
    with pytest.raises(StoreError):
        store.publish_fact(FactName.LAUNCH, wire.encode_fact(alternate))
    assert store.read_fact(FactName.LAUNCH) == launch


def test_created_objects_get_exact_modes_under_restrictive_umask(store: ManagedJobStore, tmp_path: Path) -> None:
    previous = os.umask(0o777)
    try:
        store.publish_fact(FactName.LAUNCH, _launch())
        store.capture_prefix(Stream.STDOUT, 1, (b"x",))
    finally:
        os.umask(previous)
    path = _run_path(store, tmp_path)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert stat.S_IMODE((path / "launch").stat().st_mode) == 0o400
    assert stat.S_IMODE((path / "stdout").stat().st_mode) == 0o600


def test_orphan_and_partial_stages_are_invisible(store: ManagedJobStore, tmp_path: Path) -> None:
    path = _run_path(store, tmp_path)
    store.publish_fact(FactName.LAUNCH, _launch())
    (path / ".fact-stage-orphan").write_bytes(b"partial")
    os.chmod(path / ".fact-stage-orphan", 0o400)
    assert store.read_fact(FactName.WAIT) is None
    assert store.read_fact(FactName.LAUNCH) == _launch()


def test_crash_time_stage_link_is_accepted_but_extra_links_refuse(store: ManagedJobStore, tmp_path: Path) -> None:
    store.publish_fact(FactName.LAUNCH, _launch())
    path = _run_path(store, tmp_path)
    os.link(path / "launch", path / ".fact-stage-crash")
    assert store.read_fact(FactName.LAUNCH) == _launch()
    os.link(path / "launch", path / ".extra-link")
    with pytest.raises(StoreError):
        store.read_fact(FactName.LAUNCH)


def test_short_writes_complete_and_interrupted_publication_stays_invisible(
    store: ManagedJobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write = os.write
    calls = 0

    def short_write(fd: int, data: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("interrupted")
        return original_write(fd, data[:1])

    monkeypatch.setattr(os, "write", short_write)
    with pytest.raises(OSError):
        store.publish_fact(FactName.LAUNCH, _launch())
    assert store.read_fact(FactName.LAUNCH) is None
    assert any(name.startswith(".fact-stage-") for name in os.listdir(_run_path(store, tmp_path)))
    monkeypatch.setattr(os, "write", lambda fd, data: original_write(fd, data[:1]))
    store.publish_fact(FactName.LAUNCH, _launch())
    assert store.read_fact(FactName.LAUNCH) == _launch()


@pytest.mark.parametrize("leaf", ["symlink", "directory", "fifo", "bad-mode", "bad-link"])
def test_strange_fact_leaf_refuses(store: ManagedJobStore, tmp_path: Path, leaf: str) -> None:
    store.publish_fact(FactName.LAUNCH, _launch())
    path = _run_path(store, tmp_path) / "wait"
    if leaf == "symlink":
        path.symlink_to("launch")
    elif leaf == "directory":
        path.mkdir()
    elif leaf == "fifo":
        os.mkfifo(path)
    else:
        path.write_bytes(b"x")
        os.chmod(path, 0o600 if leaf == "bad-mode" else 0o400)
        if leaf == "bad-link":
            os.link(path, path.parent / ".one")
            os.link(path, path.parent / ".two")
    with pytest.raises(StoreError):
        store.read_fact(FactName.WAIT)


@pytest.mark.parametrize("leaf", ["namespace", "run"])
@pytest.mark.parametrize("condition", ["mode", "symlink", "file"])
def test_unsafe_directories_refuse(store: ManagedJobStore, tmp_path: Path, leaf: str, condition: str) -> None:
    store.publish_fact(FactName.LAUNCH, _launch())
    path = _run_path(store, tmp_path)
    target = path if leaf == "run" else path.parent
    if condition == "mode":
        os.chmod(target, 0o770)
    else:
        # Move the genuine tree aside before replacing only the tested path.
        target.rename(target.with_name(target.name + "-saved"))
        if condition == "symlink":
            target.symlink_to(target.name + "-saved")
        else:
            target.write_bytes(b"x")
    with pytest.raises(StoreError):
        store.read_fact(FactName.LAUNCH)


@pytest.mark.parametrize(
    ("name", "data"),
    [
        (FactName.WAIT, _launch()),
        (FactName.LAUNCH, _launch(OTHER_RUN)),
        (FactName.STDERR_END, _end(_launch(), stream="stdout")),
    ],
)
def test_wrong_fact_identity_refuses(store: ManagedJobStore, name: FactName, data: bytes) -> None:
    with pytest.raises(StoreError):
        store.publish_fact(name, data)


def test_wrong_expected_owner_and_unsafe_anchor_refuse(tmp_path: Path) -> None:
    os.chmod(tmp_path, 0o700)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with pytest.raises(StoreError):
            ManagedJobStore(RUN, _namespace="ns", _owner_uid=os.getuid() + 1, _anchor_fd=fd)
        os.chmod(tmp_path, 0o777)
        with pytest.raises(StoreError):
            ManagedJobStore(RUN, _namespace="ns", _owner_uid=os.getuid(), _anchor_fd=fd)
    finally:
        os.close(fd)


def test_invalid_namespace_and_exact_input_types_refuse(store: ManagedJobStore, tmp_path: Path) -> None:
    with pytest.raises(StoreError):
        ManagedJobStore(RUN, _namespace=str(tmp_path))
    with pytest.raises(StoreError):
        store.read_fact("launch")  # type: ignore[arg-type]
    with pytest.raises(StoreError):
        store.capture_prefix(Stream.STDOUT, True, ())
    with pytest.raises(StoreError):
        store.capture_prefix(Stream.STDOUT, wire.MAX_CAPTURE_PREFIX_BYTES_V1 + 1, ())
    with pytest.raises(StoreError):
        store.publish_fact(FactName.LAUNCH, bytearray(_launch()))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "chunks,limit,expected,omitted",
    [
        ((), 3, b"", False),
        ((b"ab",), 3, b"ab", False),
        ((b"abc",), 3, b"abc", False),
        ((b"ab", b"cdef"), 3, b"abc", True),
        ((b"x",), 0, b"", True),
    ],
)
def test_bounded_prefix_drains_and_reports(
    store: ManagedJobStore, tmp_path: Path, chunks: tuple[bytes, ...], limit: int, expected: bytes, omitted: bool
) -> None:
    seen: list[bytes] = []

    def source() -> Iterator[bytes]:
        for chunk in chunks:
            seen.append(chunk)
            yield chunk

    result = store.capture_prefix(Stream.STDOUT, limit, source())
    assert seen == list(chunks)
    assert result.length == len(expected)
    assert result.sha256 == hashlib.sha256(expected).hexdigest()
    assert result.disposition.value == ("truncated-capture" if omitted else "complete-capture")
    path = _run_path(store, tmp_path) / "stdout"
    assert path.read_bytes() == expected
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_closed_capture_requires_end_and_binding(store: ManagedJobStore) -> None:
    launch = _launch()
    store.publish_fact(FactName.LAUNCH, launch)
    prefix = store.capture_prefix(Stream.STDOUT, 3, (b"abcdef",))
    assert store.read_capture(Stream.STDOUT, launch) is None
    wrong = _end(launch, data=b"abc")
    value = wire.decode_fact(wrong)
    value["receipt_sha256"] = "a" * 64
    store.publish_fact(FactName.STDOUT_END, wire.encode_fact(value))
    with pytest.raises(StoreError):
        store.read_capture(Stream.STDOUT, launch)
    assert prefix.length == 3


@pytest.mark.parametrize("mutation", ["none", "length", "digest", "oversize", "mode", "missing", "symlink", "hardlink"])
def test_closed_capture_checks_exact_spool(store: ManagedJobStore, tmp_path: Path, mutation: str) -> None:
    launch = _launch()
    store.publish_fact(FactName.LAUNCH, launch)
    store.capture_prefix(Stream.STDOUT, 3, (b"abc",))
    end = _end(
        launch,
        data=b"abc",
        length=(
            wire.MAX_CAPTURE_PREFIX_BYTES_V1 + 1 if mutation == "oversize" else 4 if mutation == "length" else None
        ),
        digest="a" * 64 if mutation == "digest" else None,
    )
    store.publish_fact(FactName.STDOUT_END, end)
    path = _run_path(store, tmp_path) / "stdout"
    if mutation == "mode":
        os.chmod(path, 0o644)
    elif mutation == "oversize":
        os.truncate(path, wire.MAX_CAPTURE_PREFIX_BYTES_V1 + 1)
    elif mutation == "missing":
        path.unlink()
    elif mutation == "symlink":
        path.unlink()
        path.symlink_to("launch")
    elif mutation == "hardlink":
        os.link(path, path.parent / ".spool-link")
    if mutation == "none":
        assert store.read_capture(Stream.STDOUT, launch) == b"abc"
    else:
        with pytest.raises(StoreError):
            store.read_capture(Stream.STDOUT, launch)


@pytest.mark.parametrize("disposition", ["discarded", "sensitivity-suppressed"])
def test_noncapture_has_no_spool_and_no_bytes(store: ManagedJobStore, tmp_path: Path, disposition: str) -> None:
    launch = _launch()
    store.publish_fact(FactName.LAUNCH, launch)
    store.publish_fact(FactName.STDOUT_END, _end(launch, disposition=disposition))
    assert store.read_capture(Stream.STDOUT, launch) is None
    path = _run_path(store, tmp_path) / "stdout"
    assert not path.exists()
    path.write_bytes(b"")
    os.chmod(path, 0o600)
    with pytest.raises(StoreError):
        store.read_capture(Stream.STDOUT, launch)


@pytest.mark.parametrize("interpreter", [Path(sys.executable), Path("/usr/bin/python3.11")])
def test_exact_source_bundle_without_installed_agentworks(interpreter: Path, tmp_path: Path) -> None:
    if not interpreter.is_file():
        pytest.skip("Python 3.11 is unavailable")
    from agentworks.execution._helper_bundle import build_helper_modules

    script = (
        build_helper_modules("_agw_store", ("_helper_identity", "_managed_job_wire", "_managed_job_store"))
        + """
import os
import sys
from pathlib import Path
assert 'agentworks' not in sys.modules
store_module = sys.modules['_agw_store._managed_job_store']
wire = sys.modules['_agw_store._managed_job_wire']
anchor = Path(sys.argv[1])
fd = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    with store_module.ManagedJobStore('1' * 32, _namespace='ns', _owner_uid=os.getuid(), _anchor_fd=fd) as store:
        assert store.read_fact(store_module.FactName.LAUNCH) is None
        launch = bytes.fromhex(sys.argv[2])
        store.publish_fact(store_module.FactName.LAUNCH, launch)
        assert store.read_fact(store_module.FactName.LAUNCH) == launch
finally:
    os.close(fd)
assert 'agentworks' not in sys.modules
"""
    )
    os.chmod(tmp_path, 0o700)
    result = subprocess.run(
        [str(interpreter), "-I", "-S", "-B", "-c", script, str(tmp_path), _launch().hex()],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
