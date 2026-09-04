"""AWS SDK plumbing shared by the EC2 platform's ops: typed error
classification, per-VM security-group exposure mechanics, and the distinct
strict-delete and create-rollback cleanup paths.

Split out of ``platform.py`` mirroring ``plugins/azure/network.py``:
``platform.py`` keeps the ``VMPlatform`` capability surface (config, sessions,
sizing, ops); this module owns the network-resource mechanics those ops share.
The functions take an already-built boto3 EC2 client plus plain identifiers
(the security-group id, the backend name, the instance id), so this module
never touches ``VMRow`` or client caching. boto3/botocore imports stay
function-local, matching ``platform.py``, so aws modules never load at CLI
startup.

Exposure model (baseline deny, ephemeral scoped allows): a fresh EC2 security
group has NO inbound rules, which IS the deny-all-inbound baseline (a security
group denies every inbound flow it does not explicitly allow), so unlike
azure's NSG there is no deny rule to install: the empty group is the baseline.
SSH access happens through ephemeral ingress rules on TCP/22 scoped to the
operator's egress prefixes (plus ``operator.ssh_allow_cidrs``), authorized when
a route is needed and revoked after:

- the create bootstrap poke (operator prefixes), removed the moment Tailscale
  is confirmed or the create fails/aborts by revoking EXACTLY those prefixes
  (recorded in platform_metadata at create), the EC2 analog of azure deleting
  its fixed-name bootstrap allow. Revoking the recorded tuples rather than
  sweeping all ingress is what lets a concurrent ``vm shell --platform`` route's
  distinct allow survive the close (nothing serializes commands per VM);
- ``transient_route``'s per-operation poke, removed by revoking exactly the
  prefixes it authorized.

EC2-native divergence from azure's per-operation nonce-named rules: an EC2
ingress rule's identity is its ``(protocol, port, cidr)`` tuple, NOT a name or
description (a second authorize of an identical tuple returns
``InvalidPermission.Duplicate`` regardless of description; descriptions are
metadata, managed separately). So two concurrent routes from one operator
egress share ONE underlying rule rather than owning independent ones. The poke
is therefore idempotent (tolerate ``Duplicate``) and the per-operation remove
is tolerant (tolerate ``NotFound``); with a shared tuple the first route's exit
revokes it, and a still-active concurrent route from the same egress loses that
allow until its next poke. This fails CLOSED (the empty-group baseline), never
open, so the worst case is a benign re-poke on the next entry, never exposure.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from agentworks import output
from agentworks.errors import AgentworksError, AuthorizationError, ProvisioningError, TokenRejectedError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


# The tag key every agentworks-created EC2 resource carries, its value the
# backend name ({slug}-{vm} or {vm}). It is the collision-preflight key and the
# instance/volume/security-group marker. One spelling, shared by every writer
# and reader.
VM_TAG_KEY = "agentworks:vm"

# Codes that reject the credential itself. Runup and status share this set.
_CREDENTIAL_REJECTION_CODES = frozenset(
    {
        "AuthFailure",
        "InvalidClientTokenId",
        "SignatureDoesNotMatch",
        "UnrecognizedClientException",
        "InvalidAccessKeyId",
        "ExpiredToken",
        "ExpiredTokenException",
    }
)

# Codes that deny an authenticated identity's operation.
_AUTHORIZATION_DENIAL_CODES = frozenset({"UnauthorizedOperation", "AccessDenied", "AccessDeniedException"})
_DRY_RUN_ALLOWED_CODE = "DryRunOperation"

# The error codes describe_subnets answers with when a configured subnet does
# not exist in the region: a definitive misconfiguration.
SUBNET_NOT_FOUND_CODES = frozenset({"InvalidSubnetID.NotFound", "InvalidSubnet.NotFound"})

# A poke of an ingress tuple that already exists (a concurrent route or a
# leftover); a revoke of one that is already gone. Both are tolerated.
_DUPLICATE_PERMISSION_CODE = "InvalidPermission.Duplicate"
_PERMISSION_NOT_FOUND_CODE = "InvalidPermission.NotFound"
_GROUP_NOT_FOUND_CODES = frozenset({"InvalidGroup.NotFound", "InvalidGroupId.NotFound"})
_INSTANCE_NOT_FOUND_CODES = frozenset({"InvalidInstanceID.NotFound"})


class EC2Error(ProvisioningError):
    """An EC2 (AWS) API operation failed.

    Attributes:
        summary: A concise, user-facing error message.
        detail: The full error details (for logs).

    The optional entity / hint keywords are the base ``AgentworksError`` ones,
    forwarded so a failure that KNOWS which resource it is about can say so (the
    explicit-credential build names its site and its secret). :func:`wrap_ec2_error`,
    which converts an arbitrary SDK exception, has no such knowledge and passes
    none.
    """

    def __init__(
        self,
        summary: str,
        detail: str,
        *,
        entity_kind: str | None = None,
        entity_name: str | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(summary, entity_kind=entity_kind, entity_name=entity_name, hint=hint)
        self.summary = summary
        self.detail = detail


def error_code(exc: Exception) -> str | None:
    """The botocore error code for a ``ClientError`` (e.g. ``AuthFailure``,
    ``DependencyViolation``), or ``None`` for anything else. Reads the
    structured ``response['Error']['Code']`` rather than string-matching."""
    from botocore.exceptions import ClientError

    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
        code = error.get("Code")
        return str(code) if code else None
    return None


def wrap_ec2_error(exc: Exception) -> AgentworksError:
    """Map structured credential and permission codes to their domain types;
    retain :class:`EC2Error` for other SDK and transport failures."""
    from botocore.exceptions import ClientError

    if isinstance(exc, (AuthorizationError, TokenRejectedError)):
        return exc
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
        code = error.get("Code")
        message = error.get("Message") or str(exc)
        summary = f"{code}: {message}" if code else str(message)
        if code in _AUTHORIZATION_DENIAL_CODES:
            return AuthorizationError(f"AWS denied permission ({summary})")
        if code in _CREDENTIAL_REJECTION_CODES:
            return TokenRejectedError(f"AWS rejected the selected credential ({summary})")
        return EC2Error(summary, detail=str(exc))
    return EC2Error(str(exc), detail=str(exc))


def is_credential_rejection(exc: Exception) -> bool:
    """Whether a structured code definitively rejects the credential."""
    return error_code(exc) in _CREDENTIAL_REJECTION_CODES


def is_authorization_denial(exc: Exception) -> bool:
    """Whether a structured code denies the authenticated AWS identity."""
    return error_code(exc) in _AUTHORIZATION_DENIAL_CODES


def _check_ssh_revoke_permissions(ec2: Any, security_group_id: str, prefixes: Sequence[str]) -> None:
    """Dry-run every exact revoke before any matching authorize occurs."""
    for prefix in prefixes:
        permission = _ssh_permission(prefix)
        try:
            ec2.revoke_security_group_ingress(
                GroupId=security_group_id,
                IpPermissions=[permission],
                DryRun=True,
            )
        except Exception as exc:
            code = error_code(exc)
            if code == _DRY_RUN_ALLOWED_CODE:
                continue
            if code in _AUTHORIZATION_DENIAL_CODES:
                raise AuthorizationError(
                    f"AWS denied permission to revoke the planned SSH ingress rule ({code})",
                    entity_kind="security-group",
                    entity_name=security_group_id,
                ) from exc
            if code in _CREDENTIAL_REJECTION_CODES:
                raise TokenRejectedError(
                    f"AWS rejected the selected credential while checking SSH cleanup permission ({code})",
                    entity_kind="security-group",
                    entity_name=security_group_id,
                ) from exc
            raise EC2Error(
                "AWS could not confirm permission to revoke the planned SSH ingress rule",
                detail=str(exc),
                entity_kind="security-group",
                entity_name=security_group_id,
                hint="retry when AWS can return a definitive DryRun result",
            ) from exc
        raise EC2Error(
            "AWS did not confirm permission to revoke the planned SSH ingress rule",
            detail="the EC2 DryRun request returned normally instead of DryRunOperation",
            entity_kind="security-group",
            entity_name=security_group_id,
            hint="retry when AWS can return a definitive DryRun result",
        )


def vm_tag(backend_name: str) -> dict[str, str]:
    """The ``agentworks:vm=<backend-name>`` tag every created resource carries."""
    return {"Key": VM_TAG_KEY, "Value": backend_name}


def create_security_group(ec2: Any, backend_name: str, vpc_id: str) -> str:
    """Create the per-VM security group with NO ingress rules (the azure
    per-VM NSG analog). An EC2 security group with no ingress rules IS the
    deny-all-inbound baseline, so unlike azure there is no deny rule to install:
    the empty group is the baseline. Egress is the group's default allow-all.
    Created in the launch subnet's VPC so the group and the instance's network
    interface always share a VPC, and tagged for cleanup."""
    result = ec2.create_security_group(
        GroupName=f"agentworks-{backend_name}",
        Description=f"agentworks VM {backend_name}",
        VpcId=vpc_id,
        TagSpecifications=[
            {
                "ResourceType": "security-group",
                "Tags": [vm_tag(backend_name), {"Key": "Name", "Value": f"agentworks-{backend_name}"}],
            }
        ],
    )
    return str(result["GroupId"])


def _ssh_permission(prefix: str) -> dict[str, Any]:
    """One TCP/22 ingress permission scoped to ``prefix``."""
    return {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": prefix}]}


def poke_ssh_allow(ec2: Any, security_group_id: str, prefixes: Sequence[str]) -> None:
    """Authorize an ephemeral SSH allow on TCP/22 scoped to ``prefixes``.

    Dry-runs every exact matching revoke before any authorize. Then authorizes
    one prefix at a time so overlap with a concurrent route does not prevent
    non-overlapping prefixes from being added. Duplicate tuples are tolerated.
    If an actual authorize fails partway through, all planned tuples are
    revoked best-effort before the typed failure propagates.
    """
    _check_ssh_revoke_permissions(ec2, security_group_id, prefixes)
    output.info(f"Opening SSH route (allow scoped to {', '.join(prefixes)})...")
    try:
        for prefix in prefixes:
            try:
                ec2.authorize_security_group_ingress(
                    GroupId=security_group_id,
                    IpPermissions=[_ssh_permission(prefix)],
                )
            except Exception as exc:
                if error_code(exc) == _DUPLICATE_PERMISSION_CODE:
                    continue
                raise
    except KeyboardInterrupt:
        remove_ssh_allow(ec2, security_group_id, prefixes)
        raise
    except Exception as exc:
        failure = wrap_ec2_error(exc)
        remove_ssh_allow(ec2, security_group_id, prefixes)
        raise failure from exc


def remove_ssh_allow(ec2: Any, security_group_id: str, prefixes: Sequence[str]) -> None:
    """Revoke exactly the TCP/22 allows this operation authorized for
    ``prefixes``, restoring zero inbound exposure through them (the empty-group
    baseline stays, and any concurrent operation's non-overlapping allows are
    untouched).

    Best-effort per prefix: a tuple already gone (a concurrent route's exit
    revoked a shared prefix, or the group is gone) is tolerated; any other
    failure warns naming the prefix and the residual exposure rather than
    raising, because every call site must keep unwinding."""
    output.info(f"Closing SSH route (revoking allow scoped to {', '.join(prefixes)})...")
    for prefix in prefixes:
        try:
            ec2.revoke_security_group_ingress(
                GroupId=security_group_id,
                IpPermissions=[_ssh_permission(prefix)],
            )
        except Exception as exc:
            code = error_code(exc)
            if code == _PERMISSION_NOT_FOUND_CODE or code in _GROUP_NOT_FOUND_CODES:
                continue
            err = wrap_ec2_error(exc)
            output.warn(
                f"could not revoke the SSH allow for {prefix} on security group "
                f"'{security_group_id}': {err}. The VM's SSH port stays open to "
                f"{prefix}; revoke it manually to restore zero inbound exposure."
            )


def cleanup_created_resources(ec2: Any, instance_id: str | None, security_group_id: str | None) -> None:
    """Clean up known create-time resources or raise so core retains their row.

    A just-created EC2 identifier can answer ``NotFound`` while it propagates.
    Unlike explicit delete of an older, account-bound row, rollback must never
    interpret that response as proof the new resource disappeared. It retries
    the exact mutation and leaves an addressable failed row if AWS never gives
    a positive result.
    """
    failures: list[AgentworksError] = []
    if instance_id is not None:
        try:
            _terminate_created_instance(ec2, instance_id)
        except Exception as exc:
            failures.append(wrap_ec2_error(exc))
    if security_group_id is not None:
        try:
            _delete_created_security_group(ec2, security_group_id)
        except Exception as exc:
            failures.append(wrap_ec2_error(exc))
    if failures:
        raise EC2Error(
            "AWS could not confirm cleanup of newly created resources",
            detail="; ".join(str(failure) for failure in failures),
        )


def _terminate_created_instance(ec2: Any, instance_id: str) -> None:
    """Obtain a positive termination response for a newly returned ID."""
    _request_created_instance_termination(ec2, instance_id)
    try:
        ec2.get_waiter("instance_terminated").wait(InstanceIds=[instance_id])
    except Exception as exc:
        raise wrap_ec2_error(exc) from exc


def _request_created_instance_termination(ec2: Any, instance_id: str) -> None:
    """Require EC2 to accept termination of one newly observed instance."""
    attempts = 6
    for attempt in range(attempts):
        try:
            ec2.terminate_instances(InstanceIds=[instance_id])
        except Exception as exc:
            if error_code(exc) not in _INSTANCE_NOT_FOUND_CODES:
                raise wrap_ec2_error(exc) from exc
            if attempt + 1 == attempts:
                raise EC2Error(
                    f"AWS never accepted termination of newly created instance '{instance_id}'",
                    detail=f"TerminateInstances returned NotFound for {attempts} attempts",
                    entity_kind="instance",
                    entity_name=instance_id,
                ) from exc
            output.detail(f"Waiting for newly created instance '{instance_id}' to become visible for cleanup...")
            time.sleep(2**attempt)
            continue
        return


def _delete_created_security_group(ec2: Any, security_group_id: str) -> None:
    """Obtain a positive delete response for a newly returned group ID."""
    attempts = 12
    for attempt in range(attempts):
        try:
            ec2.delete_security_group(GroupId=security_group_id)
            return
        except Exception as exc:
            code = error_code(exc)
            retryable = code == "DependencyViolation" or code in _GROUP_NOT_FOUND_CODES
            if not retryable:
                raise wrap_ec2_error(exc) from exc
            if attempt + 1 == attempts:
                raise EC2Error(
                    f"AWS never accepted deletion of newly created security group '{security_group_id}'",
                    detail=f"DeleteSecurityGroup remained retryable for {attempts} attempts",
                    entity_kind="security-group",
                    entity_name=security_group_id,
                ) from exc
            output.detail(
                f"Waiting for newly created security group '{security_group_id}' to become deletable for cleanup..."
            )
            time.sleep(5)


def describe_instance_exact(ec2: Any, instance_id: str) -> dict[str, Any] | None:
    """Read one exact instance, returning ``None`` only for confirmed absence."""
    try:
        result = ec2.describe_instances(InstanceIds=[instance_id])
    except Exception as exc:
        if error_code(exc) in _INSTANCE_NOT_FOUND_CODES:
            return None
        raise wrap_ec2_error(exc) from exc
    try:
        instances = [
            instance for reservation in result.get("Reservations", []) for instance in reservation.get("Instances", [])
        ]
        matches = [instance for instance in instances if instance.get("InstanceId") == instance_id]
    except Exception as exc:
        raise EC2Error(f"EC2 returned malformed instance data for '{instance_id}'", detail=str(exc)) from exc
    if not instances:
        return None
    if len(instances) == 1 and len(matches) == 1:
        return dict(matches[0])
    raise EC2Error(
        f"EC2 did not return an exact instance match for '{instance_id}'",
        detail="DescribeInstances returned unexpected instance identities",
    )


def require_owned_instance(
    instance: Mapping[str, Any],
    backend_name: str,
    security_group_id: str,
) -> None:
    """Require the ownership tag and recorded security-group association."""
    tags = instance.get("Tags")
    groups = instance.get("SecurityGroups")
    owns_instance = isinstance(tags, list) and any(
        isinstance(tag, dict) and tag.get("Key") == VM_TAG_KEY and tag.get("Value") == backend_name for tag in tags
    )
    has_security_group = isinstance(groups, list) and any(
        isinstance(group, dict) and group.get("GroupId") == security_group_id for group in groups
    )
    if not owns_instance or not has_security_group:
        raise EC2Error(
            f"instance is not owned by backend '{backend_name}'",
            detail=f"the exact {VM_TAG_KEY} tag or recorded security-group association did not match",
            entity_kind="instance",
            entity_name=str(instance["InstanceId"]),
        )


def describe_security_group_exact(ec2: Any, security_group_id: str) -> dict[str, Any] | None:
    """Read one exact security group, returning ``None`` only for confirmed absence."""
    try:
        result = ec2.describe_security_groups(GroupIds=[security_group_id])
    except Exception as exc:
        if error_code(exc) in _GROUP_NOT_FOUND_CODES:
            return None
        raise wrap_ec2_error(exc) from exc
    try:
        groups = result.get("SecurityGroups", [])
        matches = [group for group in groups if group.get("GroupId") == security_group_id]
    except Exception as exc:
        raise EC2Error(
            f"EC2 returned malformed security-group data for '{security_group_id}'",
            detail=str(exc),
        ) from exc
    if not groups:
        return None
    if len(groups) == 1 and len(matches) == 1:
        return dict(matches[0])
    raise EC2Error(
        f"EC2 did not return an exact security-group match for '{security_group_id}'",
        detail="DescribeSecurityGroups returned unexpected group identities",
    )


def terminate_and_cleanup_strict(
    ec2: Any,
    instance_id: str | None,
    security_group_id: str,
    backend_name: str,
    *,
    account_bound: bool,
) -> None:
    """Verify exact ownership, terminate the instance, and delete its group."""
    instance = describe_instance_exact(ec2, instance_id) if instance_id is not None else None
    if instance_id is not None and instance is None:
        if not account_bound:
            raise EC2Error(
                f"cannot confirm absent legacy instance '{instance_id}' belongs to the current AWS account",
                detail="the VM row predates AWS account binding and the instance is not visible",
                entity_kind="instance",
                entity_name=instance_id,
            )
    elif instance is not None:
        require_owned_instance(instance, backend_name, security_group_id)

    security_group = describe_security_group_exact(ec2, security_group_id)
    if security_group is not None:
        tags = security_group.get("Tags")
        owned = isinstance(tags, list) and any(
            isinstance(tag, dict) and tag.get("Key") == VM_TAG_KEY and tag.get("Value") == backend_name for tag in tags
        )
        if not owned:
            raise EC2Error(
                f"security group '{security_group_id}' is not owned by backend '{backend_name}'",
                detail=f"the exact {VM_TAG_KEY} tag did not match",
                entity_kind="security-group",
                entity_name=security_group_id,
            )
    if instance_id is not None and instance is not None:
        try:
            ec2.terminate_instances(InstanceIds=[instance_id])
        except Exception as exc:
            if error_code(exc) not in _INSTANCE_NOT_FOUND_CODES or not account_bound:
                raise wrap_ec2_error(exc) from exc
        else:
            try:
                ec2.get_waiter("instance_terminated").wait(InstanceIds=[instance_id])
            except Exception as exc:
                raise wrap_ec2_error(exc) from exc

    if security_group is not None:
        _delete_security_group_strict(ec2, security_group_id)


def _delete_security_group_strict(ec2: Any, security_group_id: str) -> None:
    """Delete one security group or raise after bounded dependency retries."""
    attempts = 12
    for attempt in range(attempts):
        try:
            ec2.delete_security_group(GroupId=security_group_id)
            return
        except Exception as exc:
            code = error_code(exc)
            if code in _GROUP_NOT_FOUND_CODES:
                return
            if code == "DependencyViolation" and attempt + 1 < attempts:
                time.sleep(5)
                continue
            if code == "DependencyViolation":
                raise EC2Error(
                    f"security group '{security_group_id}' still had dependencies after {attempts} delete attempts",
                    detail=str(exc),
                    entity_kind="security-group",
                    entity_name=security_group_id,
                ) from exc
            raise wrap_ec2_error(exc) from exc


def first_instance_state(describe_result: Mapping[str, Any]) -> str | None:
    """The state name of the first instance in a describe_instances result, or
    None when the result is empty (instance gone)."""
    for reservation in describe_result.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            return str(instance.get("State", {}).get("Name") or "") or None
    return None


def first_instance_public_ip(describe_result: Mapping[str, Any]) -> str:
    """The public IP of the first instance in a describe_instances result, or
    an empty string when none is attached. Always read LIVE off a fresh
    describe: EC2 releases the auto-assigned public IP on stop and assigns a
    different one on start, so it is never cached in platform_metadata."""
    for reservation in describe_result.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            return str(instance.get("PublicIpAddress") or "")
    return ""
