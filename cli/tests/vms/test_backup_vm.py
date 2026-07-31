"""``backup_vm``'s precondition ordering: deterministic fatal checks
run before the composition root; ``gated_vm_boundary`` opens the gate
and runs the boundary resolve pass, which can prompt for site secrets,
and the operator must never answer a prompt for a backup the row
already sank.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.errors import StateError
from agentworks.vms import backup as vm_backup

if TYPE_CHECKING:
    from agentworks.db import Database


def test_missing_tailscale_fails_before_the_boundary(
    db: Database, monkeypatch: pytest.MonkeyPatch, captured_output: object
) -> None:
    db.insert_vm("bvm", site="lima-local", hostname="bvm")  # no tailscale

    def _no_boundary(*args: object, **kwargs: object) -> None:
        raise AssertionError("the boundary opened (and possibly prompted) before the guard")

    monkeypatch.setattr(vm_backup, "gated_vm_boundary", _no_boundary)

    with pytest.raises(StateError, match="no Tailscale address"):
        vm_backup.backup_vm(db, object(), "bvm")  # type: ignore[arg-type]
