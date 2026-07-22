"""``delete_vm`` cleanup discipline: never gates, never lets a
best-effort step (the build-and-boundary composition, the hold, the
logout) skip the backend delete, and keeps the SIGINT contract at a
site-secret prompt.

Real config, registry, resolver, and backend loop (env-var backend);
the platform's backend ops and the Tailscale logout are the fakes,
mirroring ``test_lifecycle_orchestrated.py`` (delete shares the
lifecycle commands' composition root, ``_live_vm_boundary``).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

from agentworks.capabilities.vm_platform.proxmox import ProxmoxPlatform
from agentworks.db import VMStatus
from agentworks.errors import UserAbort
from agentworks.vms import manager as vm_manager

if TYPE_CHECKING:
    from agentworks.db import Database, VMRow
    from tests.conftest import CapturedOutput


@pytest.fixture(autouse=True)
def _no_ssh_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentworks.ssh_config.sync_ssh_config", lambda *a, **k: None)


def _seed(db: Database, *, site: str = "proxmox") -> None:
    db.insert_vm("dvm", site=site, hostname="dvm")
    db.update_vm_tailscale("dvm", "100.64.0.3")
    db.set_operator_stopped("dvm", True)  # must not matter: delete never gates


def _fake_backend(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Fake the platform's backend ops with call counters; the delete
    choreography above them runs for real."""
    counts = {"status": 0, "delete": 0}

    # The counter is the never-probes oracle (asserted zero where it
    # matters); a raise here would be swallowed by delete's best-effort
    # spans and could never signal anything.
    def _status(self: ProxmoxPlatform, row: VMRow, ctx: object) -> VMStatus:
        counts["status"] += 1
        return VMStatus.STOPPED

    def _delete(self: ProxmoxPlatform, row: VMRow, ctx: object) -> None:
        counts["delete"] += 1

    monkeypatch.setattr(ProxmoxPlatform, "status", _status)
    monkeypatch.setattr(ProxmoxPlatform, "delete", _delete)
    return counts


def test_delete_never_gates(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """An operator-stopped VM deletes cleanly: no gate, no StateError,
    no status probe, no start. The union still resolves, exactly once,
    at the boundary (the site's config secret feeds the backend
    delete): the delete-shaped mirror of the gate-prompt parity carry,
    whose gate burst is exactly absent."""
    _seed(db)
    counts = _fake_backend(monkeypatch)
    monkeypatch.setattr(vm_manager, "_tailscale_logout", lambda *a, **k: None)

    vm_manager.delete_vm(db, make_config(), "dvm", yes=True)

    assert counts["status"] == 0
    assert counts["delete"] == 1
    assert resolve_counter == [["proxmox-token"]]
    assert db.get_vm("dvm") is None


def test_hold_failure_does_not_skip_delete(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """A broken hold (e.g. a manually unregistered WSL2 distro) is
    exactly what delete cleans up: warn and keep going."""
    _seed(db)
    counts = _fake_backend(monkeypatch)

    def _broken_hold(
        self: ProxmoxPlatform, row: VMRow, *, config: object | None = None
    ) -> contextlib.AbstractContextManager[None]:
        raise RuntimeError("keepalive exited immediately")

    monkeypatch.setattr(ProxmoxPlatform, "vm_active", _broken_hold)

    vm_manager.delete_vm(db, make_config(), "dvm", yes=True)

    assert counts["delete"] == 1
    assert db.get_vm("dvm") is None
    assert any("logout skipped" in w for w in captured_output.warnings)


def test_logout_failure_does_not_skip_delete(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    _seed(db)
    counts = _fake_backend(monkeypatch)

    def _boom(*a: object, **k: object) -> None:
        raise RuntimeError("transport exploded")

    monkeypatch.setattr(vm_manager, "_tailscale_logout", _boom)

    vm_manager.delete_vm(db, make_config(), "dvm", yes=True)

    assert counts["delete"] == 1
    assert db.get_vm("dvm") is None


def test_stranded_site_warns_with_hint_and_still_deletes_row(
    db: Database,
    make_config,  # noqa: ANN001
    resolve_counter: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """A stranded site degrades: the build fails inside the best-effort
    boundary, backend cleanup is skipped with the manifest hint
    rendered, no secret ever resolves, and the DB row still goes."""
    _seed(db, site="gone")

    vm_manager.delete_vm(db, make_config(), "dvm", yes=True)

    assert db.get_vm("dvm") is None
    assert resolve_counter == []
    joined = "\n".join(captured_output.warnings)
    assert "skipping backend cleanup" in joined
    assert "kind: vm-site" in joined


def test_user_abort_at_boundary_prompt_aborts_the_delete(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """Ctrl-C at the boundary's secret prompt (inside the one resolve
    pass) aborts the whole delete rather than orphaning the backend VM
    behind a warn."""
    _seed(db)
    _fake_backend(monkeypatch)

    def _abort(*a: object, **k: object) -> dict[str, str]:
        raise UserAbort("cancelled at prompt")

    monkeypatch.setattr("agentworks.secrets.resolve.resolve_secrets", _abort)

    with pytest.raises(UserAbort):
        vm_manager.delete_vm(db, make_config(), "dvm", yes=True)

    assert db.get_vm("dvm") is not None


def test_user_abort_inside_an_op_span_aborts_the_delete(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """The op-span catch-alls are best-effort ("warn and continue") but
    must NOT downgrade a UserAbort: a swallowed abort would fall through
    and delete the DB row the operator just declined. Pinned for both
    best-effort spans (the logout hold and the backend delete)."""
    _seed(db)
    config = make_config()
    counts = _fake_backend(monkeypatch)
    monkeypatch.setattr(vm_manager, "_tailscale_logout", lambda *a, **k: None)

    def _aborting_delete(self: ProxmoxPlatform, row: VMRow, ctx: object) -> None:
        counts["delete"] += 1
        raise UserAbort("cancelled mid-op")

    monkeypatch.setattr(ProxmoxPlatform, "delete", _aborting_delete)

    with pytest.raises(UserAbort):
        vm_manager.delete_vm(db, config, "dvm", yes=True)
    assert counts["delete"] == 1
    assert db.get_vm("dvm") is not None

    # Same contract at the hold+logout span.
    counts2 = _fake_backend(monkeypatch)

    def _abort_logout(*a: object, **k: object) -> None:
        raise UserAbort("cancelled during logout")

    monkeypatch.setattr(vm_manager, "_tailscale_logout", _abort_logout)

    with pytest.raises(UserAbort):
        vm_manager.delete_vm(db, config, "dvm", yes=True)
    assert db.get_vm("dvm") is not None
    assert counts2["delete"] == 0  # aborted before the backend delete
