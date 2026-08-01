"""Azure ``delete`` post-teardown verification (#329).

The delete teardown is best-effort (a half-cleaned backend must not
block a retry), which used to let a FAILED VM delete (the reported
case: ambient credentials without delete rights) fall through to a
clean "deleted" message; the manager then dropped the DB row, orphaning
a VM nothing could target anymore. ``verify_vm_deleted`` now gates the
op: after the teardown attempt it probes the VM by name, treats
not-found as the success answer (idempotent delete), and raises a
typed error when the VM survives (``AuthorizationError`` when the
captured delete failure is an RBAC denial, ``AzureError`` otherwise)
or when its absence cannot be confirmed. Auxiliary-resource stragglers
(NIC/IP/NSG/disk) stay best-effort but now warn instead of vanishing.

Fakes come from ``tests._azure_platform_support`` (shared with the
create-interrupt and NSG tests); ``vm_exists_lookup`` picks the probe's
answer: True serves a VM back (it survived), False raises the SDK's
not-found (it is gone). No test touches Azure.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.errors import AuthorizationError
from agentworks.plugins.azure.network import AzureError
from agentworks.plugins.azure.platform import AzureVMPlatform
from tests._azure_platform_support import _RESOURCE_ID, _install_fakes

if TYPE_CHECKING:
    from tests.conftest import CapturedOutput

_CONFIG = {"subscription_id": "sub-A", "resource_group": "rg1", "region": "eastus"}


def _platform() -> AzureVMPlatform:
    return AzureVMPlatform("az-site", dict(_CONFIG))


def _vm_row(*, resource_id: str | None = _RESOURCE_ID) -> Any:
    """A stand-in for a VMRow carrying just what the delete op reads."""
    metadata = {"resource_id": resource_id} if resource_id else {}
    return SimpleNamespace(name="vm1", admin_username="agentworks", platform_metadata=metadata)


def _authorization_denied() -> Exception:
    """An ARM RBAC rejection as the SDK raises it: HttpResponseError
    carrying the documented ``AuthorizationFailed`` error code."""
    from azure.core.exceptions import HttpResponseError

    exc = HttpResponseError(message="denied")
    exc.error = SimpleNamespace(  # type: ignore[assignment]
        code="AuthorizationFailed",
        message="The client does not have authorization to perform action 'Microsoft.Compute/virtualMachines/delete'.",
        details=None,
    )
    return exc


def _authorization_denied_nested() -> Exception:
    """The same RBAC rejection with ``AuthorizationFailed`` buried in
    ``error.details[0]`` behind a generic top-level code, the nesting
    ARM sometimes uses and ``wrap_azure_error`` already walks."""
    from azure.core.exceptions import HttpResponseError

    exc = HttpResponseError(message="denied")
    exc.error = SimpleNamespace(  # type: ignore[assignment]
        code="Conflict",
        message="One of the operations failed.",
        details=[
            SimpleNamespace(
                code="AuthorizationFailed",
                message=(
                    "The client does not have authorization to perform action "
                    "'Microsoft.Compute/virtualMachines/delete'."
                ),
            )
        ],
    )
    return exc


class TestSurvivingVMRaises:
    @pytest.mark.parametrize(
        "denied",
        [_authorization_denied, _authorization_denied_nested],
        ids=["top-level-code", "nested-details-code"],
    )
    def test_rbac_denial_raises_authorization_error(
        self,
        denied: Any,
        monkeypatch: pytest.MonkeyPatch,
        captured_output: CapturedOutput,
    ) -> None:
        """The issue's repro: the VM delete is refused for lack of
        rights, the VM survives the teardown, and the op raises the
        clean typed rejection (naming the retry) instead of reporting
        success; the caller keeps the row. Both code placements (top
        level and nested under ``error.details``) must land here."""
        fakes = _install_fakes(monkeypatch)  # get() serves the VM back: it survived
        fakes.compute.virtual_machines.delete_error = denied()

        with pytest.raises(AuthorizationError) as exc:
            _platform().delete(_vm_row(), RunContext())

        assert "AuthorizationFailed" in str(exc.value)
        assert exc.value.hint is not None
        assert "Contributor" in exc.value.hint
        assert "re-run `agw vm delete`" in exc.value.hint
        # The auxiliary sweep still ran before the gate raised: the
        # stragglers stay collectable even on a failed VM delete.
        assert fakes.network.network_interfaces.deleted == [("rg1", "vm1-nic")]
        assert fakes.network.public_ip_addresses.deleted == [("rg1", "vm1-ip")]
        # The success line never printed: the verification gate sits
        # BEFORE it in delete(), so a teardown-failure warning can only
        # ever be the preamble to the typed error, never contradicted
        # by a following "deleted" claim.
        assert not any("Azure VM 'vm1' deleted" in line for line in captured_output.info)

    def test_unidentified_failure_raises_azure_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fakes = _install_fakes(monkeypatch)
        fakes.compute.virtual_machines.delete_error = RuntimeError("LRO exploded")

        with pytest.raises(AzureError, match="still exists .* LRO exploded"):
            _platform().delete(_vm_row(), RunContext())

    def test_silent_survival_raises_even_without_a_captured_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Belt-and-braces: a delete that LOOKED successful but left the
        VM behind still refuses to report success."""
        fakes = _install_fakes(monkeypatch)  # begin_delete succeeds, get still finds the VM

        with pytest.raises(AzureError, match="still exists"):
            _platform().delete(_vm_row(), RunContext())

        assert fakes.compute.virtual_machines.deleted == [("rg1", "vm1")]

    def test_unconfirmable_probe_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A probe that cannot answer is a failure, not a success:
        claiming the VM gone without positive confirmation is exactly
        how #329 orphaned one."""
        fakes = _install_fakes(monkeypatch, vm_exists_lookup=False)

        def _unreachable(rg: str, name: str, **_kw: object) -> Any:
            raise RuntimeError("ARM unreachable")

        monkeypatch.setattr(fakes.compute.virtual_machines, "get", _unreachable)

        with pytest.raises(AzureError, match="could not confirm"):
            _platform().delete(_vm_row(), RunContext())


class TestGoneVMSucceeds:
    def test_clean_delete_succeeds(self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput) -> None:
        fakes = _install_fakes(monkeypatch, vm_exists_lookup=False)

        _platform().delete(_vm_row(), RunContext())

        assert fakes.compute.virtual_machines.deleted == [("rg1", "vm1")]
        assert any("Azure VM 'vm1' deleted" in line for line in captured_output.info)

    def test_failed_delete_with_vm_actually_gone_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Idempotency kept: a delete retried against an already-gone VM
        (or one whose delete call failed while something else removed
        it) finishes the job; not-found IS the success answer."""
        fakes = _install_fakes(monkeypatch, vm_exists_lookup=False)
        fakes.compute.virtual_machines.delete_error = RuntimeError("delete raced")

        _platform().delete(_vm_row(), RunContext())  # no raise

    def test_sweep_straggler_warns_without_failing_the_delete(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """An auxiliary straggler (here the NIC) warns, names itself for
        manual cleanup, and neither fails the delete nor stops the rest
        of the sweep."""
        fakes = _install_fakes(monkeypatch, vm_exists_lookup=False)
        fakes.network.network_interfaces.delete_error = RuntimeError("nic is stuck")

        _platform().delete(_vm_row(), RunContext())  # no raise

        (warning,) = [w for w in captured_output.warnings if "vm1-nic" in w]
        assert "delete it there manually" in warning
        # The sweep continued past the straggler.
        assert fakes.network.public_ip_addresses.deleted == [("rg1", "vm1-ip")]
        assert fakes.network.network_security_groups.deleted == [("rg1", "vm1-nsg")]
        assert fakes.network.virtual_networks.deleted == [("rg1", "vm1-vnet")]


def test_no_resource_id_short_circuits(monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput) -> None:
    """Row-only delete of a never-provisioned VM: warn, touch nothing
    backend-side (no delete, no probe), and return so the caller can
    drop the row."""
    fakes = _install_fakes(monkeypatch)

    _platform().delete(_vm_row(resource_id=None), RunContext())

    assert fakes.events == []
    assert any("no Azure resource ID" in w for w in captured_output.warnings)
