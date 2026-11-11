"""Preview and apply share local trust prerequisites without guest side effects."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agentworks.db import DesiredRuntimeState, VMStatus
from agentworks.errors import StateError
from agentworks.execution import trust
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.runtime_state import reconcile_console, reconcile_session, reconcile_vm
from agentworks.runtime_state._models import Outcome
from tests.console_helpers import create_console_record
from tests.runtime_state.test_composition import REFUSE, seed_session, seed_vm

if TYPE_CHECKING:
    from agentworks.db import Database


def _snapshot(db: Database) -> tuple[object, ...]:
    consoles = db.list_consoles()
    return (
        db.list_vms(),
        db.list_sessions(),
        consoles,
        [db.list_console_sessions(console.name) for console in consoles],
        db._conn.total_changes,
    )


def _forbid_preview_mutations(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("preview attempted a runtime mutation or guest operation")

    for target in (
        "agentworks.harness_setup.locking.native_mutation_guard",
        "agentworks.transports.transport",
        "agentworks.secrets.resolver.Resolver.resolve",
    ):
        monkeypatch.setattr(target, forbidden)
    for name in ("start", "stop", "vm_active"):
        monkeypatch.setattr(ProxmoxPlatform, name, forbidden)


@pytest.mark.parametrize("scope", ["vm", "console"])
def test_missing_trust_preview_refuses_before_provider_credentials_or_queries(
    db, make_config, monkeypatch, resolve_counter, scope
):
    target = DesiredRuntimeState.RUNNING if scope == "vm" else DesiredRuntimeState.ON_DEMAND
    seed_vm(db, "missing", target=target)
    if scope == "console":
        create_console_record(db, name="view", vm_name="missing", session_specs=[], add_admin_shell=True)
    trust.remove("missing")
    before = _snapshot(db)
    config = make_config()
    _forbid_preview_mutations(monkeypatch)
    monkeypatch.setattr(ProxmoxPlatform, "status", lambda *a: pytest.fail("queried locally blocked provider"))
    monkeypatch.setattr(
        "agentworks.secrets.preview.preview_batch", lambda *a, **k: pytest.fail("previewed a secret source")
    )
    rendered = []
    monkeypatch.setattr("agentworks.runtime_state._preview.render_results", lambda rows, **k: rendered.extend(rows))

    with pytest.raises(StateError):
        if scope == "vm":
            reconcile_vm(db, config, name="missing", dry_run=True, interaction=REFUSE)
        else:
            reconcile_console(db, config, name="view", dry_run=True, interaction=REFUSE)

    assert resolve_counter == []
    assert _snapshot(db) == before
    assert not (db.path.parent / f".{db.path.name}.native-locks").exists()
    assert rendered and all(row.outcome is not Outcome.CHANGED for row in rendered)
    assert next(row for row in rendered if row.kind == "vm").outcome is Outcome.FAILED
    if scope == "console":
        assert next(row for row in rendered if row.kind == "console").outcome is Outcome.BLOCKED


def test_preview_missing_trust_isolates_independent_provider_family(db, make_config, monkeypatch, resolve_counter):
    seed_vm(db, "missing", target=DesiredRuntimeState.ON_DEMAND)
    create_console_record(db, name="view", vm_name="missing", session_specs=[], add_admin_shell=True)
    seed_vm(db, "healthy")
    trust.remove("missing")
    before = _snapshot(db)
    _forbid_preview_mutations(monkeypatch)
    providers = []

    def status(self, vm, ctx):
        providers.append(vm.name)
        return VMStatus.STOPPED

    monkeypatch.setattr(ProxmoxPlatform, "status", status)
    rendered = []
    monkeypatch.setattr("agentworks.runtime_state._preview.render_results", lambda rows, **k: rendered.extend(rows))

    with pytest.raises(StateError):
        reconcile_vm(db, make_config(), all_vms=True, dry_run=True, interaction=REFUSE)

    assert providers == ["healthy"]
    assert resolve_counter == [["proxmox-token"]]
    assert _snapshot(db) == before
    assert not (db.path.parent / f".{db.path.name}.native-locks").exists()
    outcomes = {(row.kind, row.name): row.outcome for row in rendered}
    assert outcomes[("vm", "missing")] is Outcome.FAILED
    assert outcomes[("console", "view")] is Outcome.BLOCKED
    assert outcomes[("vm", "healthy")] is Outcome.CHANGED


@pytest.mark.parametrize("scope", ["stop-vm", "idle-vm", "stopped-session", "stopped-console"])
def test_provider_only_preview_does_not_require_host_trust(db, make_config, monkeypatch, resolve_counter, scope):
    target = {
        "stop-vm": DesiredRuntimeState.STOPPED,
        "idle-vm": DesiredRuntimeState.ON_DEMAND,
    }.get(scope, DesiredRuntimeState.RUNNING)
    seed_vm(db, "provider", target=target)
    if scope == "stopped-session":
        seed_session(db, "child", "provider", target=DesiredRuntimeState.STOPPED)
    elif scope == "stopped-console":
        create_console_record(db, name="view", vm_name="provider", session_specs=[], add_admin_shell=True)
        db.set_console_desired_runtime_state("view", DesiredRuntimeState.STOPPED)
    trust.remove("provider")
    before = _snapshot(db)
    _forbid_preview_mutations(monkeypatch)
    providers = []

    def status(self, vm, ctx):
        providers.append(vm.name)
        return VMStatus.RUNNING if scope == "stop-vm" else VMStatus.STOPPED

    monkeypatch.setattr(ProxmoxPlatform, "status", status)
    config = make_config()
    if scope == "stopped-session":
        reconcile_session(db, config, name="child", dry_run=True, interaction=REFUSE)
    elif scope == "stopped-console":
        reconcile_console(db, config, name="view", dry_run=True, interaction=REFUSE)
    else:
        reconcile_vm(db, config, name="provider", dry_run=True, interaction=REFUSE)

    assert providers == ["provider"]
    assert resolve_counter == [["proxmox-token"]]
    assert _snapshot(db) == before
    assert not (db.path.parent / f".{db.path.name}.native-locks").exists()
