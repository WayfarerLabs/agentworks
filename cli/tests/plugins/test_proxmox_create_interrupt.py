"""Proxmox ``create`` rollback on failure and interrupt (#340).

``create``'s only backend artifact is the cloned VM (one VMID on one
node), made by the clone step and mutated in place after it. The caller
(``create_vm``) deletes only the DB row on an unwind, so a clone left
behind would be orphaned with nothing to target it. ``create``
therefore tears the VMID down on BOTH a plain failure (typed error
re-raised as-is; the manager wraps foreign ones) and a
``KeyboardInterrupt`` across the whole span (clone through the
guest-agent bootstrap wait), re-raising the ORIGINAL interrupt; a
SECOND interrupt during the cleanup abandons it loudly, naming the node
and VMID. A cleanup that cannot remove the VMID, abandoned or failed,
hands it to core as ``RetainedProvisioningError``, so the row survives
for ``vm delete`` to retry. An interrupt while a
task is still in flight (likeliest: the minutes-long full clone)
cancels the task first so the target VMID unlocks before the delete.

Test shape mirrors ``test_azure_create_interrupt.py``. The fake below
is a recording ``ProxmoxAPI`` stand-in injected through the platform's
``_api`` accessor; the API-client wire tests live in
``test_proxmox_api.py``; no test here touches the network.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest, RetainedProvisioningError
from agentworks.debian import DebianRelease
from agentworks.errors import ProvisioningError, StateError
from agentworks.plugins.proxmox.api import ProxmoxAPIError
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from tests._proxmox_fakes import (
    CLONE_UPID,
    NEWID,
    PROXMOX_CONFIG,
    RESIZE_UPID,
    START_UPID,
    TEMPLATE_VMID,
    FakeProxmoxAPI,
)
from tests._root_channel_fakes import Answer, FakeGuest
from tests.conftest import provisioning_trust_for_tests

if TYPE_CHECKING:
    from agentworks.db import VMRow
    from tests.conftest import CapturedOutput

_SENTINEL = "tskey-proxmox-create-'sentinel"


def _assert_exception_graph_is_value_free(failure: BaseException) -> None:
    pending = [failure]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        assert _SENTINEL not in repr(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)


def _assert_template_untouched(fake: FakeProxmoxAPI) -> None:
    """#340's catastrophic-regression fence: no rollback path may ever
    stop or delete the template VMID, only the clone's."""
    assert ("delete_vm", "pve1", TEMPLATE_VMID) not in fake.calls
    assert ("stop_vm", "pve1", TEMPLATE_VMID) not in fake.calls


def _platform_with_fake(monkeypatch: pytest.MonkeyPatch) -> tuple[ProxmoxPlatform, FakeProxmoxAPI]:
    fake = FakeProxmoxAPI()
    monkeypatch.setattr(ProxmoxPlatform, "_api", lambda self, ctx: fake)
    return ProxmoxPlatform("pve-site", dict(PROXMOX_CONFIG)), fake


def _request(*, tailscale: bool) -> ProvisionRequest:
    """With a Tailscale key create runs the guest-agent bootstrap wait
    (the longest window); without one it returns straight after the
    readiness waits."""
    return ProvisionRequest(
        vm_name="vm1",
        debian_release=DebianRelease.TRIXIE,
        hostname="vm1",
        system_slug=None,
        admin_username="agentworks",
        ssh_public_key="ssh-ed25519 AAAA test",
        ssh_private_key=Path("/dev/null"),
        provisioning_trust=provisioning_trust_for_tests("vm1"),
        tailscale_auth_key=_SENTINEL if tailscale else "tskey-test",
        progress=MagicMock(),
        # The vm-template layer's resolved defaults, which is the only
        # shape a platform ever sees (the hardware fields are required).
        cpus=4,
        memory_gib=8,
        disk_gib=50,
        swap_gib=4,
    )


def _interrupt_the_bootstrap(monkeypatch: pytest.MonkeyPatch) -> KeyboardInterrupt:
    """Make the guest-agent bootstrap wait raise, returning the instance
    so tests can assert the ORIGINAL interrupt is what propagates."""
    interrupt = KeyboardInterrupt("first")

    def _raise(run_as_root: object, **kwargs: object) -> None:
        raise interrupt

    monkeypatch.setattr("agentworks.plugins.proxmox.platform.run_bootstrap", _raise)
    return interrupt


class TestInterruptDuringBootstrapWait:
    def test_stops_and_deletes_the_cloned_vm_and_reraises(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """The issue's scenario: the VM is cloned, configured, and
        running when the bootstrap wait is interrupted, so the rollback
        stops it, deletes the VMID, and the interrupt propagates for the
        caller's row unwind. No task is in flight, so no cancel."""
        platform, fake = _platform_with_fake(monkeypatch)
        interrupt = _interrupt_the_bootstrap(monkeypatch)

        with pytest.raises(KeyboardInterrupt) as exc:
            platform.create(_request(tailscale=True), RunContext())

        assert exc.value is interrupt
        assert ("stop_vm", "pve1", NEWID) in fake.calls
        assert ("delete_vm", "pve1", NEWID) in fake.calls
        assert fake.ops().index("stop_vm") < fake.ops().index("delete_vm")
        assert "stop_task" not in fake.ops()
        _assert_template_untouched(fake)

    def test_second_interrupt_abandons_cleanup_and_retains_the_row(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """A second Ctrl-C during the cleanup abandons it instead of
        wedging: no further API calls are made, the warning names the
        node and VMID, and the VMID is handed to core so the row
        survives for ``vm delete``."""
        platform, fake = _platform_with_fake(monkeypatch)
        _interrupt_the_bootstrap(monkeypatch)
        fake.stop_error = KeyboardInterrupt("second")

        with pytest.raises(RetainedProvisioningError) as exc:
            platform.create(_request(tailscale=True), RunContext())

        assert exc.value.platform_metadata == {"vmid": str(NEWID), "node": "pve1"}
        # Abandoned at the stop: nothing else was touched.
        assert "delete_vm" not in fake.ops()
        assert "stop_task" not in fake.ops()
        assert _retention_warned(captured_output, interrupted=True)


def _retention_warned(captured_output: CapturedOutput, *, interrupted: bool) -> bool:
    """Whether a warning names the VMID and node, besides an interrupt's notice, which names them too.

    The interrupt path warns its notice first, before any cleanup; the
    failure path has none.
    """
    warnings = captured_output.warnings[1:] if interrupted else captured_output.warnings
    return any(f"VM {NEWID}" in w and "node 'pve1'" in w for w in warnings)


class TestFailClosedBootstrap:
    def test_cloud_init_that_does_not_complete_rolls_back_before_the_bootstrap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        fake.guest = FakeGuest(lambda payload, seen: Answer(status=None))

        with pytest.raises(ProvisioningError) as caught:
            platform.create(_request(tailscale=True), RunContext())

        assert ("stop_vm", "pve1", NEWID) in fake.calls
        assert ("delete_vm", "pve1", NEWID) in fake.calls
        assert fake.guest.payloads == ["cloud-init status --wait"]
        _assert_exception_graph_is_value_free(caught.value)

    @pytest.mark.parametrize(
        "answer",
        [Answer(status=None), Answer(status=1, stdout=b"##STEP## Hostname\n##ERROR## reflected\n")],
        ids=("unknown", "failed"),
    )
    def test_bootstrap_failure_rolls_back_before_the_key_is_sent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        captured_output: CapturedOutput,
        answer: Answer,
    ) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        fake.guest = FakeGuest(lambda payload, seen: answer)

        with pytest.raises(ProvisioningError) as caught:
            platform.create(_request(tailscale=True), RunContext())

        assert ("stop_vm", "pve1", NEWID) in fake.calls
        assert ("delete_vm", "pve1", NEWID) in fake.calls
        assert fake.ops().index("stop_vm") < fake.ops().index("delete_vm")
        (script,) = fake.guest.scripts
        assert b"AGW_SECRET" not in script
        _assert_exception_graph_is_value_free(caught.value)
        assert _SENTINEL not in repr(captured_output.lines)
        _assert_template_untouched(fake)

    def test_bootstrap_failure_cleanup_survivor_is_retained_without_the_key(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        fake.guest = FakeGuest(lambda payload, seen: Answer(status=None))
        fake.delete_error = ProxmoxAPIError("VM is locked")

        with pytest.raises(RetainedProvisioningError) as caught:
            platform.create(_request(tailscale=True), RunContext())

        assert caught.value.platform_metadata == {"vmid": str(NEWID), "node": "pve1"}
        _assert_exception_graph_is_value_free(caught.value)
        warned = "\n".join(captured_output.warnings)
        assert f"VM {NEWID}" in warned
        assert "node 'pve1'" in warned
        assert _SENTINEL not in warned


class TestInterruptDuringCloneTask:
    def test_cancels_the_inflight_clone_then_deletes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An interrupt while the clone task is still running: the task
        holds a lock on the target VMID, so the rollback cancels it,
        waits for it to settle, and then deletes the VMID (not running,
        so no stop step), in exactly that order."""
        platform, fake = _platform_with_fake(monkeypatch)
        interrupt = KeyboardInterrupt("first")
        fake.wait_errors[CLONE_UPID] = [interrupt]

        with pytest.raises(KeyboardInterrupt) as exc:
            platform.create(_request(tailscale=False), RunContext())

        assert exc.value is interrupt
        assert ("stop_task", "pve1", CLONE_UPID) in fake.calls
        # The settle wait re-polls the cancelled task before the delete
        # (the first recorded wait: the create-side wait raised instead
        # of recording).
        assert ("wait_for_task", "pve1", CLONE_UPID) in fake.calls
        assert ("delete_vm", "pve1", NEWID) in fake.calls
        assert fake.ops().index("stop_task") < fake.ops().index("wait_for_task") < fake.ops().index("delete_vm")
        assert "stop_vm" not in fake.ops()
        _assert_template_untouched(fake)

    def test_cancel_failure_still_attempts_the_delete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed task cancel must not skip the teardown: the delete
        is still attempted and the original interrupt still propagates."""
        platform, fake = _platform_with_fake(monkeypatch)
        interrupt = KeyboardInterrupt("first")
        fake.wait_errors[CLONE_UPID] = [interrupt]
        fake.stop_task_error = ProxmoxAPIError("cannot stop task")

        with pytest.raises(KeyboardInterrupt) as exc:
            platform.create(_request(tailscale=False), RunContext())

        assert exc.value is interrupt
        assert ("delete_vm", "pve1", NEWID) in fake.calls

    def test_settle_tolerates_the_cancelled_tasks_failure_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A cancelled task settles with a non-OK exit, which the settle
        wait surfaces as wait_for_task's "Task failed" raise; that is the
        expected outcome of the cancellation and must not skip the
        delete."""
        platform, fake = _platform_with_fake(monkeypatch)
        interrupt = KeyboardInterrupt("first")
        fake.wait_errors[CLONE_UPID] = [interrupt, ProxmoxAPIError("Task failed: interrupted by signal")]

        with pytest.raises(KeyboardInterrupt) as exc:
            platform.create(_request(tailscale=False), RunContext())

        assert exc.value is interrupt
        assert ("delete_vm", "pve1", NEWID) in fake.calls


class TestInterruptDuringStartTask:
    def test_cancels_the_inflight_start_then_stops_and_deletes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same pending-task path as the clone case, pinned for the
        start task: the rollback cancels the in-flight start, then runs
        the stop-then-delete (the VM reports running by then)."""
        platform, fake = _platform_with_fake(monkeypatch)
        interrupt = KeyboardInterrupt("first")
        fake.wait_errors[START_UPID] = [interrupt]

        with pytest.raises(KeyboardInterrupt) as exc:
            platform.create(_request(tailscale=False), RunContext())

        assert exc.value is interrupt
        assert ("stop_task", "pve1", START_UPID) in fake.calls
        assert ("stop_vm", "pve1", NEWID) in fake.calls
        assert ("delete_vm", "pve1", NEWID) in fake.calls
        assert fake.ops().index("stop_vm") < fake.ops().index("delete_vm")
        _assert_template_untouched(fake)


class TestEscalationFromFailureRollback:
    def test_interrupt_during_the_failure_arms_rollback_escalates(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """A plain failure enters the inner arm; a Ctrl-C during ITS
        rollback escapes rollback_partial_create by design and escalates
        to the outer interrupt arm, which restarts the cleanup under the
        full interrupt protocol. The interrupt propagates, the delete
        still happens, and no teardown step runs twice."""
        platform, fake = _platform_with_fake(monkeypatch)
        fake.configure_error = ProxmoxAPIError("config exploded")
        escalation = KeyboardInterrupt("during failure rollback")
        # The failure arm's teardown dies at its first step (the status
        # probe); the interrupt arm's re-entry then runs clean.
        fake.status_errors = [escalation]

        with pytest.raises(KeyboardInterrupt) as exc:
            platform.create(_request(tailscale=False), RunContext())

        assert exc.value is escalation
        assert ("delete_vm", "pve1", NEWID) in fake.calls
        assert fake.ops().count("delete_vm") == 1
        assert fake.ops().count("stop_vm") == 0
        _assert_template_untouched(fake)


class TestUnremovedVMIsRetained:
    """A rollback that cannot show the VMID gone hands it to core as
    ``RetainedProvisioningError``, after a warning naming it, so the row
    survives for ``vm delete`` to retry: Proxmox has no azure-style tag
    sweep to find an orphan later, and nothing else would target it."""

    def test_interrupt_path_retains_a_vm_that_survives_the_delete(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        _interrupt_the_bootstrap(monkeypatch)
        fake.delete_error = ProxmoxAPIError(f"VM {NEWID} is locked (clone)")

        with pytest.raises(RetainedProvisioningError) as exc:
            platform.create(_request(tailscale=True), RunContext())

        assert exc.value.platform_metadata == {"vmid": str(NEWID), "node": "pve1"}
        assert _retention_warned(captured_output, interrupted=True)

    def test_failure_path_retains_a_vm_that_survives_the_delete_and_keeps_the_failure(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        fake.configure_error = ProxmoxAPIError("config exploded")
        fake.delete_error = ProxmoxAPIError(f"VM {NEWID} is locked (clone)")

        with pytest.raises(RetainedProvisioningError, match="config exploded") as exc:
            platform.create(_request(tailscale=False), RunContext())

        assert exc.value.platform_metadata == {"vmid": str(NEWID), "node": "pve1"}
        warned = "\n".join(captured_output.warnings)
        assert f"VM {NEWID}" in warned
        assert "node 'pve1'" in warned

    @pytest.mark.parametrize("path", ["failure", "interrupt"])
    def test_an_api_outage_during_the_rollback_retains_the_vm(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput, path: str
    ) -> None:
        """The live case: the API stops answering while a bootstrap fails, so no call can show the VM gone."""
        platform, fake = _platform_with_fake(monkeypatch)
        if path == "failure":
            fake.guest = FakeGuest(lambda payload, seen: Answer(status=None))
        else:
            _interrupt_the_bootstrap(monkeypatch)
        outage = ProxmoxAPIError("Proxmox API failed: connection refused")
        fake.status_errors = [outage]
        fake.delete_error = outage
        fake.in_use_error = outage

        with pytest.raises(RetainedProvisioningError) as exc:
            platform.create(_request(tailscale=True), RunContext())

        assert exc.value.platform_metadata == {"vmid": str(NEWID), "node": "pve1"}
        assert _retention_warned(captured_output, interrupted=path == "interrupt")

    @pytest.mark.parametrize("path", ["failure", "interrupt"])
    def test_a_raw_transport_failure_during_the_rollback_retains_the_vm(
        self, monkeypatch: pytest.MonkeyPatch, path: str
    ) -> None:
        """The real API wraps transport failures; one that escapes unwrapped is still never a silent orphan."""
        platform, fake = _platform_with_fake(monkeypatch)
        if path == "failure":
            fake.configure_error = ProxmoxAPIError("config exploded")
        else:
            _interrupt_the_bootstrap(monkeypatch)
        fake.status_errors = [OSError("connection refused")]

        with pytest.raises(RetainedProvisioningError) as exc:
            platform.create(_request(tailscale=path == "interrupt"), RunContext())

        assert exc.value.platform_metadata == {"vmid": str(NEWID), "node": "pve1"}


class TestPlainFailure:
    def test_failure_mid_create_cleans_up_and_reraises_the_typed_error(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """A backend failure after the clone (here: the config write)
        deletes the cloned VMID and re-raises the typed error as-is
        (ProxmoxAPIError is already a ProvisioningError; the manager
        wraps foreign exceptions). The interrupt messaging never
        appears."""
        platform, fake = _platform_with_fake(monkeypatch)
        failure = ProxmoxAPIError("config exploded")
        fake.configure_error = failure

        with pytest.raises(ProxmoxAPIError) as exc:
            platform.create(_request(tailscale=False), RunContext())

        assert exc.value is failure
        # Cloned but never started: no stop step, just the delete.
        assert ("delete_vm", "pve1", NEWID) in fake.calls
        assert "stop_vm" not in fake.ops()
        assert any("Cleaning up the partial VM" in d for d in captured_output.detail)
        assert not any("Interrupted" in w for w in captured_output.warnings)
        _assert_template_untouched(fake)

    def test_failure_before_the_clone_makes_no_cleanup_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Nothing mutated, nothing to clean: a failure before the clone
        (here: the VMID allocation read) propagates without any teardown
        call."""
        platform, fake = _platform_with_fake(monkeypatch)
        fake.next_id_error = ProxmoxAPIError("cluster unreachable")

        with pytest.raises(ProxmoxAPIError, match="cluster unreachable"):
            platform.create(_request(tailscale=False), RunContext())

        for op in ("clone_vm", "stop_vm", "delete_vm", "stop_task"):
            assert op not in fake.ops()


def test_create_waits_for_the_resize_task_before_starting(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resize still running holds the VM's lock, which the start would time out on under load."""
    platform, fake = _platform_with_fake(monkeypatch)

    platform.create(_request(tailscale=False), RunContext())

    assert fake.calls.index(("wait_for_task", "pve1", RESIZE_UPID)) < fake.ops().index("start_vm")


class TestCloneThatLostItsVMID:
    """A clone refused for its VMID made nothing, so the rollback touches nothing (material 8 of #866's round two)."""

    def test_the_winner_of_a_next_id_race_is_left_untouched(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        fake.holder = "winner"
        fake.running = True
        refusal = ProxmoxAPIError(f"unable to create VM {NEWID}: config file already exists")
        fake.clone_error = refusal

        with pytest.raises(ProxmoxAPIError) as caught:
            platform.create(_request(tailscale=False), RunContext())

        assert caught.value is refusal
        assert "stop_vm" not in fake.ops()
        assert "delete_vm" not in fake.ops()
        assert fake.holder == "winner"
        warned = "\n".join(captured_output.warnings)
        assert "'winner'" in warned
        assert f"VMID {NEWID}" in warned

    def test_a_clone_that_made_nothing_needs_no_retained_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A mistyped storage: PVE refuses the clone, nothing holds the VMID, and the pool token sees a 403."""
        platform, fake = _platform_with_fake(monkeypatch)
        refusal = ProxmoxAPIError("storage 'typo' does not exist")
        fake.clone_error = refusal

        with pytest.raises(ProxmoxAPIError) as caught:
            platform.create(_request(tailscale=False), RunContext())

        assert caught.value is refusal
        assert "delete_vm" not in fake.ops()

    def test_a_winner_the_token_cannot_audit_is_retained_rather_than_touched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        fake.holder = "elsewhere"
        fake.auditable = False
        fake.clone_error = ProxmoxAPIError(f"unable to create VM {NEWID}: config file already exists")

        with pytest.raises(RetainedProvisioningError, match="cannot audit"):
            platform.create(_request(tailscale=False), RunContext())

        assert "stop_vm" not in fake.ops()
        assert "delete_vm" not in fake.ops()


class TestDeleteOp:
    """The delete op deletes only our guest, and returns only once its VMID is gone (incident row I18)."""

    @staticmethod
    def _vm_row() -> VMRow:
        return cast(
            "VMRow",
            SimpleNamespace(name="vm1", hostname="vm1", platform_metadata={"vmid": str(NEWID), "node": "pve1"}),
        )

    def test_running_vm_is_stopped_then_deleted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        fake.holder = "vm1"
        fake.running = True

        platform.delete(self._vm_row(), RunContext())

        assert ("stop_vm", "pve1", NEWID) in fake.calls
        assert ("delete_vm", "pve1", NEWID) in fake.calls
        assert fake.ops().index("stop_vm") < fake.ops().index("delete_vm")
        assert "stop_task" not in fake.ops()
        assert fake.holder is None

    def test_a_gone_vm_counts_as_deleted_through_the_pool_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The pool token sees a gone VMID as a 403 on every call but /cluster/nextid, which says nothing holds it."""
        platform, fake = _platform_with_fake(monkeypatch)

        platform.delete(self._vm_row(), RunContext())  # no raise

        assert "stop_vm" not in fake.ops()
        assert "delete_vm" not in fake.ops()
        assert ("vmid_in_use", NEWID) in fake.calls

    def test_a_gone_vm_counts_as_deleted_through_a_token_with_rights(self, monkeypatch: pytest.MonkeyPatch) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        fake.status_errors = [ProxmoxAPIError(f"Configuration file 'qemu-server/{NEWID}.conf' does not exist")]

        platform.delete(self._vm_row(), RunContext())  # no raise

        assert "delete_vm" not in fake.ops()

    def test_a_reissued_vmid_is_left_to_its_new_guest(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """Blocker 3 of #866's round two: the row's guest is gone and another create got its VMID."""
        platform, fake = _platform_with_fake(monkeypatch)
        fake.holder = "vm2"
        fake.running = True

        platform.delete(self._vm_row(), RunContext())  # the row may go

        assert "stop_vm" not in fake.ops()
        assert "delete_vm" not in fake.ops()
        assert fake.holder == "vm2"
        warned = "\n".join(captured_output.warnings)
        assert f"VMID {NEWID}" in warned
        assert "'vm2'" in warned

    def test_a_guest_the_token_cannot_audit_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        fake.holder = "outside-the-pool"
        fake.auditable = False

        with pytest.raises(StateError) as caught:
            platform.delete(self._vm_row(), RunContext())

        assert caught.value.hint
        assert "delete_vm" not in fake.ops()

    def test_a_locked_vm_that_survives_the_delete_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        fake.holder = "vm1"
        fake.delete_error = ProxmoxAPIError(f"VM {NEWID} is locked (backup)")

        with pytest.raises(ProxmoxAPIError, match="locked"):
            platform.delete(self._vm_row(), RunContext())

    @pytest.mark.parametrize("code", [401, 403, 500])
    def test_a_failed_delete_of_a_guest_still_there_raises(self, monkeypatch: pytest.MonkeyPatch, code: int) -> None:
        """Material 10 of #866's round two: only /cluster/nextid can show a failed delete's VMID free."""
        platform, fake = _platform_with_fake(monkeypatch)
        fake.holder = "vm1"
        failure = ProxmoxAPIError(f"Proxmox API DELETE /nodes/pve1/qemu/{NEWID} failed ({code})")
        failure.code = code
        fake.delete_error = failure

        with pytest.raises(ProxmoxAPIError) as caught:
            platform.delete(self._vm_row(), RunContext())

        assert caught.value is failure
        assert fake.holder == "vm1"

    def test_a_failed_delete_whose_vmid_is_free_counts_as_done(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The guest went between the identity check and the delete."""
        platform, fake = _platform_with_fake(monkeypatch)
        fake.holder = "vm1"

        def _gone_meanwhile(node: str, vmid: int) -> str:
            fake.holder = None
            raise ProxmoxAPIError(f"Configuration file 'qemu-server/{vmid}.conf' does not exist")

        monkeypatch.setattr(fake, "delete_vm", _gone_meanwhile)

        platform.delete(self._vm_row(), RunContext())  # no raise

    def test_a_delete_while_the_api_is_down_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No call answers, so nothing shows the VMID gone."""
        platform, fake = _platform_with_fake(monkeypatch)
        outage = ProxmoxAPIError("Proxmox API failed: connection refused")
        fake.holder = "vm1"
        fake.status_errors = [outage]
        fake.in_use_error = outage

        with pytest.raises(ProxmoxAPIError, match="connection refused"):
            platform.delete(self._vm_row(), RunContext())

        assert "delete_vm" not in fake.ops()

    def test_a_status_failure_on_a_held_vmid_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Something holds the VMID and its name cannot be read, so it cannot be shown to be ours or not."""
        platform, fake = _platform_with_fake(monkeypatch)
        fake.holder = "vm1"
        failure = ProxmoxAPIError("Proxmox API failed: 500 timeout")
        fake.status_errors = [failure]

        with pytest.raises(ProxmoxAPIError) as caught:
            platform.delete(self._vm_row(), RunContext())

        assert caught.value is failure
        assert "delete_vm" not in fake.ops()

    def test_a_vm_migrated_from_before_vm_sites_is_recognized_by_its_clone_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Such a row's hostname is proxmox--NAME, the guest's OS hostname, while its clone was named NAME."""
        platform, fake = _platform_with_fake(monkeypatch)
        fake.holder = "vm1"
        row = cast(
            "VMRow",
            SimpleNamespace(
                name="vm1", hostname="proxmox--vm1", platform_metadata={"vmid": str(NEWID), "node": "pve1"}
            ),
        )

        platform.delete(row, RunContext())

        assert ("delete_vm", "pve1", NEWID) in fake.calls

    @pytest.mark.parametrize("vmid", ["not-a-number", "-1", "1e3", "\u0661\u0662", -1, True, 1.5])
    def test_a_row_whose_vmid_is_not_a_number_is_refused_naming_the_row(
        self, monkeypatch: pytest.MonkeyPatch, vmid: object
    ) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        row = cast(
            "VMRow", SimpleNamespace(name="vm1", hostname="vm1", platform_metadata={"vmid": vmid, "node": "pve1"})
        )

        with pytest.raises(StateError, match="VM 'vm1'"):
            platform.delete(row, RunContext())

        assert "delete_vm" not in fake.ops()
