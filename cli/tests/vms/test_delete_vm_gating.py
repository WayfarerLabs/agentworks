"""``delete_vm`` cleanup discipline: never gates, never lets a
best-effort step (the build-and-boundary composition, the hold, the
logout) skip the backend delete, keeps the SIGINT contract at a
site-secret prompt, and never deletes the row past a FAILED backend
delete (the one non-best-effort step: a raise from ``platform.delete``
keeps the row so the surviving backend VM stays reachable, #329).

Real config, registry, resolver, and backend loop (env-var backend);
the platform's backend ops and the Tailscale logout are the fakes,
mirroring ``test_lifecycle_orchestrated.py`` (delete shares the
lifecycle commands' composition root, ``_live_vm_boundary``). One
end-to-end azure pin at the bottom drops one level lower: the REAL
``AzureVMPlatform.delete`` (with its #329 verification gate) runs
through this same manager path, with only the Azure SDK faked
(``tests._azure_platform_support``), composing the two halves the
suites above and ``test_azure_delete_verify.py`` pin separately.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest

from agentworks.db import VMStatus
from agentworks.errors import AuthorizationError, UserAbort
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.vms import manager as vm_manager
from tests._azure_platform_support import _RESOURCE_ID, _authorization_denied, _install_fakes
from tests.orchestrated_fixtures import write_operator_config

if TYPE_CHECKING:
    from pathlib import Path

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


def _failing_backend_delete(monkeypatch: pytest.MonkeyPatch, counts: dict[str, int]) -> AuthorizationError:
    """Make the platform's backend delete raise the typed error azure's
    #329 verification gate raises (the backend VM survived the teardown
    attempt), returning the instance so tests can assert identity."""
    error = AuthorizationError("Azure refused to delete VM 'dvm'")

    def _refused(self: ProxmoxPlatform, row: VMRow, ctx: object) -> None:
        counts["delete"] += 1
        raise error

    monkeypatch.setattr(ProxmoxPlatform, "delete", _refused)
    return error


def test_backend_delete_failure_keeps_the_row(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """#329: the backend delete is the one step delete_vm must NOT
    treat as best-effort. A platform delete that raises (it could not
    remove the backend VM) aborts the command and keeps the row, so
    the operator can fix the cause and retry; warning past it would
    orphan the surviving VM with nothing left to target it."""
    _seed(db)
    counts = _fake_backend(monkeypatch)
    monkeypatch.setattr(vm_manager, "_tailscale_logout", lambda *a, **k: None)
    error = _failing_backend_delete(monkeypatch, counts)

    with pytest.raises(AuthorizationError) as exc:
        vm_manager.delete_vm(db, make_config(), "dvm", yes=True)

    assert exc.value is error
    assert counts["delete"] == 1
    assert db.get_vm("dvm") is not None


def test_force_does_not_suppress_backend_delete_failure(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """--force skips the child-count guard and the confirm prompt,
    never a failed backend delete: the #329 repro used --force and the
    failure must still surface with the row kept."""
    _seed(db)
    counts = _fake_backend(monkeypatch)
    monkeypatch.setattr(vm_manager, "_tailscale_logout", lambda *a, **k: None)
    _failing_backend_delete(monkeypatch, counts)

    with pytest.raises(AuthorizationError):
        vm_manager.delete_vm(db, make_config(), "dvm", force=True)

    assert db.get_vm("dvm") is not None


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


def test_azure_rbac_delete_failure_keeps_the_row_end_to_end(
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_output: CapturedOutput,
) -> None:
    """The #329 seam composed end to end for azure: the RBAC-refused
    backend delete surfaces through the platform's REAL verification
    gate (``verify_vm_deleted``) and the real manager delete path as
    the typed ``AuthorizationError``, aborting the command with the
    row kept. Real config (azure plugin enabled, its ``[azure]`` site
    section), registry, resolver, and boundary; only the Azure SDK is
    faked. The suites above fake ``platform.delete`` wholesale and
    ``test_azure_delete_verify.py`` stops at the platform, so this is
    the one pin proving the two halves meet."""
    config = write_operator_config(
        tmp_path,
        '[plugins]\nsystem = ["azure"]\n\n'
        "[azure]\n"
        'subscription_id = "sub-A"\n'
        'resource_group = "rg1"\n'
        'region = "eastus"\n',
    )
    # No tailscale host: the best-effort logout span never opens, so
    # nothing here needs the transport stubs.
    db.insert_vm("vm1", site="azure", hostname="vm1")
    db.update_vm_platform_metadata("vm1", {"resource_id": _RESOURCE_ID})
    fakes = _install_fakes(monkeypatch)  # the probe serves the VM back: it survived
    fakes.compute.virtual_machines.delete_error = _authorization_denied()

    with pytest.raises(AuthorizationError) as exc:
        vm_manager.delete_vm(db, config, "vm1", yes=True)

    assert "AuthorizationFailed" in str(exc.value)
    assert exc.value.hint is not None
    assert "Contributor" in exc.value.hint
    assert db.get_vm("vm1") is not None
