"""``rekey_vm``'s composition-root ordering: the is-it-running check is
an op (a backend status read; on proxmox it needs the token), so it runs
PAST the preflight boundary: after the one resolve pass, never before.
The trade (a stopped-VM error lands after the prompt session) was ruled
preferable to a second prompt session, which the contract forbids.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agentworks.config import load_config
from agentworks.db import VMStatus
from agentworks.errors import StateError
from agentworks.vms import manager as vm_manager

if TYPE_CHECKING:
    from agentworks.db import Database, VMRow


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    key = tmp_path / "id_ed25519"
    key.write_text("private")
    (tmp_path / "id_ed25519.pub").write_text("public")
    monkeypatch.setenv("AW_SECRET_TAILSCALE_AUTH_KEY", "tskey-test")
    path = tmp_path / "config.toml"
    path.write_text(
        f'[operator]\nssh_public_key = "{key}.pub"\nssh_private_key = "{key}"\n'
    )
    return load_config(path, warn_issues=False, warn_deprecations=False)


def test_rekey_running_check_runs_after_the_resolve_boundary(
    db: Database, config, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    from agentworks.secrets.resolver import Resolver

    db.insert_vm("rvm", site="lima", hostname="rvm")
    order: list[str] = []

    class _StoppedPlatform:
        name = "stub"

        def preflight(self, ctx: object) -> None:
            order.append("preflight")

        def status(self, vm: VMRow) -> VMStatus:
            order.append("status")
            return VMStatus.STOPPED

    monkeypatch.setattr(
        vm_manager,
        "bind_platform",
        lambda config, vm, registry=None, resolver=None, prepare=True: _StoppedPlatform(),
    )

    real_resolve = Resolver.resolve

    def _spying_resolve(self: Resolver) -> None:
        order.append("resolve")
        real_resolve(self)

    monkeypatch.setattr(Resolver, "resolve", _spying_resolve)

    with pytest.raises(StateError, match="is not running"):
        vm_manager.rekey_vm(db, config, "rvm")

    # The boundary (preflight, then the one resolve pass) fully
    # precedes the status op; nothing re-resolves afterwards.
    assert order == ["preflight", "resolve", "status"]
