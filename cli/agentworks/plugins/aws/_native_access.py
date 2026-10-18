"""Private retained EC2 observations and direct-client public-close custody."""

from __future__ import annotations

import time
from dataclasses import replace
from threading import TIMEOUT_MAX, Lock
from typing import TYPE_CHECKING, Any

from agentworks.capabilities.vm_platform.base import (
    ProviderLocator,
    provider_locator_remaining,
)
from agentworks.db import LifecycleObligationState, VMStatus
from agentworks.errors import AgentworksError, NotFoundError, StateError
from agentworks.execution.binding import _IndependentJobAvailability
from agentworks.operations import LifecycleObligation
from agentworks.plugins.aws._activation import EC2Activation, encode_activation_payload
from agentworks.plugins.aws._owned_auth import _OwnedRoleSession
from agentworks.plugins.aws.auth import _build_ambient_session
from agentworks.plugins.aws.config import AwsAmbientAuth
from agentworks.plugins.aws.network import error_code, wrap_ec2_error
from agentworks.vms._ssh_native_binding import require_ssh_settings, ssh_native_binding
from agentworks.vms._ssh_native_exposure import OwnedSSHExposure
from agentworks.vms.target_preparation import VMTargetPreparationControlFact, prepare_managed_vm_target_from_platform

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentworks.capabilities.base import RunContext
    from agentworks.db import VMRow
    from agentworks.execution.binding import NativeExecutionBinding
    from agentworks.execution.carrier import Deadline
    from agentworks.operations import OperationOwner
    from agentworks.plugins.aws._native_route import EC2NativeRoute
    from agentworks.plugins.aws.platform import EC2Platform
    from agentworks.vms.target_preparation import VMTargetPreparation


class EC2OwnedAccess:
    """Caller-retained exact reads and optional owned native preparation.

    Construction is passive. Retain this object before observing it. The slot
    covers returned EC2 and configured-role STS originals, not arbitrary ambient
    credential clients or SDK constructor handoff. Normal public close is a
    client lifecycle fact, not synchronous closure of all sockets.
    """

    def __init__(
        self,
        vm: VMRow,
        platform: EC2Platform,
        ctx: RunContext,
        *,
        owner: OperationOwner | None = None,
    ) -> None:
        self._vm = vm
        self._platform = platform
        self._ctx = ctx
        self._session: Any = None
        self._read_client: Any = None
        self._role_auth: _OwnedRoleSession | None = None
        self._closed = False
        self._lock = Lock()
        self._owner = owner
        self.binding: NativeExecutionBinding | None = None
        self.preparation: VMTargetPreparation | None = None
        self._activation: EC2Activation | None = None
        self._route: EC2NativeRoute | None = None
        self._exposure: OwnedSSHExposure | None = None
        self._locator: ProviderLocator | None = None
        self._endpoint: object = None
        self._used = False
        self._running: tuple[ProviderLocator, dict[str, Any]] | None = None
        self._effects_settled = False

    @property
    def route_check(self) -> Callable[[Deadline], None] | None:
        return self._check_route if self.binding is not None else None

    def prepare(self, power: VMStatus, deadline: Deadline) -> None:
        """Retain each selected effect before the next preparation boundary."""
        from agentworks.capabilities.vm_platform.ssh_exposure import config_allow_cidrs
        from agentworks.plugins.aws._native_route import EC2NativeRoute

        if self._used or self._owner is None or power not in (VMStatus.RUNNING, VMStatus.STOPPED):
            raise StateError("EC2 native preparation cannot be admitted")
        self._used = True
        settings = require_ssh_settings(self._vm, self._ctx, self._ctx.config)
        locator, instance = self._read_exact_instance(deadline)
        self._locator = locator
        if power is VMStatus.STOPPED:
            state = instance.get("State")
            if self.cleanup_incomplete or type(state) is not dict or state.get("Name") != "stopped":
                raise StateError("EC2 stopped selection changed before startup")
            instance_id, region, account_id = self._platform._locator_metadata(self._vm)
            self._activation = EC2Activation(
                self._owner,
                self._vm.name,
                self._session,
                account_id,
                region,
                instance_id,
                locator,
                role_auth=self._role_auth,
            )
            self._active_transition(self._activation.start, deadline)
            while not self._complete_start(deadline):
                # Only supported transitional states reach this foreground pause.
                remaining = provider_locator_remaining(deadline, vm_name=self._vm.name)
                time.sleep(min(0.25, remaining))
                provider_locator_remaining(deadline, vm_name=self._vm.name)
        else:
            self._require_running(instance)
        # A separate fresh endpoint read follows startup, even when it was already running.
        locator, instance = self._read_exact_instance(deadline)
        if locator != self._locator:
            raise StateError("EC2 selected locator changed")
        self._require_running(instance)
        self._endpoint = instance.get("PublicIpAddress")
        self.binding = ssh_native_binding(
            self._vm, self._endpoint, settings, independent_availability=_IndependentJobAvailability.NO_IDLE_STOP
        )
        provider_locator_remaining(deadline, vm_name=self._vm.name)
        self._exposure = OwnedSSHExposure(self._vm.name)
        prefixes = self._exposure.resolve(config_allow_cidrs(self._ctx.config), deadline)
        provider_locator_remaining(deadline, vm_name=self._vm.name)
        instance_id, region, account_id = self._platform._locator_metadata(self._vm)
        self._route = EC2NativeRoute(
            self._owner,
            self._vm.name,
            self._session,
            account_id,
            region,
            self._platform._security_group_id(self._vm),
            self._platform._backend_name(self._vm),
            instance,
            prefixes,
            role_auth=self._role_auth,
        )
        self._active_transition(self._route.open, deadline)
        try:
            self.preparation = prepare_managed_vm_target_from_platform(
                self._vm,
                self._platform,
                locator,
                self.binding,
                deadline=deadline,
                owner=self._owner,
                observe_locator=self.observe_locator,
            )
        except BaseException as control:
            if isinstance(control.__cause__, VMTargetPreparationControlFact):
                self.preparation = control.__cause__.preparation
            raise

    def _active_transition(self, action: Any, deadline: Deadline) -> Any:
        """Serialize the same session's explicitly admitted credential interval."""
        from botocore.exceptions import BotoCoreError, ClientError

        self._acquire(deadline)
        control: BaseException | None = None
        try:
            if self._closed or self.cleanup_incomplete:
                raise StateError("EC2 active transition retains unsettled clients")
            if self._role_auth is not None:
                self._role_auth.begin(deadline)
            try:
                return action(deadline)
            except (BotoCoreError, ClientError) as error:
                # Internal bookkeeping errors and deadline/control categories stay intact.
                raise wrap_ec2_error(error) from error
        except BaseException as error:
            if not isinstance(error, Exception):
                control = error
            raise
        finally:
            try:
                if self._role_auth is not None:
                    try:
                        self._role_auth.close_client()
                    finally:
                        self._role_auth.end()
            except BaseException:
                if control is not None:
                    raise control from None
                raise
            finally:
                self._lock.release()

    def _complete_start(self, deadline: Deadline) -> bool:
        activation = self._activation
        if activation is None or activation.closed_without_start_attempt:
            return True
        assert self._owner is not None
        if self._running is None:
            activation.reconcile(deadline)
            if activation.obligation is not None and activation.obligation.state is LifecycleObligationState.RESOLVED:
                return True
            if activation.payload.request_id is None:
                return False
            locator, instance = self._read_exact_instance(deadline)
            state = instance.get("State")
            name = state.get("Name") if type(state) is dict else None
            if locator != self._locator or type(name) is not str or name not in ("running", "pending", "stopped"):
                raise StateError("EC2 startup observation is unknown or invalid")
            if name != "running":
                return False
            # Keep the timely exact evidence before a possibly lost resolution reply.
            self._running = locator, instance
        obligation = activation.obligation
        if obligation is None or self._running is None:
            raise StateError("EC2 start completion has no retained evidence")
        expected = obligation._persisted_obligation
        row = self._owner.inspect_lifecycle_obligation(activation.obligation_id)
        if row is None or replace(row, state=expected.state, updated_at=expected.updated_at) != expected:
            raise StateError("EC2 start completion custody conflicts")
        if row.payload != encode_activation_payload(activation.payload) or row.payload_revision != 1:
            raise StateError("EC2 start completion acknowledgment conflicts")
        if row.state is LifecycleObligationState.RESOLVED:
            return True
        if row.state is not LifecycleObligationState.POSSIBLE_EFFECT:
            raise StateError("EC2 start completion state conflicts")
        provider_locator_remaining(deadline, vm_name=self._vm.name)
        LifecycleObligation(self._owner, row).resolve()
        return True

    def _check_route(self, deadline: Deadline) -> None:
        from agentworks.plugins.aws.network import require_owned_instance

        locator, instance = self._read_exact_instance(deadline)
        self._require_running(instance)
        if locator != self._locator or instance.get("PublicIpAddress") != self._endpoint:
            raise StateError("EC2 native route endpoint or locator changed")
        if self._route is None:
            raise StateError("EC2 native route is unavailable")
        require_owned_instance(
            instance, self._platform._backend_name(self._vm), self._platform._security_group_id(self._vm)
        )
        self._active_transition(self._route.check, deadline)
        provider_locator_remaining(deadline, vm_name=self._vm.name)
        if self.cleanup_incomplete:
            raise StateError("EC2 native route retains unsettled clients")

    def settle(self, deadline: Deadline) -> bool:
        """Settle retained effects without stopping the VM or closing its owner."""
        provider_locator_remaining(deadline, vm_name=self._vm.name)
        settled = True
        control: BaseException | None = None
        self._acquire(deadline)
        try:
            closers: list[Callable[[], object]] = [self._close_clients]
            activation, route = self._activation, self._route
            if activation is not None:
                closers.append(lambda: activation.close(deadline))
            if route is not None:
                closers.append(lambda: route.close(deadline))
            exposure = self._exposure
            if exposure is not None:
                closers.append(lambda: exposure.close(deadline))
            for closer in closers:
                try:
                    closer()
                except BaseException as error:
                    settled = False
                    if not isinstance(error, Exception) and control is None:
                        control = error
        finally:
            self._lock.release()
        if control is not None:
            raise control
        if self.cleanup_incomplete:
            return False
        if not self._effects_settled:
            route_settled = self._route is None or self._active_transition(self._route.settle, deadline)
            # Cleanup only observes the existing start under its own finite budget.
            start_settled = self._complete_start(deadline)
            self._effects_settled = route_settled and start_settled
        if self._effects_settled:
            return self.close(deadline) and settled
        return False

    def _require_running(self, instance: dict[str, Any]) -> None:
        """Refuse new route/guest admission after a stale state or uncertain read close."""
        state = instance.get("State")
        if self.cleanup_incomplete or type(state) is not dict or state.get("Name") != "running":
            raise StateError("EC2 native preparation requires fresh running state and retired read clients")

    @property
    def cleanup_incomplete(self) -> bool:
        """Whether a retained original lacks a normal public-close return."""
        return (
            self._read_client is not None
            or (self._role_auth is not None and self._role_auth.cleanup_incomplete)
            or (self._activation is not None and self._activation.cleanup_incomplete)
            or (self._route is not None and self._route.cleanup_incomplete)
            or (self._exposure is not None and self._exposure.cleanup_incomplete)
        )

    def observe_power(self, deadline: Deadline) -> VMStatus:
        if self._owner is not None:
            require_ssh_settings(self._vm, self._ctx, self._ctx.config)
        _, instance = self._read_exact_instance(deadline)
        state = instance.get("State")
        name = state.get("Name") if isinstance(state, dict) else None
        if type(name) is not str:
            return VMStatus.UNKNOWN
        return {"running": VMStatus.RUNNING, "stopped": VMStatus.STOPPED}.get(name, VMStatus.UNKNOWN)

    def observe_locator(self, deadline: Deadline) -> ProviderLocator:
        """Bound exact-identity callback for the retained caller."""
        locator, instance = self._read_exact_instance(deadline)
        if self._owner is not None:
            self._require_running(instance)
        return locator

    def observe_public_endpoint(self, deadline: Deadline) -> object:
        """Read the selected instance's endpoint value for later binding validation."""
        _, instance = self._read_exact_instance(deadline)
        return instance.get("PublicIpAddress")

    def _acquire(self, deadline: Deadline) -> None:
        remaining = provider_locator_remaining(deadline, vm_name=self._vm.name)
        if not self._lock.acquire(timeout=min(remaining, TIMEOUT_MAX)):
            raise StateError(
                "EC2 access transition exceeded its deadline",
                entity_kind="vm",
                entity_name=self._vm.name,
            )

    def _read_exact_instance(self, deadline: Deadline) -> tuple[ProviderLocator, dict[str, Any]]:
        self._acquire(deadline)
        control: BaseException | None = None
        admitted = False
        completed = False
        try:
            provider_locator_remaining(deadline, vm_name=self._vm.name)
            if self._closed or self.cleanup_incomplete:
                raise StateError(
                    "EC2 access cannot admit an observation",
                    entity_kind="vm",
                    entity_name=self._vm.name,
                )
            instance_id, region, account_id = self._platform._locator_metadata(self._vm)
            admitted = True
            auth = self._platform.config.auth
            if not isinstance(auth, AwsAmbientAuth):
                if self._role_auth is None:
                    self._role_auth = _OwnedRoleSession(self._vm.name)
                self._role_auth.begin(deadline)
            if self._session is None:
                session_region = self._platform.config.region
                if isinstance(auth, AwsAmbientAuth):
                    self._session = _build_ambient_session(session_region)
                else:
                    assert self._role_auth is not None
                    self._session = self._role_auth.build_session(
                        auth,
                        self._ctx.secret(auth.access_key_secret),
                        self._platform.site_name,
                        session_region,
                    )
            from botocore.config import Config

            remaining = provider_locator_remaining(deadline, vm_name=self._vm.name)
            try:
                self._read_client = self._session.client(
                    "ec2",
                    region_name=region,
                    config=Config(
                        connect_timeout=remaining,
                        read_timeout=remaining,
                        retries={"total_max_attempts": 1, "mode": "standard"},
                    ),
                )
                if self._role_auth is not None:
                    self._read_client.meta.events.register(
                        "before-send.ec2.DescribeInstances", self._role_auth.guard_ec2_send
                    )
            except AgentworksError:
                raise
            except Exception as exc:
                raise wrap_ec2_error(exc) from exc
            client = self._read_client
            provider_locator_remaining(deadline, vm_name=self._vm.name)
            try:
                result = client.describe_instances(InstanceIds=[instance_id])
            except AgentworksError:
                raise
            except Exception as exc:
                if error_code(exc) == "InvalidInstanceID.NotFound":
                    raise NotFoundError(
                        f"EC2 instance '{instance_id}' no longer exists",
                        entity_kind="vm",
                        entity_name=self._vm.name,
                    ) from exc
                raise wrap_ec2_error(exc) from exc
            owner_id = self._platform._locator_owner_id(result, instance_id, self._vm)
            if owner_id != account_id:
                raise StateError(
                    f"EC2 instance '{instance_id}' belongs to a different AWS account",
                    entity_kind="vm",
                    entity_name=self._vm.name,
                    hint="restore the original persisted AWS account identity before retrying",
                )
            instance: dict[str, Any] = result["Reservations"][0]["Instances"][0]
            locator = ProviderLocator(f"aws-ec2:{account_id}:{region}:{instance_id}")
            provider_locator_remaining(deadline, vm_name=self._vm.name)
            completed = True
            return locator, instance
        except BaseException as error:
            # Preserve a dispatch control even if cleanup is interrupted at a
            # Python boundary, including after the public close returned.
            if control is not None:
                raise control from None
            if not isinstance(error, Exception):
                control = error
            raise
        finally:
            try:
                try:
                    if admitted:
                        self._close_clients()
                    if completed:
                        provider_locator_remaining(deadline, vm_name=self._vm.name)
                except BaseException as error:
                    if control is not None:
                        raise control from None
                    if not isinstance(error, Exception):
                        control = error
                    raise
            finally:
                try:
                    if self._role_auth is not None:
                        self._role_auth.end()
                except BaseException:
                    if control is not None:
                        raise control from None
                    raise
                finally:
                    self._lock.release()

    def _close_clients(self) -> None:
        control: BaseException | None = None
        try:
            try:
                self._close_read_client()
            except BaseException as error:
                control = error
                raise
            finally:
                if self._role_auth is not None:
                    self._role_auth.close_client()
        except BaseException:
            if control is not None:
                raise control from None
            raise

    def _close_read_client(self) -> None:
        if self._read_client is not None:
            try:
                self._read_client.close()
            except Exception:
                pass
            else:
                self._read_client = None

    def close(self, deadline: Deadline) -> bool:
        """Stop admission and retry only retained EC2 and configured STS closes."""
        self._acquire(deadline)
        try:
            self._closed = True
            if self._role_auth is not None:
                self._role_auth.end()
            provider_locator_remaining(deadline, vm_name=self._vm.name)
            self._close_clients()
            provider_locator_remaining(deadline, vm_name=self._vm.name)
            return not self.cleanup_incomplete
        finally:
            self._lock.release()
