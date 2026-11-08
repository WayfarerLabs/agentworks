"""``vm create`` ended by any signal, or with nothing reading its output: a cloud's create still rolls back."""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import sys
import time
from typing import TYPE_CHECKING

import pytest

from agentworks.config import load_config
from agentworks.db import Database
from agentworks.execution import trust
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager
from tests._aws_fakes import install_fakes, stub_egress
from tests._root_channel_fakes import Answer, FakeGuest, install_at_run_process
from tests.ssh_fixtures import write_test_ssh_keypair

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from agentworks.config import Config

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="os.kill delivery is POSIX")

_AWS_SITE = (
    "apiVersion: agentworks/v1\nkind: vm-site\nmetadata:\n  name: aws-dev\nspec:\n"
    "  platform:\n    name: aws-ec2\n    region: us-east-1\n    auth:\n      mode: ambient\n"
)


def _config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    key = tmp_path / "id_ed25519"
    write_test_ssh_keypair(key)
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "tskey-test")
    monkeypatch.setattr(vm_manager, "verify_tailscale_available", lambda: None)
    config = tmp_path / "config.toml"
    config.write_text(
        f'[operator]\nssh_public_key = "{key.as_posix()}.pub"\nssh_private_key = "{key.as_posix()}"\n'
        '[plugins]\nsystem = ["aws"]\n'
    )
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "site.yaml").write_text(_AWS_SITE)
    return load_config(config, warn_issues=False, warn_deprecations=False)


@pytest.mark.parametrize("signal_name", ["SIGINT", "SIGTERM", "SIGHUP"])
def test_a_termination_during_a_cloud_create_rolls_it_back(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signal_name: str
) -> None:
    """The signal lands while the bootstrap allow is open; the create's interrupt arm must still remove it."""
    config = _config(tmp_path, monkeypatch)
    recorder = install_fakes(monkeypatch)
    stub_egress(monkeypatch)
    before = signal.getsignal(getattr(signal, signal_name))

    def respond(payload: str, seen: int) -> Answer:
        os.kill(os.getpid(), getattr(signal, signal_name))
        raise AssertionError(f"{signal_name} did not interrupt the create")

    install_at_run_process(monkeypatch, FakeGuest(respond))

    with pytest.raises(KeyboardInterrupt):
        vm_manager.create_vm(db, config, name="dev", site="aws-dev", interaction=TtyInteractionPolicy.REFUSE)

    calls = recorder.methods("ec2")
    assert "authorize_security_group_ingress" in calls
    assert "terminate_instances" in calls
    assert "delete_security_group" in calls
    assert db.get_vm("dev") is None
    assert not trust.store_file("dev").exists()
    assert signal.getsignal(getattr(signal, signal_name)) == before


@contextlib.contextmanager
def _dead_pipe() -> Iterator[None]:
    """``agw vm create 2>&1 | tee create.log`` once ``tee`` has gone: both streams are a pipe nobody reads.

    The CLI's own handler writes the lines, as in a real run. It is entered in
    the test body, since the runner's capture puts its own streams back
    around each fixture.
    """
    from agentworks import output
    from agentworks.cli._typer_output import TyperHandler

    reader, writer = os.pipe()
    os.close(reader)
    stream = open(writer, "w", encoding="utf-8", buffering=1)  # noqa: SIM115 - closed below
    saved = (sys.stdout, sys.stderr, output.get_handler())
    sys.stdout = sys.stderr = stream
    output.set_handler(TyperHandler())
    try:
        yield
    finally:
        sys.stdout, sys.stderr = saved[0], saved[1]
        output.set_handler(saved[2])
        with contextlib.suppress(OSError):
            stream.close()


@pytest.mark.parametrize("signal_name", ["SIGINT", "SIGTERM", "SIGHUP"])
def test_a_create_whose_reader_has_gone_rolls_back_as_on_a_live_terminal(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signal_name: str
) -> None:
    """Closing the window ends ``tee`` too, so every line the rollback prints fails; none of them may stop it."""
    config = _config(tmp_path, monkeypatch)
    recorder = install_fakes(monkeypatch)
    stub_egress(monkeypatch)

    def respond(payload: str, seen: int) -> Answer:
        os.kill(os.getpid(), getattr(signal, signal_name))
        raise AssertionError(f"{signal_name} did not interrupt the create")

    install_at_run_process(monkeypatch, FakeGuest(respond))

    with pytest.raises(KeyboardInterrupt), _dead_pipe():
        vm_manager.create_vm(db, config, name="dev", site="aws-dev", interaction=TtyInteractionPolicy.REFUSE)

    calls = recorder.methods("ec2")
    assert "revoke_security_group_ingress" in calls
    assert "terminate_instances" in calls
    assert "delete_security_group" in calls
    assert db.get_vm("dev") is None
    assert not trust.store_file("dev").exists()


@pytest.mark.parametrize("second", ["SIGTERM", "SIGHUP"])
def test_a_termination_during_a_ctrl_c_rollback_does_not_cut_it_short(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, second: str
) -> None:
    """Ctrl-C, then the window closes while the rollback waits on the provider: the rollback still finishes."""
    from tests import _aws_fakes

    config = _config(tmp_path, monkeypatch)
    recorder = install_fakes(monkeypatch)
    stub_egress(monkeypatch)
    terminate = _aws_fakes._FakeEC2.terminate_instances

    def terminate_then_signal(self: object, **kwargs: object) -> object:
        os.kill(os.getpid(), getattr(signal, second))
        return terminate(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(_aws_fakes._FakeEC2, "terminate_instances", terminate_then_signal)

    def respond(payload: str, seen: int) -> Answer:
        os.kill(os.getpid(), signal.SIGINT)
        raise AssertionError("Ctrl-C did not interrupt the create")

    install_at_run_process(monkeypatch, FakeGuest(respond))

    with pytest.raises(KeyboardInterrupt):
        vm_manager.create_vm(db, config, name="dev", site="aws-dev", interaction=TtyInteractionPolicy.REFUSE)

    calls = recorder.methods("ec2")
    assert "terminate_instances" in calls
    assert "delete_security_group" in calls
    assert db.get_vm("dev") is None


# The forked child runs the create with every fixture this process set up. A
# test runner's worker has threads of its own, so a lock one held at the fork
# could deadlock the child; the parent's watchdog then fails the test rather
# than hanging it.
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_a_closed_terminal_during_a_cloud_create_still_rolls_it_back(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The create runs on a real terminal whose far end closes mid-bootstrap, as when its window is closed.

    The kernel hangs the terminal up and sends ``SIGHUP``. Every write to the
    terminal now fails, and the rollback's first line would end it with the
    VM running and its allow open, so a failed line must not stop it.
    """
    import pty

    config = _config(tmp_path, monkeypatch)
    recorder = install_fakes(monkeypatch)
    stub_egress(monkeypatch)
    started_read, started_write = os.pipe()
    report_read, report_write = os.pipe()

    def respond(payload: str, seen: int) -> Answer:
        os.write(started_write, b"x")
        time.sleep(60)
        raise AssertionError("the closed terminal did not interrupt the create")

    install_at_run_process(monkeypatch, FakeGuest(respond))

    pid, terminal = pty.fork()
    if pid == 0:  # pragma: no cover - the child reports through the pipe and never returns
        outcome = "returned"
        try:
            # A CLI's streams sit on the terminal, not on the test runner's capture.
            sys.stdout = open(1, "w", encoding="utf-8", buffering=1, closefd=False)  # noqa: SIM115
            sys.stderr = open(2, "w", encoding="utf-8", buffering=1, closefd=False)  # noqa: SIM115
            child_db = Database(tmp_path / "test.db")
            vm_manager.create_vm(child_db, config, name="dev", site="aws-dev", interaction=TtyInteractionPolicy.REFUSE)
        except BaseException as error:
            outcome = type(error).__name__
        os.write(report_write, json.dumps({"outcome": outcome, "calls": recorder.methods("ec2")}).encode())
        os._exit(0)

    os.close(started_write)
    os.close(report_write)
    try:
        # Drain the terminal until the bootstrap is running, so the child never blocks writing to it.
        deadline = time.monotonic() + 60
        while True:
            assert time.monotonic() < deadline, "the create never reached its bootstrap"
            ready, _, _ = select.select([terminal, started_read], [], [], 1)
            if started_read in ready:
                break
            if terminal in ready:
                os.read(terminal, 65536)
    finally:
        os.close(terminal)
    ready, _, _ = select.select([report_read], [], [], 60)
    if not ready:
        os.kill(pid, signal.SIGKILL)  # the watchdog: the report below is then empty, and the test fails
    os.waitpid(pid, 0)
    report = json.loads(os.read(report_read, 1 << 20) or b"{}")
    os.close(started_read)
    os.close(report_read)

    assert report["outcome"] == "KeyboardInterrupt"
    assert "terminate_instances" in report["calls"]
    assert "revoke_security_group_ingress" in report["calls"]
    assert "delete_security_group" in report["calls"]
    assert db.get_vm("dev") is None
