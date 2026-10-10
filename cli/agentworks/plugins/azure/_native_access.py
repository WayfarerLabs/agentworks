"""Private exact-incarnation Azure reads with concrete public-close custody."""

from __future__ import annotations

from collections.abc import Mapping
from threading import TIMEOUT_MAX, Lock
from typing import TYPE_CHECKING, Literal, cast

from agentworks.capabilities.vm_platform.base import ProviderLocator, provider_locator_remaining
from agentworks.db import VMStatus
from agentworks.errors import AgentworksError, NotFoundError, StateError, ValidationError
from agentworks.plugins.azure._identity import canonical_vm_id, parse_resource_id
from agentworks.plugins.azure._owned_auth import _OwnedAzureCredential
from agentworks.plugins.azure.network import AzureError, _native_read_options, read_native_public_ipv4

if TYPE_CHECKING:
    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.compute.models import VirtualMachine
    from azure.mgmt.network import NetworkManagementClient

    from agentworks.capabilities.base import RunContext
    from agentworks.db import VMRow
    from agentworks.execution.carrier import Deadline
    from agentworks.plugins.azure.platform import AzureVMPlatform


class AzureOwnedReadAccess:
    """Caller-retained reads, without activation, routes or native preparation.

    Construction is passive. Retain this object before its first observation.
    Normal public close retires a returned original locally; it does not prove
    all SDK sockets/processes drained or recover constructor-internal handles.
    ARM authentication can resend a GET despite disabled service retries.
    """

    def __init__(self, vm: VMRow, platform: AzureVMPlatform, ctx: RunContext) -> None:
        self._vm_name = vm.name
        metadata = vm.platform_metadata
        try:
            if not isinstance(metadata, Mapping):
                raise ValidationError("Azure VM metadata is invalid")
            resource_id = metadata.get("resource_id")
            self._subscription, self._group, self._name = parse_resource_id(resource_id)
            self._vm_id = canonical_vm_id(metadata.get("vm_id"))
        except ValidationError as error:
            raise StateError(
                "Azure owned reads require a recorded resource path and unique VM ID",
                entity_kind="vm",
                entity_name=self._vm_name,
                hint="Restore recorded identity evidence; current observations cannot supply historical identity.",
            ) from error
        assert isinstance(resource_id, str)
        self._resource_id = resource_id
        self._auth = _OwnedAzureCredential(platform.config.auth, ctx, platform.site_name, vm.name)
        self._read_client: ComputeManagementClient | NetworkManagementClient | None = None
        self._closed = False
        self._lock = Lock()

    @property
    def cleanup_incomplete(self) -> bool:
        return (
            self._read_client is not None
            or self._auth.cleanup_incomplete
            or (self._closed and self._auth.has_credential)
        )

    def observe_power(self, deadline: Deadline) -> VMStatus:
        return cast("VMStatus", self._observe(deadline, "power"))

    def observe_locator(self, deadline: Deadline) -> ProviderLocator:
        return cast("ProviderLocator", self._observe(deadline, "locator"))

    def observe_public_endpoint(self, deadline: Deadline) -> object:
        return self._observe(deadline, "endpoint")

    def _acquire(self, deadline: Deadline) -> None:
        remaining = provider_locator_remaining(deadline, vm_name=self._vm_name)
        if not self._lock.acquire(timeout=min(remaining, TIMEOUT_MAX)):
            raise StateError(
                "Azure read access transition exceeded its deadline",
                entity_kind="vm",
                entity_name=self._vm_name,
            )

    def _refusal(self) -> StateError:
        return StateError("Azure read access cannot admit an observation", entity_kind="vm", entity_name=self._vm_name)

    def _observe(self, deadline: Deadline, kind: Literal["power", "locator", "endpoint"]) -> object:
        self._acquire(deadline)
        control: BaseException | None = None
        admitted = False
        completed = False
        read_close_attempted = False
        try:
            try:
                provider_locator_remaining(deadline, vm_name=self._vm_name)
                if self._closed or self._auth.failed or self.cleanup_incomplete:
                    raise self._refusal()
                admitted = True
                credential = self._auth.credential(deadline)
                observed = self._read_vm(credential, deadline, expand="instanceView" if kind == "power" else None)
                if kind == "power":
                    result: object = self._power(observed)
                elif kind == "locator":
                    result = ProviderLocator(f"azure-vm:v2:{self._vm_id}:{self._resource_id}")
                else:
                    # Never continue to linked reads after uncertain compute closure.
                    read_close_attempted = True
                    self._close_read_client()
                    provider_locator_remaining(deadline, vm_name=self._vm_name)
                    if self._read_client is not None:
                        raise self._refusal()
                    # Positive compute retirement admits the network phase's close.
                    read_close_attempted = False
                    result = self._read_endpoint(credential, observed, deadline)
                provider_locator_remaining(deadline, vm_name=self._vm_name)
                completed = True
                return result
            except BaseException as error:
                if not isinstance(error, Exception):
                    control = error
                raise
            finally:
                if admitted and not read_close_attempted:
                    self._close_read_client()
                if completed:
                    provider_locator_remaining(deadline, vm_name=self._vm_name)
        except BaseException as error:
            if control is not None:
                raise control from None
            if not isinstance(error, Exception):
                control = error
            raise
        finally:
            try:
                self._lock.release()
            except BaseException:
                if control is not None:
                    raise control from None
                raise

    def _read_vm(self, credential: object, deadline: Deadline, *, expand: str | None) -> VirtualMachine:
        from azure.core.exceptions import ResourceNotFoundError

        from agentworks.plugins.azure._passive_clients import compute_read_client

        provider_locator_remaining(deadline, vm_name=self._vm_name)
        missing = False
        failure_kind: str | None = None
        try:
            client = compute_read_client(credential, self._subscription)
            self._read_client = client
            remaining = provider_locator_remaining(deadline, vm_name=self._vm_name)
            observed = client.virtual_machines.get(
                self._group, self._name, expand=expand, **_native_read_options(remaining)
            )
        except AgentworksError:
            raise
        except ResourceNotFoundError:
            missing = True
        except Exception as error:
            failure_kind = type(error).__name__
        if missing:
            raise NotFoundError(
                f"Azure VM '{self._name}' no longer exists", entity_kind="vm", entity_name=self._vm_name
            )
        if failure_kind is not None:
            raise self._read_failure(failure_kind)
        try:
            observed_vm_id = canonical_vm_id(getattr(observed, "vm_id", None))
        except ValidationError as error:
            raise self._identity_mismatch() from error
        if type(getattr(observed, "id", None)) is not str or observed.id != self._resource_id:
            raise self._identity_mismatch()
        if observed_vm_id != self._vm_id:
            raise self._identity_mismatch()
        provider_locator_remaining(deadline, vm_name=self._vm_name)
        return observed

    def _read_endpoint(self, credential: object, observed: object, deadline: Deadline) -> object:
        from agentworks.plugins.azure._passive_clients import network_read_client

        failure_kind: str | None = None
        domain_error: AgentworksError | None = None
        try:
            client = network_read_client(credential, self._subscription)
            self._read_client = client
            provider_locator_remaining(deadline, vm_name=self._vm_name)
            result = read_native_public_ipv4(
                client,
                observed,
                subscription_id=self._subscription,
                vm_name=self._vm_name,
                deadline=deadline,
            )
        except AzureError as error:
            # The legacy linked reader translates SDK failures. Keep no SDK
            # message/body/cause in this owned read's public diagnostic.
            if isinstance(error.__cause__, AgentworksError) and not isinstance(error.__cause__, AzureError):
                domain_error = error.__cause__
            else:
                failure_kind = type(error.__cause__).__name__ if error.__cause__ is not None else type(error).__name__
        except AgentworksError:
            raise
        except Exception as error:
            failure_kind = type(error).__name__
        # Detached raises happen after handlers; core-authored failures retain
        # identity, but an SDK-derived wrapper cannot enter persisted chains.
        if domain_error is not None:
            raise domain_error
        if failure_kind is not None:
            raise self._read_failure(failure_kind)
        return result

    def _read_failure(self, failure_kind: str) -> AzureError:
        return AzureError(
            "Azure owned VM read failed",
            detail=failure_kind,
            entity_kind="vm",
            entity_name=self._vm_name,
            hint="Check the selected Azure identity's permissions and service connectivity.",
        )

    def _identity_mismatch(self) -> StateError:
        return StateError(
            "Azure VM no longer matches its recorded path and unique identity",
            entity_kind="vm",
            entity_name=self._vm_name,
            hint="Refuse replacement admission; do not overwrite recorded identity with a current observation.",
        )

    @staticmethod
    def _power(observed: object) -> VMStatus:
        statuses = getattr(getattr(observed, "instance_view", None), "statuses", None)
        if not isinstance(statuses, list):
            return VMStatus.UNKNOWN
        power_codes = []
        for status in statuses:
            code = getattr(status, "code", None)
            if type(code) is not str:
                return VMStatus.UNKNOWN
            if code.startswith("PowerState/"):
                power_codes.append(code)
        if len(power_codes) != 1:
            return VMStatus.UNKNOWN
        return {
            "PowerState/running": VMStatus.RUNNING,
            "PowerState/stopped": VMStatus.STOPPED,
            "PowerState/deallocated": VMStatus.DEALLOCATED,
        }.get(power_codes[0], VMStatus.UNKNOWN)

    def _close_read_client(self) -> None:
        client = self._read_client
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            else:
                self._read_client = None

    def close(self, deadline: Deadline) -> bool:
        """Stop reads; retry concrete originals and close their credential last."""
        self._acquire(deadline)
        control: BaseException | None = None
        try:
            self._closed = True
            provider_locator_remaining(deadline, vm_name=self._vm_name)
            self._close_read_client()
            provider_locator_remaining(deadline, vm_name=self._vm_name)
            if self._read_client is None:
                self._auth.close()
            provider_locator_remaining(deadline, vm_name=self._vm_name)
            return not self.cleanup_incomplete
        except BaseException as error:
            if not isinstance(error, Exception):
                control = error
            raise
        finally:
            try:
                self._lock.release()
            except BaseException:
                if control is not None:
                    raise control from None
                raise
