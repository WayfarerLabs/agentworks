"""AWS ambient, access-key, and auto-refreshing role sessions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentworks.plugins.aws.network import EC2Error

if TYPE_CHECKING:
    from agentworks.plugins.aws.config import AwsAccessKeyAuth


def _build_ambient_session(region: str) -> Any:
    """Build boto3's ambient default-credential session."""
    import boto3

    return boto3.session.Session(region_name=region)


def _build_access_key_session(
    auth: AwsAccessKeyAuth,
    secret_value: str,
    site_name: str,
    region: str,
) -> Any:
    """Build the site's explicit session, optionally assuming a role."""
    import boto3

    if not secret_value:
        raise EC2Error(
            f"could not authenticate the AWS credentials for vm-site '{site_name}' "
            f"(access key {auth.access_key_id}, secret '{auth.access_key_secret}'): the resolved secret is empty",
            detail="the framework resolved the configured secret to an empty string",
            entity_kind="vm-site",
            entity_name=site_name,
            hint=(
                f"check the value of the '{auth.access_key_secret}' secret (its default env-var backend key is "
                "AW_SECRET_AWS_SECRET_ACCESS_KEY)"
            ),
        )
    base = boto3.session.Session(
        aws_access_key_id=auth.access_key_id,
        aws_secret_access_key=secret_value,
        region_name=region,
    )
    if auth.assume_role_arn is None:
        return base
    return _assume_role_session(base, auth.assume_role_arn, region)


def _assume_role_session(base: Any, role_arn: str, region: str) -> Any:
    """Build auto-refreshing deferred AssumeRole credentials."""
    import boto3
    import botocore.session
    from botocore.credentials import AssumeRoleCredentialFetcher, DeferredRefreshableCredentials

    base_botocore = base._session
    fetcher = AssumeRoleCredentialFetcher(
        client_creator=base_botocore.create_client,
        source_credentials=base_botocore.get_credentials(),
        role_arn=role_arn,
        extra_args={"RoleSessionName": "agentworks"},
    )
    assumed = botocore.session.Session()
    assumed._credentials = DeferredRefreshableCredentials(
        method="assume-role",
        refresh_using=fetcher.fetch_credentials,
    )
    assumed.set_config_variable("region", region)
    return boto3.session.Session(botocore_session=assumed)
