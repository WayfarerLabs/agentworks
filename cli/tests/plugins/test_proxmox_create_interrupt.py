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

import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest
from agentworks.debian import DebianRelease
from agentworks.errors import ProvisioningError, StateError
from agentworks.plugins.proxmox.api import ProxmoxAPIError
from agentworks.plugins.proxmox.platform import ProxmoxPlatform
from agentworks.transports import SSHTransport

pytestmark = pytest.mark.usefixtures("verified_debian_release")

if TYPE_CHECKING:
    from agentworks.db import VMRow
    from tests.conftest import CapturedOutput

_CONFIG = {
    "api_url": "https://pve.example.com:8006",
    "node": "pve1",
    "token_id": "agw@pam!agw",
    "template_vmids": {"trixie": 9001},
}

_NEWID = 100
_TEMPLATE_VMID = 9001
_CLONE_UPID = "UPID:pve1:clone"
_START_UPID = "UPID:pve1:start"
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
    assert ("delete_vm", "pve1", _TEMPLATE_VMID) not in fake.calls
    assert ("stop_vm", "pve1", _TEMPLATE_VMID) not in fake.calls


class FakeProxmoxAPI:
    """Recording ``ProxmoxAPI`` stand-in for the platform's create and
    delete paths: happy-path returns, a shared call-order log, and
    per-method error injection. Errors are typed ``BaseException``
    because the interrupt tests inject ``KeyboardInterrupt`` mid-call;
    ``wait_errors`` and ``status_errors`` queue raises consumed
    front-first, so a test can make one call raise and still choose
    what a later call on the same method sees (the rollback's settle
    wait, the escalation re-entry, the orphan-backstop probe); a
    ``None`` entry in ``status_errors`` means "this call succeeds",
    letting a test target a later status call (e.g. the probe, the
    second status call in a teardown flow) without failing the first.
    A deleted VM is modeled: ``delete_vm`` marks it gone and
    ``vm_status`` then raises the API error a real PVE returns, which
    is what keeps the post-teardown existence probe quiet on the happy
    paths."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.running = False
        self.vm_deleted = False
        self.next_id_error: BaseException | None = None
        self.configure_error: BaseException | None = None
        self.stop_error: BaseException | None = None
        self.delete_error: BaseException | None = None
        self.stop_task_error: BaseException | None = None
        self.status_errors: list[BaseException | None] = []
        self.wait_errors: dict[str, list[BaseException]] = {}
        self.bootstrap_result: dict[str, Any] | None = {
            "exitcode": 0,
            "out-data": "##STEP## Tailscale\n##SUCCESS## tailscale-ip=100.64.0.7\n",
        }
        self.files: dict[str, str] = {}
        self.file_payloads: list[str] = []

    def ops(self) -> list[str]:
        return [c[0] for c in self.calls]

    # -- cluster / VM ops ------------------------------------------------------

    def next_id(self) -> int:
        if self.next_id_error is not None:
            raise self.next_id_error
        self.calls.append(("next_id",))
        return _NEWID

    def list_vms(self, node: str) -> list[dict[str, Any]]:
        return []

    def clone_vm(self, node: str, template_vmid: int, newid: int, name: str, **kw: Any) -> str:
        self.calls.append(("clone_vm", node, template_vmid, newid, name))
        return _CLONE_UPID

    def configure_vm(self, node: str, vmid: int, **params: Any) -> None:
        if self.configure_error is not None:
            raise self.configure_error
        self.calls.append(("configure_vm", node, vmid))

    def resize_disk(self, node: str, vmid: int, disk: str, size: str) -> None:
        self.calls.append(("resize_disk", node, vmid, disk, size))

    def start_vm(self, node: str, vmid: int) -> str:
        self.calls.append(("start_vm", node, vmid))
        self.running = True
        return _START_UPID

    def stop_vm(self, node: str, vmid: int) -> str:
        if self.stop_error is not None:
            raise self.stop_error
        self.calls.append(("stop_vm", node, vmid))
        self.running = False
        return "UPID:pve1:stop"

    def delete_vm(self, node: str, vmid: int) -> str:
        if self.delete_error is not None:
            raise self.delete_error
        self.calls.append(("delete_vm", node, vmid))
        self.vm_deleted = True
        return "UPID:pve1:delete"

    def vm_status(self, node: str, vmid: int) -> dict[str, Any]:
        if self.status_errors:
            err = self.status_errors.pop(0)
            if err is not None:
                raise err
        self.calls.append(("vm_status", node, vmid))
        if self.vm_deleted:
            raise ProxmoxAPIError(f"Configuration file 'qemu-server/{vmid}.conf' does not exist")
        return {"status": "running" if self.running else "stopped"}

    # -- tasks -----------------------------------------------------------------

    def stop_task(self, node: str, upid: str) -> None:
        if self.stop_task_error is not None:
            raise self.stop_task_error
        self.calls.append(("stop_task", node, upid))

    def wait_for_task(self, node: str, upid: str, *, timeout: int = 300, poll_interval: float = 2.0) -> None:
        queue = self.wait_errors.get(upid)
        if queue:
            raise queue.pop(0)
        self.calls.append(("wait_for_task", node, upid))

    # -- guest agent (immediate readiness: no polling sleeps) ------------------

    def guest_agent_network(self, node: str, vmid: int) -> list[dict[str, Any]]:
        return [
            {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1", "ip-address-type": "ipv4"}]},
            {"name": "eth0", "ip-addresses": [{"ip-address": "10.0.0.5", "ip-address-type": "ipv4"}]},
        ]

    def guest_agent_exec_wait(
        self, node: str, vmid: int, command: str, args: list[str] | None = None, *, timeout: int = 60
    ) -> dict[str, Any] | None:
        argv = tuple(args or ())
        self.calls.append(("guest_agent_exec_wait", node, vmid, command, argv, timeout))
        if command == "/usr/bin/install":
            self.files[str(argv[-1])] = ""
            return {"exitcode": 0, "out-data": ""}
        if command == "/bin/bash":
            return self.bootstrap_result
        if command == "/bin/sh":
            self.files.pop(str(argv[-1]), None)
            return {"exitcode": 0, "out-data": ""}
        return {"exitcode": 0, "out-data": ""}

    def guest_agent_file_write(self, node: str, vmid: int, path: str, content: str) -> None:
        self.calls.append(("guest_agent_file_write", node, vmid, path))
        self.files[path] = content
        self.file_payloads.append(content)


def _platform_with_fake(monkeypatch: pytest.MonkeyPatch) -> tuple[ProxmoxPlatform, FakeProxmoxAPI]:
    fake = FakeProxmoxAPI()
    monkeypatch.setattr(ProxmoxPlatform, "_api", lambda self, ctx: fake)
    return ProxmoxPlatform("pve-site", dict(_CONFIG)), fake


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
        ssh_private_key=None,
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

    def _raise(self: ProxmoxPlatform, node: str, vmid: int, script: str, ctx: RunContext) -> str | None:
        raise interrupt

    monkeypatch.setattr(ProxmoxPlatform, "_run_bootstrap_via_agent", _raise)
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
        assert ("stop_vm", "pve1", _NEWID) in fake.calls
        assert ("delete_vm", "pve1", _NEWID) in fake.calls
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
        assert f"VM {_NEWID}" in abandoned
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
            f"Timed out waiting for cloud-init on Proxmox VMID {_NEWID}; "
            "the template must have cloud-init and the QEMU guest agent installed and enabled"
        )
        assert ("stop_vm", "pve1", _NEWID) in fake.calls
        assert ("delete_vm", "pve1", _NEWID) in fake.calls
        assert fake.file_payloads == []
        _assert_exception_graph_is_value_free(caught.value)

    @pytest.mark.parametrize(
        ("bootstrap_result", "message"),
        [
            (None, "bootstrap timed out"),
            (
                {
                    "exitcode": 1,
                    "out-data": f"##STEP## Tailscale\n##ERROR## reflected {_SENTINEL}\n",
                    "err-data": f"stderr reflected {_SENTINEL}",
                },
                r"bootstrap failed \(exit 1\)",
            ),
            (
                {"exitcode": 0, "out-data": "##STEP## Tailscale\n##SUCCESS## joined\n"},
                r"bootstrap failed \(exit 0\)",
            ),
            (
                {
                    "exitcode": 0,
                    "out-data": f"##STEP## Tailscale\n##SUCCESS## tailscale-ip={_SENTINEL}\n",
                },
                r"bootstrap failed \(exit 0\)",
            ),
        ],
        ids=("timeout", "parsed-failure", "missing-ip", "forged-ip"),
    )
    def test_bootstrap_failure_removes_private_stage_and_rolls_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
        captured_output: CapturedOutput,
        bootstrap_result: dict[str, Any] | None,
        message: str,
    ) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        fake.bootstrap_result = bootstrap_result

        with pytest.raises(ProvisioningError, match=message) as caught:
            platform.create(_request(tailscale=True), RunContext())

        assert ("stop_vm", "pve1", _NEWID) in fake.calls
        assert ("delete_vm", "pve1", _NEWID) in fake.calls
        assert fake.ops().index("stop_vm") < fake.ops().index("delete_vm")
        assert fake.files == {}
        assert len(fake.file_payloads) == 1
        assert "TAILSCALE_AUTH_KEY='tskey-proxmox-create-" in fake.file_payloads[0]
        guest_argv = [call for call in fake.calls if call[0] == "guest_agent_exec_wait"]
        assert _SENTINEL not in repr(guest_argv)
        _assert_exception_graph_is_value_free(caught.value)
        assert _SENTINEL not in repr(captured_output.lines)
        _assert_template_untouched(fake)

    def test_bootstrap_failure_cleanup_survivor_keeps_primary_and_manual_guidance(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        fake.bootstrap_result = None
        fake.delete_error = ProxmoxAPIError("VM is locked")

        with pytest.raises(ProvisioningError, match="bootstrap timed out") as caught:
            platform.create(_request(tailscale=True), RunContext())

        _assert_exception_graph_is_value_free(caught.value)
        (warning,) = [warning for warning in captured_output.warnings if "may remain" in warning]
        assert f"VM {_NEWID}" in warning
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
        fake.wait_errors[_CLONE_UPID] = [interrupt]

        with pytest.raises(KeyboardInterrupt) as exc:
            platform.create(_request(tailscale=False), RunContext())

        assert exc.value is interrupt
        assert ("stop_task", "pve1", _CLONE_UPID) in fake.calls
        # The settle wait re-polls the cancelled task before the delete
        # (the first recorded wait: the create-side wait raised instead
        # of recording).
        assert ("wait_for_task", "pve1", _CLONE_UPID) in fake.calls
        assert ("delete_vm", "pve1", _NEWID) in fake.calls
        assert fake.ops().index("stop_task") < fake.ops().index("wait_for_task") < fake.ops().index("delete_vm")
        assert "stop_vm" not in fake.ops()
        _assert_template_untouched(fake)

    def test_cancel_failure_still_attempts_the_delete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failed task cancel must not skip the teardown: the delete
        is still attempted and the original interrupt still propagates."""
        platform, fake = _platform_with_fake(monkeypatch)
        interrupt = KeyboardInterrupt("first")
        fake.wait_errors[_CLONE_UPID] = [interrupt]
        fake.stop_task_error = ProxmoxAPIError("cannot stop task")

        with pytest.raises(KeyboardInterrupt) as exc:
            platform.create(_request(tailscale=False), RunContext())

        assert exc.value is interrupt
        assert ("delete_vm", "pve1", _NEWID) in fake.calls

    def test_settle_tolerates_the_cancelled_tasks_failure_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A cancelled task settles with a non-OK exit, which the settle
        wait surfaces as wait_for_task's "Task failed" raise; that is the
        expected outcome of the cancellation and must not skip the
        delete."""
        platform, fake = _platform_with_fake(monkeypatch)
        interrupt = KeyboardInterrupt("first")
        fake.wait_errors[_CLONE_UPID] = [interrupt, ProxmoxAPIError("Task failed: interrupted by signal")]

        with pytest.raises(KeyboardInterrupt) as exc:
            platform.create(_request(tailscale=False), RunContext())

        assert exc.value is interrupt
        assert ("delete_vm", "pve1", _NEWID) in fake.calls


class TestInterruptDuringStartTask:
    def test_cancels_the_inflight_start_then_stops_and_deletes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same pending-task path as the clone case, pinned for the
        start task: the rollback cancels the in-flight start, then runs
        the stop-then-delete (the VM reports running by then)."""
        platform, fake = _platform_with_fake(monkeypatch)
        interrupt = KeyboardInterrupt("first")
        fake.wait_errors[_START_UPID] = [interrupt]

        with pytest.raises(KeyboardInterrupt) as exc:
            platform.create(_request(tailscale=False), RunContext())

        assert exc.value is interrupt
        assert ("stop_task", "pve1", _START_UPID) in fake.calls
        assert ("stop_vm", "pve1", _NEWID) in fake.calls
        assert ("delete_vm", "pve1", _NEWID) in fake.calls
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
        assert ("delete_vm", "pve1", _NEWID) in fake.calls
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
        fake.delete_error = ProxmoxAPIError(f"VM {_NEWID} is locked (clone)")

        with pytest.raises(KeyboardInterrupt) as exc:
            platform.create(_request(tailscale=True), RunContext())

        assert exc.value is interrupt
        (remains,) = [w for w in captured_output.warnings if "may remain" in w]
        assert f"VM {_NEWID}" in remains
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
        fake.delete_error = ProxmoxAPIError(f"VM {_NEWID} is locked (clone)")

        with pytest.raises(ProxmoxAPIError) as exc:
            platform.create(_request(tailscale=False), RunContext())

        assert exc.value is failure
        (remains,) = [w for w in captured_output.warnings if "may remain" in w]
        assert f"VM {_NEWID}" in remains
        assert "node 'pve1'" in remains
        # The teardown itself completed, so this is the backstop, not
        # the rollback-incomplete containment.
        assert not any("Rollback incomplete" in w for w in captured_output.warnings)


class TestTransportFailureDuringRollback:
    """ProxmoxAPI types only HTTP failures as ProxmoxAPIError; a
    transport-level failure (URLError/OSError) during the rollback must
    be contained by the wrappers, never replacing the original
    interrupt or masking the original error."""

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
        assert f"VM {_NEWID}" in abandoned
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
        fake.delete_error = ProxmoxAPIError(f"VM {_NEWID} is locked (clone)")
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
        fake.delete_error = ProxmoxAPIError(f"VM {_NEWID} is locked (clone)")
        # First status call (teardown) succeeds; the probe hits the blip.
        fake.status_errors = [None, OSError("network blip")]

        with pytest.raises(KeyboardInterrupt) as exc:
            platform.create(_request(tailscale=True), RunContext())

        assert exc.value is interrupt
        (abandoned,) = [w for w in captured_output.warnings if "Cleanup abandoned" in w]
        assert f"VM {_NEWID}" in abandoned
        assert "node 'pve1'" in abandoned


class TestPlainFailure:
    def test_release_verification_failure_stays_inside_create_rollback(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        failure = StateError("guest release mismatch")
        monkeypatch.setattr(
            "agentworks.plugins.proxmox.platform.verify_provisioned_release",
            MagicMock(side_effect=failure),
        )

        with pytest.raises(StateError) as caught:
            platform.create(_request(tailscale=False), RunContext())

        assert caught.value is failure
        assert ("delete_vm", "pve1", _NEWID) in fake.calls
        _assert_template_untouched(fake)

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
        assert ("delete_vm", "pve1", _NEWID) in fake.calls
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
    """The transport a successful ``create`` hands back (#345): every
    guest-facing ``SSHTransport`` construction passes
    ``force_tty=sys.platform.name == "win32"`` (the Windows-zsh workaround
    documented on the class); Proxmox's provisioning transport omitted
    it, so interactive use from a Windows host misbehaved."""

    @pytest.mark.parametrize(("host_platform", "expected"), [("win32", True), ("linux", False)])
    def test_provisioning_transport_forces_tty_on_windows_hosts_only(
        self, monkeypatch: pytest.MonkeyPatch, host_platform: str, expected: bool
    ) -> None:
        platform, _fake = _platform_with_fake(monkeypatch)
        monkeypatch.setattr(sys, "platform", host_platform)

        result = platform.create(_request(tailscale=False), RunContext())

        assert result.debian_release is DebianRelease.TRIXIE
        target = result.native_transport
        assert isinstance(target, SSHTransport)
        assert target.host == "100.64.0.7"
        assert target.user == "agentworks"
        assert target.force_tty is expected


class TestDeleteOpUnchanged:
    """The delete op now composes the shared teardown; its observable
    behavior is byte-for-byte the pre-#340 sequence."""

    @staticmethod
    def _vm_row() -> VMRow:
        return cast(
            "VMRow",
            SimpleNamespace(name="vm1", platform_metadata={"vmid": str(_NEWID), "node": "pve1"}),
        )

    def test_running_vm_is_stopped_then_deleted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        platform, fake = _platform_with_fake(monkeypatch)
        fake.running = True

        platform.delete(self._vm_row(), RunContext())

        assert ("stop_vm", "pve1", _NEWID) in fake.calls
        assert ("delete_vm", "pve1", _NEWID) in fake.calls
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
