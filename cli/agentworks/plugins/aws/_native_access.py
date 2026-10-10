"""Private retained EC2 observations and direct-client public-close custody."""

from __future__ import annotations

from threading import TIMEOUT_MAX, Lock
from typing import TYPE_CHECKING, Any

from agentworks.capabilities.vm_platform.base import (
    ProviderLocator,
    provider_locator_remaining,
)
from agentworks.db import VMStatus
from agentworks.errors import AgentworksError, NotFoundError, StateError
from agentworks.plugins.aws._owned_auth import _OwnedRoleSession
from agentworks.plugins.aws.auth import _build_ambient_session
from agentworks.plugins.aws.config import AwsAmbientAuth
from agentworks.plugins.aws.network import error_code, wrap_ec2_error

if TYPE_CHECKING:
    from agentworks.capabilities.base import RunContext
    from agentworks.db import VMRow
    from agentworks.execution.carrier import Deadline
    from agentworks.plugins.aws.platform import EC2Platform


class EC2OwnedAccess:
    """Caller-retained exact reads, without activation or native preparation.

    Construction is passive. Retain this object before observing it. The slot
    covers returned EC2 and configured-role STS originals, not arbitrary ambient
    credential clients or SDK constructor handoff. Normal public close is a
    client lifecycle fact, not synchronous closure of all sockets.
    """

    def __init__(self, vm: VMRow, platform: EC2Platform, ctx: RunContext) -> None:
        self._vm = vm
        self._platform = platform
        self._ctx = ctx
        self._session: Any = None
        self._read_client: Any = None
        self._role_auth: _OwnedRoleSession | None = None
        self._closed = False
        self._lock = Lock()

    @property
    def cleanup_incomplete(self) -> bool:
        """Whether either concrete original lacks a normal public-close return."""
        return self._read_client is not None or (self._role_auth is not None and self._role_auth.cleanup_incomplete)

    def observe_power(self, deadline: Deadline) -> VMStatus:
        _, instance = self._read_exact_instance(deadline)
        state = instance.get("State")
        name = state.get("Name") if isinstance(state, dict) else None
        if type(name) is not str:
            return VMStatus.UNKNOWN
        return {"running": VMStatus.RUNNING, "stopped": VMStatus.STOPPED}.get(name, VMStatus.UNKNOWN)

    def observe_locator(self, deadline: Deadline) -> ProviderLocator:
        """Bound exact-identity callback for the retained caller."""
        locator, _ = self._read_exact_instance(deadline)
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
