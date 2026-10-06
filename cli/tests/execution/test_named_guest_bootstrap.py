"""Credential and descriptor boundaries of fixed named-account admission."""

from __future__ import annotations

import builtins
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentworks.errors import ValidationError
from agentworks.execution import _guest_bootstrap as bootstrap
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._named_guest_bootstrap import build_named_guest_bootstrap_argv
from agentworks.execution._runtime_prerequisite import RuntimeSelection, RuntimeTargetOS

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux system account admission")
_NONCE = "a" * 32
_ROOT = IdentityPlan(IdentityExpectation(0, 0, (0,)), IdentityMode.DIRECT)
_BODY = "def main(nonce):return 7\n"


@dataclass
class _Credentials:
    uid: tuple[int, int, int] = (0, 0, 0)
    gid: tuple[int, int, int] = (0, 0, 0)
    groups: tuple[int, ...] = (0,)


def _guest_os() -> SimpleNamespace:
    namespace = vars(bootstrap)["os"]
    assert isinstance(namespace, SimpleNamespace)
    return namespace


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    events: list[object],
    *,
    target_uid: int = 1001,
) -> tuple[_Credentials, list[int]]:
    import pwd

    monkeypatch.setattr(bootstrap, "os", SimpleNamespace(**vars(os)))
    stat = tmp_path / "stat"
    stat.write_bytes(b"first")
    monkeypatch.setattr(bootstrap, "_INIT_PATH", str(stat))
    credentials = _Credentials()
    monkeypatch.setattr(sys, "argv", ["fixed-helper", _NONCE])
    monkeypatch.setattr(_guest_os(), "getresuid", lambda: credentials.uid)
    monkeypatch.setattr(_guest_os(), "getresgid", lambda: credentials.gid)
    monkeypatch.setattr(_guest_os(), "getegid", lambda: credentials.gid[1])
    monkeypatch.setattr(_guest_os(), "getgroups", lambda: list(credentials.groups))
    monkeypatch.setattr(builtins, "_agw_named_events", events, raising=False)
    opened: list[int] = []
    real_open = os.open
    real_compile = builtins.compile

    def lookup(account: str) -> pwd.struct_passwd:
        events.append(("lookup", account))
        return pwd.struct_passwd((account, "x", target_uid, 1002, "", "/unused", "/unused"))

    def grouplist(account: str, gid: int) -> list[int]:
        events.append(("lookup-groups", account, gid))
        return [1003, 1003]

    def open_init(path: str, flags: int, *, dir_fd: int | None = None) -> int:
        assert credentials.uid == (0, 0, 0)
        assert path == str(stat)
        assert flags == os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        events.append("open")
        descriptor = real_open(path, flags, dir_fd=dir_fd)
        opened.append(descriptor)
        return descriptor

    def setgroups(groups: list[int]) -> None:
        events.append(("groups", tuple(groups)))
        credentials.groups = tuple(groups)

    def setgid(real: int, effective: int, saved: int) -> None:
        events.append(("gid", real, effective, saved))
        credentials.gid = (real, effective, saved)

    def setuid(real: int, effective: int, saved: int) -> None:
        events.append(("uid", real, effective, saved))
        credentials.uid = (real, effective, saved)

    def caps(*, non_root: bool) -> bool:
        assert credentials.uid == (target_uid,) * 3
        assert credentials.gid == (1002,) * 3
        assert credentials.groups == (1002, 1003)
        events.append(("caps", non_root))
        return True

    def compile_body(*args: Any, **kwargs: Any) -> Any:
        assert credentials.uid == (target_uid,) * 3
        assert events[-1] == ("caps", target_uid != 0)
        events.append("compile")
        return real_compile(*args, **kwargs)

    monkeypatch.setattr(pwd, "getpwnam", lookup)
    monkeypatch.setattr(_guest_os(), "getgrouplist", grouplist)
    monkeypatch.setattr(_guest_os(), "open", open_init)
    monkeypatch.setattr(_guest_os(), "setgroups", setgroups)
    monkeypatch.setattr(_guest_os(), "setresgid", setgid)
    monkeypatch.setattr(_guest_os(), "setresuid", setuid)
    monkeypatch.setattr(bootstrap, "_capabilities_zero", caps)
    monkeypatch.setattr(bootstrap, "compile", compile_body, raising=False)
    return credentials, opened


@pytest.mark.parametrize("target_uid", [1001, 0])
def test_exact_named_drop_precedes_compile_and_stdio_preserves_nonce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    target_uid: int,
) -> None:
    events: list[object] = []
    credentials, opened = _prepare(tmp_path, monkeypatch, events, target_uid=target_uid)

    def read_stdin(descriptor: int, count: int) -> bytes:
        assert descriptor == 0 and count == 5
        assert credentials.uid == (target_uid,) * 3
        events.append("stdin")
        return b"input"

    monkeypatch.setattr(os, "read", read_stdin)
    body = (
        "import builtins,os\n"
        "builtins._agw_named_events.append('load')\n"
        "def main(nonce):\n"
        " builtins._agw_named_events.append(('body',nonce))\n"
        " assert os.read(0,5)==b'input'\n"
        " os.write(1,b'output')\n"
        " os.write(2,b'diagnostic')\n"
        " return 7\n"
    )
    assert bootstrap.main_named("configured-agent", body) == 7
    assert events == [
        ("lookup", "configured-agent"),
        ("lookup-groups", "configured-agent", 1002),
        "open",
        ("groups", (1002, 1003)),
        ("gid", 1002, 1002, 1002),
        ("uid", target_uid, target_uid, target_uid),
        ("caps", target_uid != 0),
        "compile",
        "load",
        ("body", _NONCE),
        "stdin",
    ]
    assert capfd.readouterr() == ("output", "diagnostic")
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_reader_is_fresh_bounded_noninheritable_and_closed_after_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[object] = []
    _, opened = _prepare(tmp_path, monkeypatch, events)

    def observe(reader: Any) -> None:
        assert reader() == b"first"
        (tmp_path / "stat").write_bytes(b"second")
        assert reader() == b"second"
        (tmp_path / "stat").write_bytes(b"x" * (bootstrap._INIT_LIMIT + 100))
        assert len(reader()) == bootstrap._INIT_LIMIT + 1
        assert not os.get_inheritable(opened[0])

    monkeypatch.setattr(builtins, "_agw_named_observe", observe, raising=False)
    body = "import builtins\ndef main(nonce):\n builtins._agw_named_observe(_agw_read_init)\n return 0\n"
    assert bootstrap.main_named("agent", body) == 0
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.parametrize("failure", [KeyError, OSError, RuntimeError])
@pytest.mark.parametrize("step", ["lookup", "groups"])
def test_lookup_refuses_silently_before_open_compile_or_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    failure: type[Exception],
    step: str,
) -> None:
    import pwd

    events: list[object] = []
    _, opened = _prepare(tmp_path, monkeypatch, events)

    def fail(*args: object) -> Any:
        raise failure("private-account-canary")

    if step == "lookup":
        monkeypatch.setattr(pwd, "getpwnam", fail)
    else:
        monkeypatch.setattr(_guest_os(), "getgrouplist", fail)
    monkeypatch.setattr(_guest_os(), "read", lambda *_args: pytest.fail("stdin reached"))
    assert bootstrap.main_named("agent", "raise AssertionError") == 125
    assert not opened and "compile" not in events
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize("field,value", [("pw_uid", -1), ("pw_uid", True), ("pw_gid", 2**32)])
def test_invalid_system_account_ids_refuse_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    import pwd

    events: list[object] = []
    _, opened = _prepare(tmp_path, monkeypatch, events)
    entry = SimpleNamespace(pw_uid=1001, pw_gid=1002)
    setattr(entry, field, value)
    monkeypatch.setattr(pwd, "getpwnam", lambda _account: entry)
    assert bootstrap.main_named("agent", "raise AssertionError") == 125
    assert not opened


@pytest.mark.parametrize("groups", [[-1], [True], [2**32], list(range(65_537))])
def test_invalid_system_groups_refuse_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, groups: list[int]
) -> None:
    events: list[object] = []
    _, opened = _prepare(tmp_path, monkeypatch, events)
    monkeypatch.setattr(_guest_os(), "getgrouplist", lambda *_args: groups)
    assert bootstrap.main_named("agent", "raise AssertionError") == 125
    assert not opened


@pytest.mark.parametrize("failure_step", ["setgroups", "setresgid", "setresuid", "caps", "uid", "gid", "groups"])
def test_failed_drop_or_verification_refuses_and_closes_without_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    failure_step: str,
) -> None:
    events: list[object] = []
    _, opened = _prepare(tmp_path, monkeypatch, events)

    def fail(*args: object) -> None:
        raise OSError("private-drop-canary")

    if failure_step.startswith("set"):
        monkeypatch.setattr(_guest_os(), failure_step, fail)
    elif failure_step == "caps":
        monkeypatch.setattr(bootstrap, "_capabilities_zero", lambda **_kwargs: False)
    elif failure_step == "uid":
        monkeypatch.setattr(_guest_os(), "setresuid", lambda *_args: None)
    elif failure_step == "gid":
        monkeypatch.setattr(_guest_os(), "setresgid", lambda *_args: None)
    else:
        monkeypatch.setattr(_guest_os(), "setgroups", lambda *_args: None)
    monkeypatch.setattr(_guest_os(), "read", lambda *_args: pytest.fail("stdin reached"))
    assert bootstrap.main_named("agent", "raise AssertionError") == 125
    assert "compile" not in events
    assert capfd.readouterr() == ("", "")
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.parametrize("phase", ["platform", "root"])
def test_wrong_entry_refuses_before_account_lookup(monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    import pwd

    monkeypatch.setattr(bootstrap, "os", SimpleNamespace(**vars(os)))
    if phase == "platform":
        monkeypatch.setattr(sys, "platform", "darwin")
    else:
        monkeypatch.setattr(_guest_os(), "getresuid", lambda: (0, 1001, 0))
    monkeypatch.setattr(pwd, "getpwnam", lambda *_args: pytest.fail("lookup reached"))
    monkeypatch.setattr(_guest_os(), "open", lambda *_args: pytest.fail("open reached"))
    assert bootstrap.main_named("agent", "raise AssertionError") == 125


@pytest.mark.parametrize("phase", ["open", "cloexec"])
def test_descriptor_acquisition_failure_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    events: list[object] = []
    _, opened = _prepare(tmp_path, monkeypatch, events)

    def fail(*args: object) -> None:
        raise OSError("descriptor-canary")

    monkeypatch.setattr(_guest_os(), "open" if phase == "open" else "set_inheritable", fail)
    assert bootstrap.main_named("agent", "raise AssertionError") == 125
    assert "compile" not in events
    if opened:
        with pytest.raises(OSError):
            os.fstat(opened[0])


def test_symlink_init_fact_refuses_without_loading_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = []
    _prepare(tmp_path, monkeypatch, events)
    link = tmp_path / "symlink"
    link.symlink_to(tmp_path / "stat")
    monkeypatch.setattr(bootstrap, "_INIT_PATH", str(link))
    monkeypatch.setattr(_guest_os(), "open", os.open)
    assert bootstrap.main_named("agent", "raise AssertionError") == 125
    assert "compile" not in events


@pytest.mark.parametrize("field", ["uid", "gid"])
@pytest.mark.parametrize("index", [0, 1, 2])
def test_every_real_effective_saved_id_must_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, index: int
) -> None:
    events: list[object] = []
    credentials, opened = _prepare(tmp_path, monkeypatch, events)

    def mismatch(real: int, effective: int, saved: int) -> None:
        ids = [real, effective, saved]
        ids[index] = 0
        setattr(credentials, field, tuple(ids))

    monkeypatch.setattr(_guest_os(), "setresuid" if field == "uid" else "setresgid", mismatch)
    assert bootstrap.main_named("agent", "raise AssertionError") == 125
    assert "compile" not in events
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_cleanup_failure_does_not_replace_admission_or_body_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[object] = []
    _prepare(tmp_path, monkeypatch, events)
    real_close = os.close

    def close_then_fail(descriptor: int) -> None:
        real_close(descriptor)
        raise OSError("close-canary")

    monkeypatch.setattr(_guest_os(), "close", close_then_fail)
    monkeypatch.setattr(bootstrap, "_capabilities_zero", lambda **_kwargs: False)
    assert bootstrap.main_named("agent", "raise AssertionError") == 125
    _prepare(tmp_path, monkeypatch, events)
    monkeypatch.setattr(_guest_os(), "close", close_then_fail)
    with pytest.raises(KeyboardInterrupt):
        bootstrap.main_named("agent", "def main(nonce):raise KeyboardInterrupt\n")


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit, RuntimeError])
def test_body_failures_propagate_and_close_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: type[BaseException]
) -> None:
    events: list[object] = []
    _, opened = _prepare(tmp_path, monkeypatch, events)
    with pytest.raises(error):
        bootstrap.main_named("agent", f"def main(nonce):raise {error.__name__}\n")
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_body_syntax_error_propagates_and_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = []
    _, opened = _prepare(tmp_path, monkeypatch, events)
    with pytest.raises(SyntaxError):
        bootstrap.main_named("agent", "def invalid(")
    with pytest.raises(OSError):
        os.fstat(opened[0])


def test_body_exit_status_must_be_integer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[object] = []
    _, opened = _prepare(tmp_path, monkeypatch, events)
    with pytest.raises(TypeError):
        bootstrap.main_named("agent", "def main(nonce):return True\n")
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.parametrize("mode", [IdentityMode.DIRECT, IdentityMode.SUDO_ROOT])
def test_builder_selects_only_linux_system_python_and_root_caps(mode: IdentityMode) -> None:
    root = IdentityPlan(_ROOT.expected, mode)
    argv, candidates, shim = build_named_guest_bootstrap_argv(
        root, "agent'\naccount", selection=RuntimeSelection(RuntimeTargetOS.LINUX), fixed_source=_BODY, nonce=_NONCE
    )
    assert candidates == ("/usr/bin/python3",) and shim is None
    offset = 0 if mode is IdentityMode.DIRECT else 4
    assert argv[offset : offset + 4] == ("/usr/bin/setpriv", "--inh-caps=-all", "--ambient-caps=-all", "--")
    assert argv.count("/usr/bin/python3") == 1
    compile(argv[-2], "<generated-bootstrap>", "exec")


@pytest.mark.parametrize("account", ["", "a\0b", "\ud800"])
def test_builder_rejects_invalid_configured_account(account: str) -> None:
    with pytest.raises(ValidationError):
        build_named_guest_bootstrap_argv(
            _ROOT, account, selection=RuntimeSelection(RuntimeTargetOS.LINUX), fixed_source=_BODY, nonce=_NONCE
        )


@pytest.mark.parametrize(
    "selection,root",
    [
        (RuntimeSelection(RuntimeTargetOS.DARWIN), _ROOT),
        (RuntimeSelection(RuntimeTargetOS.LINUX, "/tmp/python3"), _ROOT),
        (
            RuntimeSelection(RuntimeTargetOS.LINUX),
            IdentityPlan(IdentityExpectation(1001, 1001, (1001,)), IdentityMode.DIRECT),
        ),
    ],
)
def test_builder_rejects_unbound_runtime_or_root(selection: RuntimeSelection, root: IdentityPlan) -> None:
    with pytest.raises(ValidationError):
        build_named_guest_bootstrap_argv(root, "agent", selection=selection, fixed_source=_BODY, nonce=_NONCE)


@pytest.mark.parametrize("runtime", [Path(sys.executable), Path("/usr/bin/python3.11")])
def test_generated_source_refuses_nonroot_silently_without_consuming_stdin(runtime: Path) -> None:
    if not runtime.is_file() or os.geteuid() == 0:
        pytest.skip("requires nonroot Linux and selected interpreter")
    argv, _, _ = build_named_guest_bootstrap_argv(
        _ROOT, "configured-agent", selection=RuntimeSelection(RuntimeTargetOS.LINUX), fixed_source=_BODY, nonce=_NONCE
    )
    child = subprocess.Popen(
        [str(runtime), "-I", "-S", "-B", "-c", argv[-2], _NONCE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert child.wait(timeout=10) == 125
        assert child.stdout is not None and child.stderr is not None
        assert child.stdout.read() == child.stderr.read() == b""
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate()
