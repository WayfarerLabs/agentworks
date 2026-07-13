"""``describe_vm``'s live reads: the SSH resource query is skipped when
the status probe already observed the VM stopped -- connecting to a
dead host burns the transport's connect timeout (times its retries)
just to print the '-' placeholders.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.config import load_config
from agentworks.db import VMStatus
from agentworks.vms import manager as vm_manager

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, VMRow


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    key = tmp_path / "id_ed25519"
    key.write_text("private")
    (tmp_path / "id_ed25519.pub").write_text("public")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    path = tmp_path / "config.toml"
    path.write_text(
        f'[operator]\nssh_public_key = "{key}.pub"\nssh_private_key = "{key}"\n'
    )
    return load_config(path, warn_issues=False, warn_deprecations=False)


class _Platform:
    def __init__(self, status: VMStatus) -> None:
        self._status = status

    def display_backend_name(self, vm: VMRow) -> str:
        return vm.name

    def status(self, vm: VMRow) -> VMStatus:
        return self._status


def _describe(
    db: Database,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: VMStatus,
) -> list[str]:
    """Run describe against a stubbed platform; return the names the
    live-resource query was invoked for."""
    db.insert_vm("dvm", site="lima-local", hostname="dvm")
    db.update_vm_tailscale("dvm", "100.64.0.9")
    monkeypatch.setattr(
        vm_manager,
        "bind_platform",
        lambda cfg, vm, registry=None: _Platform(status),
    )
    calls: list[str] = []

    def _fake_live(vm: VMRow, cfg: Config) -> None:
        calls.append(vm.name)
        return None

    monkeypatch.setattr(vm_manager, "_query_live_resources", _fake_live)
    vm_manager.describe_vm(db, config, "dvm")
    return calls


def test_stopped_vm_skips_the_live_ssh_read(
    db: Database,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    calls = _describe(db, config, monkeypatch, status=VMStatus.STOPPED)
    assert calls == []


def test_running_vm_still_reads_live_resources(
    db: Database,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: object,
) -> None:
    calls = _describe(db, config, monkeypatch, status=VMStatus.RUNNING)
    assert calls == ["dvm"]
