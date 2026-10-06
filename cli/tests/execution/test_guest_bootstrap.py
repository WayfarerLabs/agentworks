"""Synthetic checks for the fixed Linux two-phase guest bootstrap."""

from __future__ import annotations

import builtins
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import _guest_bootstrap as bootstrap
from agentworks.execution._helper_bundle import RootGuestDelivery, _build_root_guest_program, build_root_guest_program
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import (
    RuntimeSelection,
    RuntimeTargetOS,
    build_root_guest_bootstrap_argv,
)
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity

_NONCE = "a" * 32
_ROOT = IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.DIRECT)
_TARGET = IdentityExpectation(1001, 1001, (1001,))
_GUEST = VMGuestIdentity("a" * 32, "123e4567-e89b-12d3-a456-426614174000", 1234)
_EXPECTED = (_GUEST.instance_marker, _GUEST.boot_id, _GUEST.init_start_ticks)


def _synthetic_loader(identity: tuple[str, str, int] = _EXPECTED, *, body: str = "return 0") -> str:
    body_source = "".join(f" {line}\n" for line in body.splitlines())
    return (
        "import builtins,sys,types\n"
        "_agw_guest_module='_agw_test._vm_guest_identity_guest'\n"
        "def _agw_load_identity():\n"
        " builtins._agw_bootstrap_events.append('identity')\n"
        " m=types.ModuleType(_agw_guest_module)\n"
        " m._bind_init_reader=lambda reader:builtins._agw_bootstrap_events.append('bind')\n"
        f" m._identity=lambda:types.SimpleNamespace(instance_marker={identity[0]!r},"
        f"boot_id={identity[1]!r},init_start_ticks={identity[2]!r})\n"
        " sys.modules[_agw_guest_module]=m\n"
        "def _agw_load_remaining():builtins._agw_bootstrap_events.append('remaining')\n"
        "def _agw_enter_body():\n"
        " builtins._agw_bootstrap_events.append('body')\n" + body_source
    )


def _prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: list[str]) -> list[int]:
    stat_path = tmp_path / "stat"
    stat_path.write_bytes(b"stat")
    monkeypatch.setattr(bootstrap, "_INIT_PATH", str(stat_path))
    monkeypatch.setattr(os, "getresuid", lambda: (0, 0, 0))
    monkeypatch.setattr(bootstrap, "_drop_and_verify", lambda *_args: events.append("drop"))
    monkeypatch.setattr(builtins, "_agw_bootstrap_events", events, raising=False)
    opened: list[int] = []
    real_open = os.open

    def capture_open(path: str, flags: int, *, dir_fd: int | None = None) -> int:
        descriptor = real_open(path, flags, dir_fd=dir_fd)
        if path == str(stat_path):
            opened.append(descriptor)
            assert flags & os.O_NOFOLLOW
            assert flags & os.O_CLOEXEC
        return descriptor

    monkeypatch.setattr(os, "open", capture_open)
    return opened


def test_builder_uses_one_system_runtime_and_requires_full_guest() -> None:
    program = build_root_guest_program(
        "_agw_test",
        ("_vm_guest_identity_protocol", "_vm_guest_identity_guest"),
        "_vm_guest_identity_guest",
        delivery=RootGuestDelivery.INLINE,
    )
    argv, candidates, shim = build_root_guest_bootstrap_argv(
        _ROOT,
        _TARGET,
        selection=RuntimeSelection(RuntimeTargetOS.LINUX),
        program=program,
        nonce=_NONCE,
        expected_guest=_GUEST,
    )
    assert candidates == ("/usr/bin/python3",)
    assert shim is None
    assert argv[:4] == ("/usr/bin/setpriv", "--inh-caps=-all", "--ambient-caps=-all", "--")
    assert argv.count("/usr/bin/python3") == 1
    compile(argv[-2], "<fixed-bootstrap>", "exec")
    invalid_guest: Any = None
    with pytest.raises(ValidationError):
        build_root_guest_bootstrap_argv(
            _ROOT,
            _TARGET,
            selection=RuntimeSelection(RuntimeTargetOS.LINUX),
            program=program,
            nonce=_NONCE,
            expected_guest=invalid_guest,
        )


@pytest.mark.parametrize("runtime", [Path(sys.executable), Path("/usr/bin/python3.11")])
def test_compressed_root_source_refuses_nonroot_under_isolated_python(runtime: Path) -> None:
    if sys.platform != "linux" or not runtime.is_file() or os.geteuid() == 0:
        pytest.skip("requires nonroot Linux and the selected interpreter")
    program = _build_root_guest_program(
        "_agw_test",
        (("_entry", "def main(nonce):return 0\n"),),
        "_entry",
        delivery=RootGuestDelivery.INLINE,
    )
    argv, _, _ = build_root_guest_bootstrap_argv(
        _ROOT,
        _TARGET,
        selection=RuntimeSelection(RuntimeTargetOS.LINUX),
        program=program,
        nonce=_NONCE,
        expected_guest=_GUEST,
    )
    completed = subprocess.run(
        [str(runtime), "-I", "-S", "-B", "-c", argv[-2]],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 125
    assert completed.stdout == completed.stderr == b""


def test_builder_refuses_unbound_runtime_or_identity() -> None:
    program = _build_root_guest_program(
        "_agw_test",
        (("_entry", "def main(nonce):return 0\n"),),
        "_entry",
        delivery=RootGuestDelivery.INLINE,
    )
    for selection, root, target in (
        (RuntimeSelection(RuntimeTargetOS.DARWIN), _ROOT, _TARGET),
        (RuntimeSelection(RuntimeTargetOS.LINUX, "/tmp/python3"), _ROOT, _TARGET),
        (RuntimeSelection(RuntimeTargetOS.LINUX), IdentityPlan(_TARGET, IdentityMode.DIRECT), _TARGET),
        (RuntimeSelection(RuntimeTargetOS.LINUX), _ROOT, IdentityExpectation(1001, 1001, (1002,))),
    ):
        with pytest.raises(ValidationError):
            build_root_guest_bootstrap_argv(
                root,
                target,
                selection=selection,
                program=program,
                nonce=_NONCE,
                expected_guest=_GUEST,
            )


def test_root_program_adds_identity_pair_and_loads_remaining_once() -> None:
    program = _build_root_guest_program(
        "_agw_test",
        (("_entry", "def main(nonce):return 0\n"),),
        "_entry",
        delivery=RootGuestDelivery.INLINE,
    )
    assert program.prefix == b""
    scope: dict[str, Any] = {}
    exec(program.loader_source, scope)
    scope["_agw_load_identity"]()
    guest = sys.modules["_agw_test._vm_guest_identity_guest"]

    def reader() -> bytes:
        return b"held init fact"

    guest._bind_init_reader(reader)
    assert guest._INIT_READER is reader
    assert "_agw_test._entry" not in sys.modules
    scope["_agw_load_remaining"]()
    assert sys.modules["_agw_test._vm_guest_identity_guest"] is guest
    assert guest._INIT_READER is reader
    assert "_agw_test._entry" in sys.modules
    for invalid in (
        (("_entry", "pass"), ("_entry", "pass")),
        (("_vm_guest_identity_guest", "pass"), ("_entry", "pass")),
        (("_vm_guest_identity_protocol", "pass"), ("_entry", "pass")),
    ):
        with pytest.raises(ValueError):
            _build_root_guest_program("_agw_test", invalid, "_entry", delivery=RootGuestDelivery.INLINE)


def test_fixed_prefix_is_read_once_and_verified_before_module_load(monkeypatch: pytest.MonkeyPatch) -> None:
    program = _build_root_guest_program(
        "_agw_test_prefix",
        (("_entry", "def main(nonce):return 0\n"),),
        "_entry",
        delivery=RootGuestDelivery.FIXED_PREFIX,
    )
    assert program.prefix
    reads = 0

    def read_prefix(descriptor: int, count: int) -> bytes:
        nonlocal reads
        assert descriptor == 0
        reads += 1
        return program.prefix

    monkeypatch.setattr(os, "read", read_prefix)
    scope: dict[str, Any] = {}
    exec(program.loader_source, scope)
    assert reads == 1
    assert "_agw_test_prefix._vm_guest_identity_guest" not in sys.modules
    scope["_agw_load_identity"]()
    assert "_agw_test_prefix._entry" not in sys.modules
    scope["_agw_load_remaining"]()
    assert "_agw_test_prefix._entry" in sys.modules
    monkeypatch.setattr(os, "read", lambda *_args: b"x" * len(program.prefix))
    with pytest.raises(ValueError):
        exec(program.loader_source, {})


@pytest.mark.parametrize("runtime", [Path(sys.executable), Path("/usr/bin/python3.11")])
def test_fixed_prefix_loader_executes_under_supported_isolated_python(runtime: Path) -> None:
    if not runtime.is_file():
        pytest.skip("Python 3.11 is unavailable")
    program = _build_root_guest_program(
        "_agw_test_runtime",
        (("_entry", "def main(nonce):return 0\n"),),
        "_entry",
        delivery=RootGuestDelivery.FIXED_PREFIX,
    )
    completed = subprocess.run(
        [str(runtime), "-I", "-S", "-B", "-c", program.loader_source + "_agw_load_identity()\n_agw_load_remaining()\n"],
        input=program.prefix,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout == completed.stderr == b""


def test_drop_verifies_exact_credentials_and_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = []
    monkeypatch.setattr(os, "setgroups", lambda groups: events.append(("groups", groups)))
    monkeypatch.setattr(os, "setresgid", lambda *ids: events.append(("gid", ids)))
    monkeypatch.setattr(os, "setresuid", lambda *ids: events.append(("uid", ids)))
    monkeypatch.setattr(os, "getresuid", lambda: (1001, 1001, 1001))
    monkeypatch.setattr(os, "getresgid", lambda: (1001, 1001, 1001))
    monkeypatch.setattr(os, "getegid", lambda: 1001)
    monkeypatch.setattr(os, "getgroups", lambda: [1001])

    def capabilities_zero(*, non_root: bool) -> bool:
        events.append(("caps", non_root))
        return True

    monkeypatch.setattr(bootstrap, "_capabilities_zero", capabilities_zero)
    bootstrap._drop_and_verify(1001, 1001, (1001,))
    assert events == [("groups", [1001]), ("gid", (1001, 1001, 1001)), ("uid", (1001, 1001, 1001)), ("caps", True)]


@pytest.mark.parametrize(
    ("capabilities", "non_root", "accepted"),
    [
        (b"CapInh:\t0000\nCapPrm:\t0000\nCapEff:\t0000\nCapAmb:\t0000\n", True, True),
        (b"CapInh:\t0000\nCapPrm:\t0001\nCapEff:\t0000\nCapAmb:\t0000\n", True, False),
        (b"CapInh:\t0000\nCapPrm:\t0001\nCapEff:\t0001\nCapAmb:\t0000\n", False, True),
        (b"CapInh:\t0000\nCapAmb:\t0001\n", False, False),
        (b"CapInh:\t0000\nCapPrm:\t0000\nCapEff:\t0000\n", True, False),
    ],
)
def test_capability_verification_is_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capabilities: bytes,
    non_root: bool,
    accepted: bool,
) -> None:
    status = tmp_path / "status"
    status.write_bytes(capabilities)
    monkeypatch.setattr(bootstrap, "_STATUS_PATH", str(status))
    assert bootstrap._capabilities_zero(non_root=non_root) is accepted


def test_held_init_reader_starts_at_zero_and_is_bounded(tmp_path: Path) -> None:
    stat_path = tmp_path / "stat"
    stat_path.write_bytes(b"first")
    descriptor = os.open(stat_path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        assert bootstrap._read_init(descriptor) == b"first"
        stat_path.write_bytes(b"second")
        assert bootstrap._read_init(descriptor) == b"second"
        stat_path.write_bytes(b"x" * (bootstrap._INIT_LIMIT + 100))
        assert len(bootstrap._read_init(descriptor)) == bootstrap._INIT_LIMIT + 1
    finally:
        os.close(descriptor)


@pytest.mark.skipif(sys.platform != "linux", reason="fixed Linux bootstrap")
def test_nonroot_entry_refuses_before_privileged_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "getresuid", lambda: (0, 1001, 0))
    monkeypatch.setattr(os, "open", lambda *_args, **_kwargs: pytest.fail("privileged open reached"))
    assert bootstrap.main(1001, 1001, (1001,), "raise AssertionError", _EXPECTED) == 125


@pytest.mark.skipif(sys.platform != "linux", reason="fixed Linux bootstrap")
def test_drop_identity_check_remaining_body_and_descriptor_lifetime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    opened = _prepare(tmp_path, monkeypatch, events)
    assert bootstrap.main(1001, 1001, (1001,), _synthetic_loader(), _EXPECTED) == 0
    assert events == ["drop", "identity", "bind", "remaining", "body"]
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.skipif(sys.platform != "linux", reason="fixed Linux bootstrap")
def test_missing_guest_checkpoint_refuses_before_remaining_or_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _prepare(tmp_path, monkeypatch, events)
    loader = (
        "import builtins\n"
        "def _agw_load_identity():builtins._agw_bootstrap_events.append('identity')\n"
        "def _agw_load_remaining():raise AssertionError('remaining reached')\n"
        "def _agw_enter_body():raise AssertionError('body reached')\n"
    )
    assert bootstrap.main(1001, 1001, (1001,), loader, _EXPECTED) == 125
    assert events == ["drop", "identity"]


@pytest.mark.skipif(sys.platform != "linux", reason="fixed Linux bootstrap")
def test_corrupt_prefix_refuses_before_identity_module_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    opened = _prepare(tmp_path, monkeypatch, events)
    program = _build_root_guest_program(
        "_agw_corrupt",
        (("_entry", "def main(nonce):return 0\n"),),
        "_entry",
        delivery=RootGuestDelivery.FIXED_PREFIX,
    )
    monkeypatch.setattr(os, "read", lambda *_args: b"x" * len(program.prefix))
    assert bootstrap.main(1001, 1001, (1001,), program.loader_source, _EXPECTED) == 125
    assert events == ["drop"]
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.skipif(sys.platform != "linux", reason="fixed Linux bootstrap")
@pytest.mark.parametrize("changed", ["marker", "boot", "init"])
def test_guest_mismatch_refuses_before_remaining_body_or_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    events: list[str] = []
    _prepare(tmp_path, monkeypatch, events)
    changed_identity = {
        "marker": ("b" * 32, _EXPECTED[1], _EXPECTED[2]),
        "boot": (_EXPECTED[0], "00000000-0000-4000-8000-000000000001", _EXPECTED[2]),
        "init": (_EXPECTED[0], _EXPECTED[1], 1235),
    }[changed]
    monkeypatch.setattr(os, "read", lambda *_args: pytest.fail("stdin reached"))
    assert bootstrap.main(1001, 1001, (1001,), _synthetic_loader(changed_identity), _EXPECTED) == 125
    assert events == ["drop", "identity", "bind"]


@pytest.mark.skipif(sys.platform != "linux", reason="fixed Linux bootstrap")
def test_body_control_propagates_and_closes_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    opened = _prepare(tmp_path, monkeypatch, events)
    with pytest.raises(KeyboardInterrupt):
        bootstrap.main(1001, 1001, (1001,), _synthetic_loader(body="raise KeyboardInterrupt"), _EXPECTED)
    assert events == ["drop", "identity", "bind", "remaining", "body"]
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.skipif(sys.platform != "linux", reason="fixed Linux bootstrap")
def test_failed_drop_refuses_before_loader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    opened = _prepare(tmp_path, monkeypatch, events)

    def fail_drop(*_args: object) -> None:
        raise OSError("drop failed")

    monkeypatch.setattr(bootstrap, "_drop_and_verify", fail_drop)
    assert bootstrap.main(1001, 1001, (1001,), "raise AssertionError", _EXPECTED) == 125
    assert events == []
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.skipif(sys.platform != "linux", reason="fixed Linux bootstrap")
def test_exec_does_not_inherit_held_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    opened = _prepare(tmp_path, monkeypatch, events)
    existing_open = os.open

    def mark_open(path: str, flags: int, *, dir_fd: int | None = None) -> int:
        descriptor = existing_open(path, flags, dir_fd=dir_fd)
        if path == bootstrap._INIT_PATH:
            monkeypatch.setattr(builtins, "_agw_held_init_fd", descriptor, raising=False)
        return descriptor

    monkeypatch.setattr(os, "open", mark_open)
    probe = "import os,sys\ntry:os.fstat(int(sys.argv[1]))\nexcept OSError:raise SystemExit(0)\nraise SystemExit(1)\n"
    body = (
        "import os,subprocess,sys\n"
        "fd=builtins._agw_held_init_fd\n"
        "assert not os.get_inheritable(fd)\n"
        f"result=subprocess.run((sys.executable,'-I','-c',{probe!r},str(fd)),close_fds=False,check=False)\n"
        "assert result.returncode==0\n"
        "return 0"
    )
    assert bootstrap.main(1001, 1001, (1001,), _synthetic_loader(body=body), _EXPECTED) == 0
    assert len(opened) == 1
