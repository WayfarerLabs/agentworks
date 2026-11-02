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
and VMID for manual removal in the Proxmox web UI. An interrupt while a
task is still in flight (likeliest: the minutes-long full clone)
cancels the task first so the target VMID unlocks before the delete.

Test shape mirrors ``test_azure_create_interrupt.py``. The fake below
is a recording ``ProxmoxAPI`` stand-in injected through the platform's
``_api`` accessor; the API-client wire tests live in
``test_proxmox_api.py``; no test here touches the network.

``TestProvisionResultTransport`` rides along as the one happy-path
``create`` test (#345): the fake is the only full create harness, so
the pin on the returned transport lives here too.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest
from agentworks.debian import DebianRelease
from agentworks.errors import ProvisioningError
from agentworks.plugins.proxmox.api import ProxmoxAPIError
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.plugins.proxmox.transport import ProxmoxExecTransport
from tests._proxmox_fakes import CLONE_UPID, NEWID, PROXMOX_CONFIG, START_UPID, TEMPLATE_VMID, FakeProxmoxAPI
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
        assert any("Ctrl-C again to abandon" in w for w in captured_output.warnings)
        # The delete succeeded, so the orphan backstop stays quiet.
        assert not any("may remain" in w for w in captured_output.warnings)
        _assert_template_untouched(fake)

    def test_second_interrupt_abandons_cleanup_loudly(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """A second Ctrl-C during the cleanup abandons it instead of
        wedging: no further API calls are made, the warning names the
        node and VMID (the operator's pointer for the Proxmox web UI),
        and the ORIGINAL interrupt still propagates."""
        platform, fake = _platform_with_fake(monkeypatch)
        interrupt = _interrupt_the_bootstrap(monkeypatch)
        fake.stop_error = KeyboardInterrupt("second")

        with pytest.raises(KeyboardInterrupt) as exc:
            platform.create(_request(tailscale=True), RunContext())

        assert exc.value is interrupt
        # Abandoned at the stop: nothing else was touched.
        assert "delete_vm" not in fake.ops()
        assert "stop_task" not in fake.ops()
        (abandoned,) = [w for w in captured_output.warnings if "Cleanup abandoned" in w]
        assert f"VM {NEWID}" in abandoned
        assert "node 'pve1'" in abandoned


class TestFailClosedBootstrap:
    def test_cloud_init_timeout_rolls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        wait_for_cloud_init = ProxmoxPlatform._wait_for_cloud_init

        def _immediate_timeout(self: ProxmoxPlatform, node: str, vmid: int, ctx: RunContext) -> None:
            wait_for_cloud_init(self, node, vmid, ctx, timeout=0)

        monkeypatch.setattr(ProxmoxPlatform, "_wait_for_cloud_init", _immediate_timeout)

        with pytest.raises(ProvisioningError) as caught:
            platform.create(_request(tailscale=True), RunContext())

        assert str(caught.value) == (
            f"Timed out waiting for cloud-init on Proxmox VMID {NEWID}; "
            "the template must have cloud-init and the QEMU guest agent installed and enabled"
        )
        assert ("stop_vm", "pve1", NEWID) in fake.calls
        assert ("delete_vm", "pve1", NEWID) in fake.calls
        assert fake.guest.scripts == []
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

    def test_bootstrap_failure_cleanup_survivor_keeps_primary_and_manual_guidance(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        fake.guest = FakeGuest(lambda payload, seen: Answer(status=None))
        fake.delete_error = ProxmoxAPIError("VM is locked")

        with pytest.raises(ProvisioningError) as caught:
            platform.create(_request(tailscale=True), RunContext())

        _assert_exception_graph_is_value_free(caught.value)
        (warning,) = [warning for warning in captured_output.warnings if "may remain" in warning]
        assert f"VM {NEWID}" in warning
        assert "node 'pve1'" in warning
        assert "delete it there manually" in warning
        assert _SENTINEL not in warning


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
        assert any("Ctrl-C again to abandon" in w for w in captured_output.warnings)
        _assert_template_untouched(fake)


class TestOrphanBackstopWarning:
    """The post-teardown existence check: stop_and_delete_vm suppresses
    a failed delete silently (delete()'s pinned contract), so both
    rollback wrappers probe the VMID afterwards and warn when it still
    exists (the likeliest cause: still locked by a clone that did not
    settle within the bound). Proxmox has no azure-style tag sweep to
    catch the orphan later. The happy-path tests pin the quiet side (VM
    gone, no warning)."""

    def test_interrupt_path_warns_when_the_vm_survives_the_delete(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        interrupt = _interrupt_the_bootstrap(monkeypatch)
        fake.delete_error = ProxmoxAPIError(f"VM {NEWID} is locked (clone)")

        with pytest.raises(KeyboardInterrupt) as exc:
            platform.create(_request(tailscale=True), RunContext())

        assert exc.value is interrupt
        (remains,) = [w for w in captured_output.warnings if "may remain" in w]
        assert f"VM {NEWID}" in remains
        assert "node 'pve1'" in remains
        # The teardown itself completed (the failed delete was
        # suppressed inside it), so this is the backstop, not the
        # abandon path.
        assert not any("Cleanup abandoned" in w for w in captured_output.warnings)

    def test_failure_path_warns_when_the_vm_survives_the_delete(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        failure = ProxmoxAPIError("config exploded")
        fake.configure_error = failure
        fake.delete_error = ProxmoxAPIError(f"VM {NEWID} is locked (clone)")

        with pytest.raises(ProxmoxAPIError) as exc:
            platform.create(_request(tailscale=False), RunContext())

        assert exc.value is failure
        (remains,) = [w for w in captured_output.warnings if "may remain" in w]
        assert f"VM {NEWID}" in remains
        assert "node 'pve1'" in remains
        # The teardown itself completed, so this is the backstop, not
        # the rollback-incomplete containment.
        assert not any("Rollback incomplete" in w for w in captured_output.warnings)


class TestTransportFailureDuringRollback:
    """Even an unexpected raw transport failure from a fake API must be
    contained by rollback, never replacing the original interrupt or
    masking the original error. The real API normalizes these failures."""

    def test_interrupt_path_absorbs_it_with_the_abandon_warning(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        interrupt = _interrupt_the_bootstrap(monkeypatch)
        fake.status_errors = [OSError("connection refused")]

        with pytest.raises(KeyboardInterrupt) as exc:
            platform.create(_request(tailscale=True), RunContext())

        assert exc.value is interrupt
        (abandoned,) = [w for w in captured_output.warnings if "Cleanup abandoned" in w]
        assert f"VM {NEWID}" in abandoned
        assert "node 'pve1'" in abandoned

    def test_failure_path_warns_and_reraises_the_original_error(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        failure = ProxmoxAPIError("config exploded")
        fake.configure_error = failure
        fake.status_errors = [OSError("connection refused")]

        with pytest.raises(ProxmoxAPIError) as exc:
            platform.create(_request(tailscale=False), RunContext())

        assert exc.value is failure
        assert any("Rollback incomplete" in w and "node 'pve1'" in w for w in captured_output.warnings)

    # The two tests above inject at the FIRST status call, inside
    # stop_and_delete_vm, short-circuiting before the orphan-backstop
    # probe. The two below target the PROBE itself (the second status
    # call): the delete has already failed silently as a suppressed
    # ProxmoxAPIError, so if the probe's transport failure were
    # swallowed in the helper, this scenario would produce ZERO warning
    # and the silent orphan would just have moved one layer up.

    def test_failure_path_probe_blip_still_warns_via_containment(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        failure = ProxmoxAPIError("config exploded")
        fake.configure_error = failure
        fake.delete_error = ProxmoxAPIError(f"VM {NEWID} is locked (clone)")
        # First status call (teardown) succeeds; the probe hits the blip.
        fake.status_errors = [None, OSError("network blip")]

        with pytest.raises(ProxmoxAPIError) as exc:
            platform.create(_request(tailscale=False), RunContext())

        assert exc.value is failure
        assert any("Rollback incomplete" in w and "node 'pve1'" in w for w in captured_output.warnings)

    def test_interrupt_path_probe_blip_still_warns_via_containment(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        interrupt = _interrupt_the_bootstrap(monkeypatch)
        fake.delete_error = ProxmoxAPIError(f"VM {NEWID} is locked (clone)")
        # First status call (teardown) succeeds; the probe hits the blip.
        fake.status_errors = [None, OSError("network blip")]

        with pytest.raises(KeyboardInterrupt) as exc:
            platform.create(_request(tailscale=True), RunContext())

        assert exc.value is interrupt
        (abandoned,) = [w for w in captured_output.warnings if "Cleanup abandoned" in w]
        assert f"VM {NEWID}" in abandoned
        assert "node 'pve1'" in abandoned


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
        assert not any("Cleanup abandoned" in w for w in captured_output.warnings)
        # The delete succeeded, so the orphan backstop stays quiet.
        assert not any("may remain" in w for w in captured_output.warnings)
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


class TestProvisionResultTransport:
    """The transport a successful ``create`` hands back (#345): a
    QGA execution channel bound to the created VM."""

    def test_provisioning_transport_targets_the_guest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        platform, _fake = _platform_with_fake(monkeypatch)

        result = platform.create(_request(tailscale=False), RunContext())

        target = result.native_transport
        assert isinstance(target, ProxmoxExecTransport)
        assert target.node == "pve1"
        assert target.vmid == NEWID
        assert target.admin_username == "agentworks"


class TestDeleteOpUnchanged:
    """The delete op now composes the shared teardown; its observable
    behavior is byte-for-byte the pre-#340 sequence."""

    @staticmethod
    def _vm_row() -> VMRow:
        return cast(
            "VMRow",
            SimpleNamespace(name="vm1", platform_metadata={"vmid": str(NEWID), "node": "pve1"}),
        )

    def test_running_vm_is_stopped_then_deleted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        fake.running = True

        platform.delete(self._vm_row(), RunContext())

        assert ("stop_vm", "pve1", NEWID) in fake.calls
        assert ("delete_vm", "pve1", NEWID) in fake.calls
        assert fake.ops().index("stop_vm") < fake.ops().index("delete_vm")
        assert "stop_task" not in fake.ops()

    def test_already_gone_vm_deletes_best_effort_without_raising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The idempotency contract: a status read that fails (VM gone)
        skips the stop, and the delete is still attempted best-effort."""
        platform, fake = _platform_with_fake(monkeypatch)
        fake.status_errors = [ProxmoxAPIError("does not exist")]
        fake.delete_error = ProxmoxAPIError("does not exist")

        platform.delete(self._vm_row(), RunContext())  # no raise

        assert "stop_vm" not in fake.ops()
