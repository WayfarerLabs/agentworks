"""``vm create`` ended by ``SIGTERM`` or ``SIGHUP``: it unwinds like Ctrl-C, so a cloud's create rolls back."""

from __future__ import annotations

import os
import signal
import sys
from typing import TYPE_CHECKING

import pytest

from agentworks.config import load_config
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager
from tests._aws_fakes import install_fakes, stub_egress
from tests._root_channel_fakes import Answer, FakeGuest, install_at_run_process
from tests.ssh_fixtures import write_test_ssh_keypair

if TYPE_CHECKING:
    from pathlib import Path

    from agentworks.config import Config
    from agentworks.db import Database

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


@pytest.mark.parametrize("signal_name", ["SIGTERM", "SIGHUP"])
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
    assert signal.getsignal(getattr(signal, signal_name)) == before
