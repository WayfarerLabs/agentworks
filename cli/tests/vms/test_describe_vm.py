"""``describe_vm``'s live reads: the SSH resource query is skipped when
the status probe already observed the VM stopped; connecting to a
dead host burns the transport's connect timeout (times its retries)
just to print the '-' placeholders.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.config import load_config
from agentworks.db import AppliedStateKey, VMStatus
from agentworks.secrets.policy import TtyInteractionPolicy
from agentworks.vms import manager as vm_manager
from tests.conftest import stub_vm_ssh_identity

if TYPE_CHECKING:
    from agentworks.config import Config
    from agentworks.db import Database, VMRow


@pytest.fixture(autouse=True)
def _stub_ssh_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_vm_ssh_identity(monkeypatch)


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    key = tmp_path / "id_ed25519"
    key.write_text("private")
    (tmp_path / "id_ed25519.pub").write_text("public")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    path = tmp_path / "config.toml"
    path.write_text(f'[operator]\nssh_public_key = "{key}.pub"\nssh_private_key = "{key}"\n')
    return load_config(path, warn_issues=False, warn_deprecations=False)


class _Platform:
    def __init__(self, status: VMStatus, outside_snapshot=None) -> None:  # noqa: ANN001
        self._status = status
        self._outside_snapshot = outside_snapshot

    def _check_boundary(self, operation: str) -> None:
        if self._outside_snapshot is not None:
            self._outside_snapshot(operation)

    def preflight(self, ctx: object) -> None:
        self._check_boundary("preflight")
        return None

    def display_backend_name(self, vm: VMRow) -> str:
        self._check_boundary("provider-backend")
        return vm.name

    def status(self, vm: VMRow, ctx: object) -> VMStatus:
        self._check_boundary("provider-status")
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
    # The orchestrated root reaches the platform through the node's
    # site edge, whose only constructor is resolve_site.
    monkeypatch.setattr(
        "agentworks.vms.sites.resolve_site",
        lambda name, registry: _Platform(status),
    )
    calls: list[str] = []

    def _fake_live(database: Database, vm: VMRow, cfg: Config) -> None:
        assert database is db
        calls.append(vm.name)
        return None

    monkeypatch.setattr(vm_manager, "_query_live_resources", _fake_live)
    vm_manager.describe_vm(db, config, "dvm", interaction=TtyInteractionPolicy.REFUSE)
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


def test_vm_description_closes_structural_snapshot_before_external_reads(
    db: Database,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agentworks.db import Database as DatabaseType
    from agentworks.secrets.resolver import Resolver
    from agentworks.ssh_identity import VerifiedSSHIdentity
    from agentworks.vms.applied_state import encode_ssh_identity

    fingerprint = f"SHA256:{'A' * 43}"
    db.insert_vm("dvm", site="lima-local", hostname="dvm")
    db.update_vm_tailscale("dvm", "100.64.0.9")
    db.instance_state.replace_applied_slices(
        "vm",
        "dvm",
        "vm-create",
        {AppliedStateKey.SSH_IDENTITY: encode_ssh_identity("/key", VerifiedSSHIdentity(fingerprint))},
    )

    snapshot_depth = 0
    external_events: list[str] = []
    original_snapshot = DatabaseType.snapshot

    @contextmanager
    def tracked_snapshot(database: Database):
        nonlocal snapshot_depth
        with original_snapshot(database):
            snapshot_depth += 1
            try:
                yield
            finally:
                snapshot_depth -= 1

    def outside_snapshot(operation: str) -> None:
        assert snapshot_depth == 0, f"{operation} ran inside the structural snapshot"
        external_events.append(operation)

    platform = _Platform(VMStatus.RUNNING, outside_snapshot)
    monkeypatch.setattr(DatabaseType, "snapshot", tracked_snapshot)
    monkeypatch.setattr("agentworks.vms.sites.resolve_site", lambda name, registry: platform)
    monkeypatch.setattr(
        "agentworks.ssh_identity.read_private_ssh_identity",
        lambda path: outside_snapshot("ssh-identity") or VerifiedSSHIdentity(fingerprint),
    )
    real_resolve = Resolver.resolve

    def checked_resolve(self: Resolver):  # noqa: ANN202
        outside_snapshot("secret-resolution")
        return real_resolve(self)

    monkeypatch.setattr(Resolver, "resolve", checked_resolve)

    def query_live(database: Database, vm: VMRow, cfg: Config) -> None:
        outside_snapshot("runtime-resources")
        return None

    monkeypatch.setattr(vm_manager, "_query_live_resources", query_live)

    vm_manager.vm_description(db, config, "dvm", interaction=TtyInteractionPolicy.REFUSE)

    assert snapshot_depth == 0
    assert set(external_events) == {
        "ssh-identity",
        "preflight",
        "secret-resolution",
        "provider-backend",
        "provider-status",
        "runtime-resources",
    }
