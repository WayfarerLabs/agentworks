"""Access-local configured credentials and returned STS public-close custody."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentworks.capabilities.vm_platform.base import provider_locator_remaining
from agentworks.errors import StateError
from agentworks.plugins.aws.network import EC2Error

if TYPE_CHECKING:
    from agentworks.execution.carrier import Deadline
    from agentworks.plugins.aws.config import AwsAccessKeyAuth


class _OwnedRoleSession:
    """One configured session's STS original, serialized by its access owner.

    Callbacks are admitted only within the owner's original operation deadline.
    SDK constructor handoff, arbitrary signals and ambient provider resources
    remain outside this returned-original public-close responsibility.
    """

    def __init__(self, vm_name: str) -> None:
        self._vm_name = vm_name
        self._deadline: Deadline | None = None
        self._client: Any = None
        self._closed = False
        self._base: Any = None
        self._fetcher: Any = None

    @property
    def cleanup_incomplete(self) -> bool:
        return self._client is not None

    def begin(self, deadline: Deadline) -> None:
        if self._deadline is not None or self._closed or self.cleanup_incomplete:
            raise self._refusal()
        provider_locator_remaining(deadline, vm_name=self._vm_name)
        self._deadline = deadline

    def end(self) -> None:
        self._deadline = None

    def _refusal(self) -> StateError:
        return StateError(
            "Configured AWS credential work cannot be admitted",
            entity_kind="vm",
            entity_name=self._vm_name,
        )

    def _remaining(self) -> float:
        if self._closed or self._deadline is None:
            raise self._refusal()
        return provider_locator_remaining(self._deadline, vm_name=self._vm_name)

    def guard_ec2_send(self, **kwargs: Any) -> None:
        """Recheck after signing, including SDK-suppressed advisory errors."""
        self._remaining()
        if self.cleanup_incomplete:
            raise self._refusal()

    def _guard_sts_send(self, **kwargs: Any) -> None:
        self._remaining()
        if self._client is None:
            raise self._refusal()

    def _create_sts_client(self, service: str, **kwargs: Any) -> Any:
        from botocore.config import Config

        remaining = self._remaining()
        if self.cleanup_incomplete or service != "sts":
            raise self._refusal()
        self._client = self._base.create_client(
            service,
            **kwargs,
            config=Config(
                connect_timeout=remaining,
                read_timeout=remaining,
                retries={"total_max_attempts": 1, "mode": "standard"},
            ),
        )
        self._remaining()
        self._client.meta.events.register("before-send.sts.AssumeRole", self._guard_sts_send)
        return self._client

    def _refresh(self) -> Any:
        control: BaseException | None = None
        try:
            try:
                self._remaining()
                if self.cleanup_incomplete:
                    raise self._refusal()
                return self._fetcher.fetch_credentials()
            except BaseException as error:
                if not isinstance(error, Exception):
                    control = error
                raise
            finally:
                self.close_client()
        except BaseException:
            if control is not None:
                raise control from None
            raise

    def close_client(self) -> None:
        """Retry only the returned original; exceptional close retains it."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            else:
                self._client = None

    def stop(self) -> None:
        self._closed = True

    def build_session(self, auth: AwsAccessKeyAuth, secret: str, site: str, region: str) -> Any:
        import boto3
        import botocore.session
        from botocore.credentials import AssumeRoleCredentialFetcher, DeferredRefreshableCredentials

        self._remaining()
        if not secret:
            raise EC2Error(
                f"could not authenticate the AWS credentials for vm-site '{site}' "
                f"(access key {auth.access_key_id}, secret '{auth.access_key_secret}'): the resolved secret is empty",
                detail="the framework resolved the configured secret to an empty string",
                entity_kind="vm-site",
                entity_name=site,
                hint=f"check the value of the '{auth.access_key_secret}' secret",
            )
        base = boto3.session.Session(
            aws_access_key_id=auth.access_key_id,
            aws_secret_access_key=secret,
            region_name=region,
        )
        if auth.assume_role_arn is None:
            return base
        self._base = base._session
        self._fetcher = AssumeRoleCredentialFetcher(
            client_creator=self._create_sts_client,
            source_credentials=self._base.get_credentials(),
            role_arn=auth.assume_role_arn,
            extra_args={"RoleSessionName": "agentworks"},
        )
        assumed = botocore.session.Session()
        assumed._credentials = DeferredRefreshableCredentials(method="assume-role", refresh_using=self._refresh)
        assumed.set_config_variable("region", region)
        return boto3.session.Session(botocore_session=assumed)


def _build_access_key_session(
    auth: AwsAccessKeyAuth, secret: str, site: str, region: str, *, owner: _OwnedRoleSession
) -> Any:
    """Keep the owned builder distinct from legacy session/client caches."""
    return owner.build_session(auth, secret, site, region)
