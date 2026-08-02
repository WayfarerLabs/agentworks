"""AWS SDK plumbing shared by the EC2 platform's ops: the typed error
wrapper, the auth-rejection classifier, the per-VM security-group exposure
mechanics, and the resource-cleanup / create-rollback sweeps.

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
from agentworks.errors import ProvisioningError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


# The tag key every agentworks-created EC2 resource carries, its value the
# backend name ({slug}-{vm} or {vm}). It is the collision-preflight key and the
# instance/volume/security-group marker. One spelling, shared by every writer
# and reader.
VM_TAG_KEY = "agentworks:vm"

# Stamped into every ephemeral SSH allow's rule description, with a created-at
# timestamp, so a future doctor stale-rule sweep can flag rules leaked by
# killed processes. Descriptions do NOT participate in EC2 rule identity (that
# is the (protocol, port, cidr) tuple), so this is observability only.
SSH_ALLOW_DESCRIPTION_MARKER = "agentworks scoped SSH allow"

# botocore error codes that mean AWS DEFINITIVELY rejected the credential, as
# opposed to a transient failure or an unreachable endpoint. This is the split
# azure-identity cannot make (it collapses both into one exception); on EC2 it
# lets runup fail fatally on a real rejection and status re-raise typed rather
# than degrade to UNKNOWN. Spans the EC2 request-signing codes and the STS/SSM
# spellings of the same faults.
_AUTH_REJECTION_CODES = frozenset(
    {
        "AuthFailure",
        "UnauthorizedOperation",
        "InvalidClientTokenId",
        "SignatureDoesNotMatch",
        "AccessDenied",
        "AccessDeniedException",
        "UnrecognizedClientException",
        "InvalidAccessKeyId",
        "ExpiredToken",
        "ExpiredTokenException",
    }
)

# The error codes describe_subnets answers with when a configured subnet does
# not exist in the region: a definitive misconfiguration.
SUBNET_NOT_FOUND_CODES = frozenset({"InvalidSubnetID.NotFound", "InvalidSubnet.NotFound"})

# A poke of an ingress tuple that already exists (a concurrent route or a
# leftover); a revoke of one that is already gone. Both are tolerated.
_DUPLICATE_PERMISSION_CODE = "InvalidPermission.Duplicate"
_PERMISSION_NOT_FOUND_CODE = "InvalidPermission.NotFound"
_GROUP_NOT_FOUND_CODES = frozenset({"InvalidGroup.NotFound", "InvalidGroupId.NotFound"})
# A terminate of an instance that is already terminated or never existed: silent
# success (the delete op and rollback are idempotent).
_INSTANCE_NOT_FOUND_CODES = frozenset({"InvalidInstanceID.NotFound", "InvalidInstanceID.Malformed"})


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


def wrap_ec2_error(exc: Exception) -> EC2Error:
    """Convert a boto3/botocore exception into an :class:`EC2Error`, unwrapping
    a ``ClientError`` to its most specific ``code: message``."""
    from botocore.exceptions import ClientError

    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
        code = error.get("Code")
        message = error.get("Message") or str(exc)
        summary = f"{code}: {message}" if code else str(message)
        return EC2Error(summary, detail=str(exc))
    return EC2Error(str(exc), detail=str(exc))


def is_auth_rejection(exc: Exception) -> bool:
    """Whether ``exc`` is a DEFINITIVE credential rejection (an auth-coded
    ``ClientError``), as opposed to a transient or unreachable failure. The
    single classifier shared by ``runup`` (which makes it fatal) and ``status``
    (which re-raises it typed rather than degrading to UNKNOWN), so the two
    stages agree on what "rejected" means from one code set."""
    return error_code(exc) in _AUTH_REJECTION_CODES


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


def _ssh_permission(prefix: str, *, with_description: bool) -> dict[str, Any]:
    """One TCP/22 ingress permission scoped to ``prefix``. The description is
    added on authorize (for the future doctor sweep) and omitted on revoke
    (revoke matches by the (protocol, port, cidr) tuple; the description is not
    part of identity)."""
    ip_range: dict[str, str] = {"CidrIp": prefix}
    if with_description:
        from datetime import UTC, datetime

        created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        ip_range["Description"] = f"{SSH_ALLOW_DESCRIPTION_MARKER} (created {created})"
    return {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [ip_range]}


def poke_ssh_allow(ec2: Any, security_group_id: str, prefixes: Sequence[str]) -> None:
    """Authorize an ephemeral SSH allow on TCP/22 scoped to ``prefixes``.

    Authorizes one prefix at a time (rather than all in one call) so a partial
    overlap with a concurrent route's rules still adds the non-overlapping
    prefixes: EC2 rejects a whole authorize call if ANY of its tuples already
    exists. A tuple that already exists (a concurrent route or a leftover) is
    tolerated, because the allow we need is present either way."""
    output.info(f"Opening SSH route (allow scoped to {', '.join(prefixes)})...")
    for prefix in prefixes:
        try:
            ec2.authorize_security_group_ingress(
                GroupId=security_group_id,
                IpPermissions=[_ssh_permission(prefix, with_description=True)],
            )
        except Exception as exc:
            if error_code(exc) == _DUPLICATE_PERMISSION_CODE:
                continue
            raise wrap_ec2_error(exc) from exc


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
                IpPermissions=[_ssh_permission(prefix, with_description=False)],
            )
        except Exception as exc:
            code = error_code(exc)
            if code == _PERMISSION_NOT_FOUND_CODE or code in _GROUP_NOT_FOUND_CODES:
                continue
            err = wrap_ec2_error(exc)
            output.warn(
                f"could not revoke the SSH allow for {prefix} on security group "
                f"'{security_group_id}': {err.summary}. The VM's SSH port stays open to "
                f"{prefix}; revoke it manually to restore zero inbound exposure."
            )


def delete_security_group(ec2: Any, security_group_id: str) -> None:
    """Delete the per-VM security group, waiting out the ENI detach a just
    terminated instance leaves behind: the delete raises ``DependencyViolation``
    until the network interface is gone, so retry with a bounded backoff.
    Already-gone (``InvalidGroup.NotFound``) is success."""
    for _attempt in range(12):
        try:
            ec2.delete_security_group(GroupId=security_group_id)
            return
        except Exception as exc:
            code = error_code(exc)
            if code in _GROUP_NOT_FOUND_CODES:
                return
            if code == "DependencyViolation":
                time.sleep(5)
                continue
            output.warn(f"could not delete security group '{security_group_id}': {wrap_ec2_error(exc).summary}")
            return
    output.warn(f"security group '{security_group_id}' still had dependencies after retries; leaving it")


def terminate_and_cleanup(ec2: Any, instance_id: str, security_group_id: str | None) -> None:
    """Terminate the instance, wait out its ENI detach, then delete the per-VM
    security group. Best-effort per resource, idempotent (already-gone
    succeeds). The one place the terminate-then-delete-group ordering lives:
    shared by the delete op and the create rollback paths.

    Unexpected failures WARN (mirroring :func:`delete_security_group`), rather
    than being swallowed silently: a real ``AuthFailure`` on terminate must not
    hide behind the later security-group ``DependencyViolation`` warning and
    misattribute the cause. Already-gone stays silent."""
    already_gone = _terminate_instance(ec2, instance_id)
    if not already_gone:
        try:
            ec2.get_waiter("instance_terminated").wait(InstanceIds=[instance_id])
        except Exception as exc:
            output.warn(
                f"waiting for instance '{instance_id}' to terminate failed: {wrap_ec2_error(exc).summary}; "
                f"the security-group delete will retry through any lingering dependency"
            )
    if security_group_id:
        delete_security_group(ec2, security_group_id)


def _terminate_instance(ec2: Any, instance_id: str) -> bool:
    """Terminate ``instance_id``; return True when it was already gone (so the
    caller can skip the termination waiter). An already-terminated / absent
    instance is silent success; any other failure warns rather than raising,
    because the cleanup paths must keep unwinding."""
    try:
        ec2.terminate_instances(InstanceIds=[instance_id])
        return False
    except Exception as exc:
        if error_code(exc) in _INSTANCE_NOT_FOUND_CODES:
            return True
        output.warn(f"could not terminate instance '{instance_id}': {wrap_ec2_error(exc).summary}")
        return False


def cleanup_partial(ec2: Any, instance_id: str | None, security_group_id: str | None) -> None:
    """Best-effort teardown of the resources ``create`` built before a plain
    (non-interrupt) failure: terminate the instance if one launched, delete the
    security group once the ENI detaches. This is the platform's own
    partial-work sweep, distinct from (and composing under) the orchestrator's
    DB-row unwind."""
    if instance_id:
        terminate_and_cleanup(ec2, instance_id, security_group_id)
    elif security_group_id:
        delete_security_group(ec2, security_group_id)


def rollback_create_on_interrupt(
    ec2: Any,
    region: str,
    backend_name: str,
    instance_id: str | None,
    security_group_id: str | None,
) -> None:
    """Roll back a partially created resource set after an operator interrupt
    inside ``EC2Platform.create``.

    The instance may fully exist here (the likeliest interrupt point is the
    minutes-long inline bootstrap wait, after every resource is up), so this
    mirrors the delete op: terminate the instance, then delete the security
    group once the ENI detaches. Best-effort per resource. A SECOND interrupt
    during the cleanup abandons it cleanly instead of wedging: the surviving
    resources are named for manual removal, and the original interrupt still
    propagates to ``create_vm``, whose unwind then deletes the DB row it no
    longer needs."""
    output.warn(
        f"Interrupted: cleaning up partial AWS resources for '{backend_name}', "
        f"please wait (Ctrl-C again to abandon them)..."
    )
    try:
        if instance_id:
            terminate_and_cleanup(ec2, instance_id, security_group_id)
        elif security_group_id:
            delete_security_group(ec2, security_group_id)
    except KeyboardInterrupt:
        output.warn(
            f"Cleanup abandoned: EC2 resources tagged '{VM_TAG_KEY}={backend_name}' may remain "
            f"in region '{region}'; terminate the instance and delete its security group there manually."
        )


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
