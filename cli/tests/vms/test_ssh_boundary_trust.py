"""``require_vm_ssh_boundary`` is the precondition the gate-opening commands run
before any gate opens or secret resolves; a VM with no host-key record is
refused there."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks.execution.errors import TrustMissing
from agentworks.vms.manager.boundary import require_vm_ssh_boundary
from tests.conftest import seed_vm_trust

if TYPE_CHECKING:
    from agentworks.db import Database

_CONFIG = SimpleNamespace(operator=SimpleNamespace(ssh_private_key=None))


def _vm(db: Database) -> object:
    db.insert_vm("web", site="proxmox", hostname="web")
    vm = db.get_vm("web")
    assert vm is not None
    return vm


def test_a_vm_without_a_record_is_refused_before_the_identity_check(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    vm = _vm(db)

    def _identity_must_not_run(*_a: object, **_k: object) -> None:
        raise AssertionError("the identity comparison ran before the record check")

    monkeypatch.setattr("agentworks.vms.applied_state.compare_vm_ssh_identity", _identity_must_not_run)

    with pytest.raises(TrustMissing):
        require_vm_ssh_boundary(db, _CONFIG, vm)  # type: ignore[arg-type]


def test_a_vm_with_a_record_goes_on_to_the_identity_check(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    vm = _vm(db)
    seed_vm_trust("web")
    checked: list[str] = []
    monkeypatch.setattr(
        "agentworks.vms.applied_state.compare_vm_ssh_identity", lambda _db, name, _key: checked.append(name)
    )
    monkeypatch.setattr("agentworks.vms.applied_state.require_vm_ssh_identity", lambda *_a, **_k: None)

    require_vm_ssh_boundary(db, _CONFIG, vm)  # type: ignore[arg-type]

    assert checked == ["web"]
