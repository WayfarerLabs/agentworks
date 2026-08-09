"""``_ensure_tailscale`` operator-facing wording on ``vm start``.

The initial connectivity probe is shared between two callers with very
different stories. On a cold start the VM has just booted and tailscaled
genuinely has to reconnect, so "Waiting for Tailscale to reconnect (this
may take several minutes)" is accurate. On an already-running VM nothing
was ever down; the same line reads as scary recovery work, so
``start_vm`` asks for truthful "Verifying Tailscale connectivity" /
"reachable" wording instead.

These pins cover the whole chain: that ``start_vm`` threads the
already-running flag from the observed status, and that the flag selects
the right ``output.detail`` lines. The probe itself and the rejoin
behaviour are unchanged; only the messages differ.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from agentworks import output
from agentworks.capabilities.base import RunContext
from agentworks.db import VMStatus
from agentworks.errors import StateError
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.secrets.policy import InteractionPolicy
from agentworks.vms import manager as vm_manager

if TYPE_CHECKING:
    from collections.abc import Iterator

    from agentworks.capabilities.vm_platform import VMPlatform
    from agentworks.db import Database, VMRow


class _ReachableTarget:
    """Stand-in transport whose ``run`` always succeeds, so
    ``wait_for_reconnect`` returns on its first probe (the usual case on
    a VM that is already up) without touching the rejoin path."""

    def run(self, command: str, **kwargs: object) -> object:
        return None


class _AuthKeys:
    def get(self, name: str) -> str:
        assert name == "tailscale-auth-key"
        return "tskey-test"


def _seed_vm(db: Database) -> VMRow:
    db.insert_vm("box", site="proxmox", hostname="box")
    db.update_vm_tailscale("box", "100.64.0.9")
    vm = db.get_vm("box")
    assert vm is not None
    return vm


def _bound_platform(db: Database, config: object, vm: VMRow) -> VMPlatform:
    """The VM's real bound platform (unused on the reachable path, but
    the honest type for the call)."""
    from agentworks.bootstrap import build_registry
    from agentworks.vms.nodes import live_vm_node

    registry = build_registry(config)  # type: ignore[arg-type]
    return live_vm_node(db, config, registry, vm).site.platform  # type: ignore[arg-type]


def _reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the connectivity probe through an always-reachable target."""
    monkeypatch.setattr("agentworks.transports.transport", lambda vm, config, **k: _ReachableTarget())


def test_already_running_probe_says_verifying_not_reconnect(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The already-running path verifies connectivity in truthful terms
    and never emits the scary reconnect wording."""
    config = make_config()
    vm = _seed_vm(db)
    _reachable(monkeypatch)

    assert not vm_manager._tailscale_rejoin_required(db, config, vm, already_running=True)

    assert any("Verifying Tailscale connectivity" in line for line in captured_output.detail)
    assert any("Tailscale SSH reachable" in line for line in captured_output.detail)
    assert not any("reconnect" in line for line in captured_output.detail)
    assert not any("several minutes" in line for line in captured_output.detail)


def test_cold_start_probe_keeps_reconnect_wording(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    captured_output,  # noqa: ANN001
) -> None:
    """The cold-start default is unchanged: a freshly booted VM may take
    minutes to reconnect, and the wording says so."""
    config = make_config()
    vm = _seed_vm(db)
    _reachable(monkeypatch)

    assert not vm_manager._tailscale_rejoin_required(db, config, vm, already_running=False)

    assert any("Waiting for Tailscale to reconnect" in line for line in captured_output.detail)
    assert any("several minutes" in line for line in captured_output.detail)
    assert any("Tailscale SSH reconnected" in line for line in captured_output.detail)


def test_rejoin_rejects_a_redaction_free_operation_logger(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auth-key command must never cross a logger lacking that redaction."""
    config = make_config()
    vm = _seed_vm(db)
    platform = _bound_platform(db, config, vm)
    monkeypatch.setattr("agentworks.transports.wait_for_reconnect", lambda *a, **k: False)
    monkeypatch.setattr(
        "agentworks.transports.native_transport",
        lambda *a, **k: SimpleNamespace(logger=object()),
    )
    monkeypatch.setattr(vm_manager, "verify_tailscale_available", lambda: None)
    monkeypatch.setattr(
        vm_manager,
        "rejoin_tailscale",
        lambda *a, **k: pytest.fail("secret-bearing rejoin reached a redaction-free logger"),
    )

    with pytest.raises(StateError, match="unexpectedly has an operation logger"):
        vm_manager._ensure_tailscale(
            db,
            config,
            vm,
            platform,
            RunContext(),
            auth_keys=_AuthKeys(),
            auth_key_name="tailscale-auth-key",
        )


@pytest.mark.parametrize(
    ("status", "expected_flag"),
    [(VMStatus.RUNNING, True), (VMStatus.STOPPED, False)],
)
def test_start_vm_threads_already_running_from_status(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    resolve_counter: list[list[str]],  # noqa: ARG001
    status: VMStatus,
    expected_flag: bool,
) -> None:
    """``start_vm`` derives the wording flag from the observed power
    state: already-running verifies, a real start waits to reconnect."""
    config = make_config()
    _seed_vm(db)
    captured: dict[str, object] = {}

    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, row, ctx: status)
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, row, ctx: None)
    monkeypatch.setattr(vm_manager, "_tailscale_rejoin_required", lambda *a, **k: captured.update(k) or False)

    vm_manager.start_vm(db, config, "box", interaction=InteractionPolicy.REFUSE)

    assert captured["already_running"] is expected_flag


def test_start_vm_through_wsl2_shaped_hold_verifies_exactly_once(
    db: Database,
    make_config,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    resolve_counter: list[list[str]],  # noqa: ARG001
    captured_output,  # noqa: ANN001
) -> None:
    """The ``vm start`` operator-visible wording, through a non-trivial hold.

    Drive ``start_vm`` through a non-trivial, WSL2-shaped ``vm_active``
    hold (one that anchors and prints its keepalive lines, NOT the no-op
    default) and confirm the operator sees the connectivity check exactly
    ONCE, in truthful "Verifying" terms, and never the scary reconnect
    wording. This pins the start_vm integration at the operator-visible
    level, not just in kwarg plumbing.

    The reintroduction guard proper (a test that fails if the removed
    ``wait_for_reconnect`` is put back into the REAL ``_keepalive``) lives
    in ``test_wsl2_keepalive.py::test_vm_active_does_not_verify_connectivity``:
    driving ``start_vm`` all the way through the real ``WSL2Platform`` hold
    is impractical here (WSL2 is categorically Windows-only, so its site is
    unavailable on the test host), hence the hand-rolled hold below stands
    in for the WSL2 shape at this layer.
    """
    config = make_config()
    _seed_vm(db)
    _reachable(monkeypatch)

    @contextlib.contextmanager
    def _wsl2_shaped_hold(self: object, vm: VMRow, *, config: object = None) -> Iterator[None]:
        # Mirror the real WSL2 keepalive's operator output: it anchors the
        # distro and says so, but performs no connectivity verification of
        # its own (that is _ensure_tailscale's job, run inside this hold).
        output.detail("Preventing idle-shutdown of WSL2 distro 'box' for the duration of this command...")
        try:
            yield
        finally:
            output.detail("Idle-shutdown prevention stopped.")

    monkeypatch.setattr(ProxmoxPlatform, "status", lambda self, row, ctx: VMStatus.RUNNING)
    monkeypatch.setattr(ProxmoxPlatform, "start", lambda self, row, ctx: None)
    monkeypatch.setattr(ProxmoxPlatform, "vm_active", _wsl2_shaped_hold)

    vm_manager.start_vm(db, config, "box", interaction=InteractionPolicy.REFUSE)

    verifying = [line for line in captured_output.detail if "Verifying Tailscale connectivity" in line]
    assert len(verifying) == 1, f"expected exactly one verify line, got {verifying}"
    assert not any("Waiting for Tailscale to reconnect" in line for line in captured_output.detail)
    assert not any("several minutes" in line for line in captured_output.detail)
