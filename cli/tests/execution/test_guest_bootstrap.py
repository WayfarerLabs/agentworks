"""Synthetic custody checks for the fixed Linux guest bootstrap."""

from __future__ import annotations

import builtins
import os
import sys
from importlib.resources import files
from pathlib import Path

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import _guest_bootstrap as bootstrap
from agentworks.execution._helper_bundle import _build_file_helper_bundle, build_helper_modules
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._runtime_prerequisite import (
    RuntimeSelection,
    RuntimeTargetOS,
    build_root_guest_bootstrap_argv,
)
from agentworks.execution._vm_guest_identity_protocol import VMGuestIdentity

from .test_vm_guest_identity import _init_stat

_NONCE = "a" * 32
_ROOT = IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.DIRECT)
_TARGET = IdentityExpectation(1001, 1001, (1001,))
_GUEST = VMGuestIdentity("a" * 32, "123e4567-e89b-12d3-a456-426614174000", 1234)
_OBSERVER_SOURCE = build_helper_modules(
    "_agw_bootstrap_guest", ("_vm_guest_identity_protocol", "_vm_guest_identity_guest")
)


def test_builder_keeps_one_system_runtime_and_fixed_capability_clear() -> None:
    argv, candidates, shim = build_root_guest_bootstrap_argv(
        _ROOT,
        _TARGET,
        selection=RuntimeSelection(RuntimeTargetOS.LINUX),
        fixed_source="pass",
        nonce=_NONCE,
        expected_guest=_GUEST,
    )
    assert candidates == ("/usr/bin/python3",)
    assert shim is None
    assert argv[:4] == ("/usr/bin/setpriv", "--inh-caps=-all", "--ambient-caps=-all", "--")
    assert argv.count("/usr/bin/python3") == 1
    compile(argv[-2], "<fixed-bootstrap>", "exec")


@pytest.mark.parametrize(
    ("selection", "root", "target"),
    [
        (RuntimeSelection(RuntimeTargetOS.DARWIN), _ROOT, _TARGET),
        (RuntimeSelection(RuntimeTargetOS.LINUX, "/tmp/python3"), _ROOT, _TARGET),
        (
            RuntimeSelection(RuntimeTargetOS.LINUX),
            IdentityPlan(_TARGET, IdentityMode.DIRECT),
            _TARGET,
        ),
        (RuntimeSelection(RuntimeTargetOS.LINUX), _ROOT, IdentityExpectation(1001, 1001, (1002,))),
    ],
)
def test_builder_refuses_unbound_runtime_or_identity(
    selection: RuntimeSelection, root: IdentityPlan, target: IdentityExpectation
) -> None:
    with pytest.raises(ValidationError):
        build_root_guest_bootstrap_argv(root, target, selection=selection, fixed_source="pass", nonce=_NONCE)


def test_drop_sets_groups_then_all_gids_then_all_uids_and_verifies_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = []
    monkeypatch.setattr(os, "setgroups", lambda groups: events.append(("groups", groups)))
    monkeypatch.setattr(os, "setresgid", lambda *ids: events.append(("gid", ids)))
    monkeypatch.setattr(os, "setresuid", lambda *ids: events.append(("uid", ids)))
    monkeypatch.setattr(os, "getresuid", lambda: (1001, 1001, 1001))
    monkeypatch.setattr(os, "getresgid", lambda: (1001, 1001, 1001))
    monkeypatch.setattr(os, "getegid", lambda: 1001)
    monkeypatch.setattr(os, "getgroups", lambda: [1001])
    monkeypatch.setattr(bootstrap, "_capabilities_zero", lambda *, non_root: events.append(("caps", non_root)) or True)

    bootstrap._drop_and_verify(1001, 1001, (1001,))
    assert events == [
        ("groups", [1001]),
        ("gid", (1001, 1001, 1001)),
        ("uid", (1001, 1001, 1001)),
        ("caps", True),
    ]


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
@pytest.mark.parametrize("actual", [(1001, 1001, 1001), (0, 1001, 0), (0, 0, 1001)])
def test_nonroot_entry_refuses_before_privileged_open_or_body(
    monkeypatch: pytest.MonkeyPatch, actual: tuple[int, int, int]
) -> None:
    monkeypatch.setattr(os, "getresuid", lambda: actual)

    def forbidden_open(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("privileged open reached")

    monkeypatch.setattr(os, "open", forbidden_open)
    assert bootstrap.main(1001, 1001, (1001,), "raise AssertionError", "raise AssertionError") == 125


@pytest.mark.skipif(sys.platform != "linux", reason="fixed Linux bootstrap")
def test_drop_precedes_observer_and_body_and_closes_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stat_path = tmp_path / "stat"
    stat_path.write_text(_init_stat("1234"), encoding="ascii")
    monkeypatch.setattr(bootstrap, "_INIT_PATH", str(stat_path))
    monkeypatch.setattr(os, "getresuid", lambda: (0, 0, 0))
    monkeypatch.setattr(os, "register_at_fork", lambda **_kwargs: pytest.fail("unexpected fork hook"))
    events: list[str] = []
    monkeypatch.setattr(builtins, "_agw_bootstrap_events", events, raising=False)
    monkeypatch.setattr(bootstrap, "_drop_and_verify", lambda *_args: events.append("drop"))
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
    observer = (
        "import builtins,sys,types\n"
        "builtins._agw_bootstrap_events.append('observer')\n"
        "m=types.ModuleType('_agw_bootstrap_guest._vm_guest_identity_guest')\n"
        "m._bind_init_reader=lambda reader: None\n"
        "sys.modules[m.__name__]=m\n"
    )
    body = "import builtins\nbuiltins._agw_bootstrap_events.append('body')\n"

    assert bootstrap.main(1001, 1001, (1001,), observer, body) == 0
    assert events == ["drop", "observer", "body"]
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.skipif(sys.platform != "linux", reason="fixed Linux bootstrap")
def test_failed_drop_refuses_before_loader_and_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stat_path = tmp_path / "stat"
    stat_path.write_bytes(b"stat")
    monkeypatch.setattr(bootstrap, "_INIT_PATH", str(stat_path))
    monkeypatch.setattr(os, "getresuid", lambda: (0, 0, 0))
    opened: list[int] = []
    real_open = os.open

    def capture_open(path: str, flags: int, *, dir_fd: int | None = None) -> int:
        descriptor = real_open(path, flags, dir_fd=dir_fd)
        if path == str(stat_path):
            opened.append(descriptor)
        return descriptor

    def fail_drop(*_args: object) -> None:
        raise OSError("credential transition failed")

    monkeypatch.setattr(os, "open", capture_open)
    monkeypatch.setattr(bootstrap, "_drop_and_verify", fail_drop)
    assert bootstrap.main(1001, 1001, (1001,), "raise AssertionError", "raise AssertionError") == 125
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.skipif(sys.platform != "linux", reason="fixed Linux bootstrap")
def test_exec_cannot_inherit_held_init_descriptor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stat_path = tmp_path / "stat"
    stat_path.write_bytes(b"stat")
    monkeypatch.setattr(bootstrap, "_INIT_PATH", str(stat_path))
    monkeypatch.setattr(os, "getresuid", lambda: (0, 0, 0))
    monkeypatch.setattr(bootstrap, "_drop_and_verify", lambda *_args: None)
    real_open = os.open

    def capture_open(path: str, flags: int, *, dir_fd: int | None = None) -> int:
        descriptor = real_open(path, flags, dir_fd=dir_fd)
        if path == str(stat_path):
            monkeypatch.setattr(builtins, "_agw_held_init_fd", descriptor, raising=False)
        return descriptor

    monkeypatch.setattr(os, "open", capture_open)
    observer = (
        "import sys,types\n"
        "m=types.ModuleType('_agw_bootstrap_guest._vm_guest_identity_guest')\n"
        "m._bind_init_reader=lambda reader: None\n"
        "sys.modules[m.__name__]=m\n"
    )
    probe = "import os,sys\ntry:os.fstat(int(sys.argv[1]))\nexcept OSError:raise SystemExit(0)\nraise SystemExit(1)\n"
    body = (
        "import builtins,os,subprocess,sys\n"
        "fd=builtins._agw_held_init_fd\n"
        "assert not os.get_inheritable(fd)\n"
        f"result=subprocess.run((sys.executable,'-I','-c',{probe!r},str(fd)),close_fds=False,check=False)\n"
        "assert result.returncode==0\n"
    )
    assert bootstrap.main(1001, 1001, (1001,), observer, body) == 0


@pytest.mark.skipif(sys.platform != "linux", reason="fixed Linux bootstrap")
@pytest.mark.parametrize("changed", ["marker", "boot", "init"])
def test_guest_mismatch_refuses_before_body_or_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    stat_path = tmp_path / "stat"
    stat_path.write_bytes(b"stat")
    monkeypatch.setattr(bootstrap, "_INIT_PATH", str(stat_path))
    monkeypatch.setattr(os, "getresuid", lambda: (0, 0, 0))
    monkeypatch.setattr(bootstrap, "_drop_and_verify", lambda *_args: None)
    values: list[object] = [_GUEST.instance_marker, _GUEST.boot_id, _GUEST.init_start_ticks]
    values[{"marker": 0, "boot": 1, "init": 2}[changed]] = (
        "b" * 32 if changed == "marker" else ("00000000-0000-4000-8000-000000000001" if changed == "boot" else 1235)
    )
    observer = (
        "import sys,types\n"
        "m=types.ModuleType('_agw_bootstrap_guest._vm_guest_identity_guest')\n"
        "m._bind_init_reader=lambda reader: None\n"
        f"m._identity=lambda:types.SimpleNamespace(instance_marker={values[0]!r},"
        f"boot_id={values[1]!r},init_start_ticks={values[2]!r})\n"
        "sys.modules[m.__name__]=m\n"
    )
    reads: list[int] = []
    real_read = os.read

    def capture_read(descriptor: int, count: int) -> bytes:
        if descriptor == 0:
            reads.append(count)
        return real_read(descriptor, count)

    monkeypatch.setattr(os, "read", capture_read)
    assert (
        bootstrap.main(
            1001,
            1001,
            (1001,),
            observer,
            "import os\nos.read(0,1)\n",
            (_GUEST.instance_marker, _GUEST.boot_id, _GUEST.init_start_ticks),
        )
        == 125
    )
    assert reads == []


@pytest.mark.skipif(sys.platform != "linux", reason="fixed Linux bootstrap")
def test_real_observer_uses_held_reader_after_drop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stat_path = tmp_path / "stat"
    stat_path.write_text(_init_stat("1234"), encoding="ascii")
    monkeypatch.setattr(bootstrap, "_INIT_PATH", str(stat_path))
    monkeypatch.setattr(os, "getresuid", lambda: (0, 0, 0))
    monkeypatch.setattr(bootstrap, "_drop_and_verify", lambda *_args: None)
    body = (
        "import sys\n"
        "guest=sys.modules['_agw_bootstrap_guest._vm_guest_identity_guest']\n"
        "assert guest._read_init_start_ticks()==1234\n"
    )
    assert bootstrap.main(1001, 1001, (1001,), _OBSERVER_SOURCE, body) == 0


@pytest.mark.skipif(sys.platform != "linux", reason="fixed Linux bootstrap")
def test_stdin_bundle_rebinds_reader_after_guest_module_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stat_path = tmp_path / "stat"
    stat_path.write_text(_init_stat("1234"), encoding="ascii")
    monkeypatch.setattr(bootstrap, "_INIT_PATH", str(stat_path))
    monkeypatch.setattr(os, "getresuid", lambda: (0, 0, 0))
    monkeypatch.setattr(bootstrap, "_drop_and_verify", lambda *_args: None)
    package = files("agentworks.execution")
    sources = tuple(
        (name, package.joinpath(f"{name}.py").read_text(encoding="utf-8"))
        for name in ("_vm_guest_identity_protocol", "_vm_guest_identity_guest")
    )
    bundle = _build_file_helper_bundle(
        "_agw_reloaded",
        (
            *sources,
            (
                "_entry",
                "from . import _vm_guest_identity_guest as guest\n"
                "def main(nonce):\n return int(guest._read_init_start_ticks()!=1234)\n",
            ),
        ),
        "_entry",
    )
    reads = 0
    real_read = os.read

    def feed_prefix(descriptor: int, count: int) -> bytes:
        nonlocal reads
        if descriptor == 0:
            reads += 1
            return bundle.prefix if reads == 1 else b""
        return real_read(descriptor, count)

    monkeypatch.setattr(os, "read", feed_prefix)
    monkeypatch.setattr(sys, "argv", ["agentworks-fixed-helper", _NONCE])
    with pytest.raises(SystemExit) as raised:
        bootstrap.main(1001, 1001, (1001,), _OBSERVER_SOURCE, bundle.bootstrap)
    assert raised.value.code == 0
    assert reads == 1
