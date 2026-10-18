"""Packed relay loads without an installation on Python 3.11 and newer."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from agentworks.execution import _guest_bootstrap as bootstrap
from agentworks.execution._helper_bundle import RootGuestDelivery
from agentworks.execution._helper_identity import IdentityExpectation
from agentworks.execution._helper_launcher import IdentityMode, IdentityPlan
from agentworks.execution._managed_job_store import ManagedJobStore
from agentworks.execution._managed_live_bundle import ROOT_PROGRAM
from agentworks.execution._managed_live_protocol import encode_envelope
from agentworks.execution._runtime_prerequisite import (
    RuntimeSelection,
    RuntimeTargetOS,
    build_root_guest_bootstrap_argv,
)

from .test_managed_live_protocol import GUEST, NONCE, _request
from .test_managed_live_relay import _handshake, _listener, _process, _publish, _read_all, _serve
from .test_managed_live_relay import store as store


@pytest.mark.parametrize("interpreter", [sys.executable, "/usr/bin/python3.11"])
def test_exact_packed_modules_load_and_leave_envelope_suffix(interpreter: str, tmp_path: Path) -> None:
    if not Path(interpreter).is_file():
        pytest.skip("interpreter unavailable")
    source = ROOT_PROGRAM.loader_source + (
        "_agw_load_identity()\n_agw_load_remaining()\n"
        "g=sys.modules['_agw_managed_live._managed_live_guest']\n"
        "p=sys.modules['_agw_managed_live._managed_live_protocol']\n"
        "request=g._read_request()\n"
        "assert p.encode_request(p.decode_request(p.encode_request(request)))==p.encode_request(request)\n"
        "suffix=os.read(0,65536)\n"
        "assert os.read(0,1)==b''\n"
        "os.write(1,suffix)\n"
    )
    result = subprocess.run(
        [interpreter, "-I", "-S", "-B", "-c", source, NONCE],
        input=ROOT_PROGRAM.prefix + encode_envelope(_request()) + b"private-live-suffix\x00\xff",
        capture_output=True,
        cwd=tmp_path,
        env={"PYTHONPATH": str(tmp_path)},
        timeout=10,
        check=False,
    )
    assert ROOT_PROGRAM.delivery is RootGuestDelivery.FIXED_PREFIX
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"private-live-suffix\x00\xff" and result.stderr == b""


def test_python311_packed_relay_uses_actual_socket_and_pipes(store: ManagedJobStore, tmp_path: Path) -> None:
    interpreter = "/usr/bin/python3.11"
    if not Path(interpreter).is_file():
        pytest.skip("Python 3.11 unavailable")
    request = _request()
    _publish(store, request)

    def action(connection: socket.socket) -> None:
        _handshake(connection, request)
        assert _read_all(connection) == b"binary-input\0\xff"
        connection.sendall(b"binary-output\xff\0")

    with _listener(store) as listener:
        thread, errors = _serve(listener, action)
        with _process(tmp_path, interpreter=interpreter) as process:
            stdout, stderr = process.communicate(
                ROOT_PROGRAM.prefix + encode_envelope(request) + b"binary-input\0\xff", timeout=10
            )
        thread.join(6)
        assert not thread.is_alive() and not errors
    assert process.returncode == 0 and stderr == b""
    assert stdout == b"binary-output\xff\0"


def test_production_numeric_launcher_contains_only_fixed_source_identity_and_nonce() -> None:
    root = IdentityExpectation(0, 0, (0,))
    argv, candidates, shim = build_root_guest_bootstrap_argv(
        IdentityPlan(root, IdentityMode.SUDO_ROOT),
        root,
        selection=RuntimeSelection(RuntimeTargetOS.LINUX),
        program=ROOT_PROGRAM,
        nonce=NONCE,
        expected_guest=GUEST,
    )
    assert argv[:4] == ("/usr/bin/sudo", "-n", "--user=#0", "--")
    assert candidates == ("/usr/bin/python3",) and shim is None
    assert _request().expected_launch.decode() not in repr(argv)
    compile(argv[-2], "<fixed-root-relay>", "exec")


def test_early_guest_checkpoint_precedes_request_consumption(monkeypatch: pytest.MonkeyPatch) -> None:
    # Use the real relay loader; inject only privilege and fixed identity reads.
    from contextlib import contextmanager

    events: list[str] = []
    pending = bytearray(ROOT_PROGRAM.prefix + encode_envelope(_request()))
    actual_read = os.read

    def read(fd: int, count: int) -> bytes:
        if fd != 0:
            return actual_read(fd, count)
        events.append("prefix" if len(pending) > len(encode_envelope(_request())) else "request")
        chunk = bytes(pending[:count])
        del pending[:count]
        return chunk

    @contextmanager
    def admit(*_args: object) -> Iterator[Callable[[], bytes]]:
        events.append("root")
        yield lambda: b"held-init-stat"

    monkeypatch.setattr(os, "read", read)
    monkeypatch.setattr(bootstrap, "_admit", admit)
    # Wrap identity installation, then keep the production two-phase ordering.
    loader = ROOT_PROGRAM.loader_source + (
        "_original_load_identity=_agw_load_identity\n"
        "def _agw_load_identity():\n"
        " _original_load_identity()\n"
        " g=sys.modules[_agw_guest_module]\n"
        f" g._identity=lambda:sys.modules['_agw_managed_live._vm_guest_identity_protocol'].VMGuestIdentity("
        f"{GUEST.instance_marker!r},{GUEST.boot_id!r},{GUEST.init_start_ticks})\n"
        "def _agw_load_remaining():raise RuntimeError('checkpoint completed')\n"
    )
    with pytest.raises(RuntimeError):
        bootstrap._run(0, 0, (0,), loader, (GUEST.instance_marker, GUEST.boot_id, GUEST.init_start_ticks))
    assert events == ["root", "prefix"]
    assert bytes(pending) == encode_envelope(_request())
