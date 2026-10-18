"""Exact EC2 native SSH rule ownership, independent of legacy tuple cleanup."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from ipaddress import IPv4Network
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agentworks.db import LifecycleObligationState
from agentworks.errors import AgentworksError, StateError, ValidationError
from agentworks.operations import LifecycleObligation
from agentworks.plugins.aws.network import error_code, require_owned_instance, wrap_ec2_error

if TYPE_CHECKING:
    from agentworks.execution.carrier import Deadline
    from agentworks.operations import OperationOwner
    from agentworks.plugins.aws._owned_auth import _OwnedRoleSession

KIND = "aws-ec2-native-route"


@dataclass
class _Rule:
    prefix: str
    nonce: str
    obligation_id: str
    rule_id: str | None = None
    obligation: LifecycleObligation | None = None
    armed: bool = False
    removed: bool = False
    resolving: bool = False


class EC2NativeRoute:
    """Caller-serialized original clients and one obligation per planned rule.

    Only a validated successful authorize receipt establishes rule-ID ownership.
    Missing receipts remain possible effect; an empty observation cannot drain an
    unknown queued authorize. Cleanup never adopts matching preexisting tuples.
    """

    def __init__(
        self,
        owner: OperationOwner,
        vm_name: str,
        session: Any,
        account: str,
        region: str,
        group: str,
        backend: str,
        instance: dict[str, Any],
        prefixes: tuple[str, ...],
        *,
        role_auth: _OwnedRoleSession | None = None,
    ) -> None:
        if re.fullmatch(r"sg-[0-9a-f]{8}(?:[0-9a-f]{9})?", group) is None or not backend:
            raise ValidationError("EC2 native route selection is invalid")
        require_owned_instance(instance, backend, group)
        if not prefixes or len(prefixes) > 64 or any(str(IPv4Network(p)) != p for p in prefixes):
            raise ValidationError("EC2 native route prefixes are invalid")
        self._owner = owner
        self._vm_name = vm_name
        self._session = session
        self._account = account
        self._region = region
        self._group = group
        self._backend = backend
        self._vpc = instance.get("VpcId")
        if type(self._vpc) is not str or not self._vpc:
            raise ValidationError("EC2 native route requires the selected VPC")
        self._role_auth = role_auth
        self._rules = [_Rule(p, uuid4().hex, uuid4().hex) for p in prefixes]
        self._client: Any = None
        self._used = False

    @property
    def cleanup_incomplete(self) -> bool:
        return self._client is not None

    def _remaining(self, deadline: Deadline) -> float:
        from agentworks.capabilities.vm_platform.base import provider_locator_remaining

        return provider_locator_remaining(deadline, vm_name=self._vm_name)

    def _payload(self, rule: _Rule, *, initial: bool = False) -> bytes:
        return json.dumps(
            {
                "account": self._account,
                "region": self._region,
                "group": self._group,
                "prefix": rule.prefix,
                "nonce": rule.nonce,
                "rule_id": None if initial else rule.rule_id,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")

    def _connect(self, deadline: Deadline) -> None:
        from botocore.config import Config

        if self._client is not None:
            raise StateError("EC2 native route retains an original client")
        remaining = self._remaining(deadline)
        self._client = self._session.client(
            "ec2",
            region_name=self._region,
            config=Config(
                connect_timeout=remaining, read_timeout=remaining, retries={"total_max_attempts": 1, "mode": "standard"}
            ),
        )
        if self._role_auth is not None:
            for operation in (
                "DescribeSecurityGroups",
                "DescribeSecurityGroupRules",
                "RevokeSecurityGroupIngress",
                "AuthorizeSecurityGroupIngress",
            ):
                self._client.meta.events.register(f"before-send.ec2.{operation}", self._role_auth.guard_ec2_send)
        self._remaining(deadline)

    def close(self, deadline: Deadline) -> bool:
        """Retry public close on the actual returned original only."""
        self._remaining(deadline)
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            else:
                self._client = None
        self._remaining(deadline)
        return not self.cleanup_incomplete

    def _finish(self, deadline: Deadline, control: BaseException | None) -> None:
        # Even expiry must attempt retirement of the already returned original.
        try:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                else:
                    self._client = None
        except BaseException:
            if control is not None:
                raise control from None
            raise

    @staticmethod
    def _receipt(result: Any) -> None:
        metadata = result.get("ResponseMetadata") if type(result) is dict else None
        if (
            type(metadata) is not dict
            or type(metadata.get("HTTPStatusCode")) is not int
            or not 200 <= metadata["HTTPStatusCode"] < 300
            or type(metadata.get("RetryAttempts")) is not int
            or metadata["RetryAttempts"] != 0
            or type(metadata.get("RequestId")) is not str
            or not metadata["RequestId"]
        ):
            raise ValidationError("EC2 native route response is invalid")

    def _validate_group(self, deadline: Deadline) -> None:
        self._remaining(deadline)
        result = self._client.describe_security_groups(GroupIds=[self._group])
        self._remaining(deadline)
        self._receipt(result)
        groups = result.get("SecurityGroups")
        if type(groups) is not list or len(groups) != 1 or type(groups[0]) is not dict:
            raise ValidationError("EC2 native route group observation is invalid")
        group = groups[0]
        tags = group.get("Tags")
        if (
            group.get("GroupId") != self._group
            or group.get("OwnerId") != self._account
            or group.get("VpcId") != self._vpc
            or type(tags) is not list
            or sum(t == {"Key": "agentworks:vm", "Value": self._backend} for t in tags) != 1
        ):
            raise StateError("EC2 native route group ownership changed")

    def _describe(self, deadline: Deadline) -> list[Any]:
        self._remaining(deadline)
        result = self._client.describe_security_group_rules(Filters=[{"Name": "group-id", "Values": [self._group]}])
        self._remaining(deadline)
        self._receipt(result)
        rules = result.get("SecurityGroupRules")
        if type(rules) is not list or result.get("NextToken"):
            raise ValidationError("EC2 native route rule observation is incomplete")
        for rule in rules:
            if (
                type(rule) is not dict
                or rule.get("GroupId") != self._group
                or rule.get("GroupOwnerId") != self._account
            ):
                raise ValidationError("EC2 native route rule identity is invalid")
        return rules

    def _validate_rule(self, value: Any, rule: _Rule) -> str:
        if (
            type(value) is not dict
            or value.get("GroupId") != self._group
            or value.get("GroupOwnerId") != self._account
            or value.get("IsEgress") is not False
            or value.get("IpProtocol") != "tcp"
            or type(value.get("FromPort")) is not int
            or value["FromPort"] != 22
            or type(value.get("ToPort")) is not int
            or value["ToPort"] != 22
            or value.get("CidrIpv4") != rule.prefix
            or value.get("Description") != f"agentworks-native:{rule.nonce}"
            or type(value.get("SecurityGroupRuleId")) is not str
            or re.fullmatch(r"sgr-[0-9a-f]{8}(?:[0-9a-f]{9})?", value["SecurityGroupRuleId"]) is None
        ):
            raise ValidationError("EC2 native route authorize identity is invalid")
        rule_id: str = value["SecurityGroupRuleId"]
        if rule.rule_id is not None and rule.rule_id != rule_id:
            raise StateError("EC2 native route rule ID changed")
        return rule_id

    def open(self, deadline: Deadline) -> None:
        if self._used:
            raise StateError("EC2 native route admission is single use")
        self._used = True
        control: BaseException | None = None
        try:
            self._connect(deadline)
            self._validate_group(deadline)
            existing = self._describe(deadline)
            for rule in self._rules:
                if any(
                    r.get("IsEgress") is False
                    and r.get("IpProtocol") == "tcp"
                    and r.get("FromPort") == 22
                    and r.get("ToPort") == 22
                    and r.get("CidrIpv4") == rule.prefix
                    for r in existing
                ):
                    raise StateError("EC2 native route tuple already exists")
            for rule in self._rules:
                self._remaining(deadline)
                try:
                    self._client.revoke_security_group_ingress(
                        GroupId=self._group,
                        DryRun=True,
                        IpPermissions=[
                            {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": rule.prefix}]}
                        ],
                    )
                except AgentworksError:
                    raise
                except Exception as error:
                    if error_code(error) != "DryRunOperation":
                        raise wrap_ec2_error(error) from error
                else:
                    raise StateError("EC2 native route cleanup permission was not confirmed")
                self._remaining(deadline)
            for rule in self._rules:
                rule.obligation = self._owner.register_lifecycle_obligation(
                    KIND,
                    payload_version=1,
                    payload=self._payload(rule),
                    obligation_id=rule.obligation_id,
                )
                self._remaining(deadline)
                rule.armed = True
                rule.obligation.mark_possible_effect()
                self._remaining(deadline)
                result = self._client.authorize_security_group_ingress(
                    GroupId=self._group,
                    IpPermissions=[
                        {
                            "IpProtocol": "tcp",
                            "FromPort": 22,
                            "ToPort": 22,
                            "IpRanges": [{"CidrIp": rule.prefix, "Description": f"agentworks-native:{rule.nonce}"}],
                        }
                    ],
                )
                self._receipt(result)
                values = result.get("SecurityGroupRules")
                if result.get("Return") is not True or type(values) is not list or len(values) != 1:
                    raise ValidationError("EC2 native route authorize receipt is invalid")
                rule.rule_id = self._validate_rule(values[0], rule)
                self._reconcile(rule)
                self._remaining(deadline)
        except BaseException as error:
            if not isinstance(error, Exception):
                control = error
            raise
        finally:
            self._finish(deadline, control)

    def _reconcile(self, rule: _Rule) -> LifecycleObligation | None:
        row = self._owner.inspect_lifecycle_obligation(rule.obligation_id)
        if row is None:
            if rule.armed or rule.obligation is not None:
                raise StateError("EC2 native route registration is uncertain")
            return None
        if (
            row.obligation_kind != KIND
            or row.payload_version != 1
            or row.ownership != self._owner.ownership
            or (row.payload, row.payload_revision)
            not in ((self._payload(rule, initial=True), 0), (self._payload(rule), 1))
        ):
            raise StateError("EC2 native route custody conflicts")
        if row.state is LifecycleObligationState.RESOLVED and rule.armed and not rule.resolving:
            raise StateError("EC2 native route was prematurely resolved")
        rule.obligation = LifecycleObligation(self._owner, row)
        if (
            row.state is LifecycleObligationState.POSSIBLE_EFFECT
            and rule.rule_id is not None
            and row.payload_revision == 0
        ):
            rule.obligation.publish_payload(expected_revision=0, payload_version=1, payload=self._payload(rule))
        return rule.obligation

    def check(self, deadline: Deadline) -> None:
        control: BaseException | None = None
        try:
            self._connect(deadline)
            self._validate_group(deadline)
            current = self._describe(deadline)
            for rule in self._rules:
                matches = [r for r in current if r.get("SecurityGroupRuleId") == rule.rule_id]
                if rule.rule_id is None or len(matches) != 1 or rule.removed:
                    raise StateError("EC2 native route is no longer available")
                self._validate_rule(matches[0], rule)
        except BaseException as error:
            if not isinstance(error, Exception):
                control = error
            raise
        finally:
            self._finish(deadline, control)

    def settle(self, deadline: Deadline) -> bool:
        control: BaseException | None = None
        complete = True
        try:
            for rule in self._rules:
                obligation = self._reconcile(rule)
                if obligation is None or obligation.state is LifecycleObligationState.RESOLVED:
                    continue
                if not rule.armed:
                    rule.resolving = True
                    obligation.resolve()
                    continue
                if rule.rule_id is None:
                    complete = False
                    continue
                if not rule.removed:
                    if self._client is None:
                        self._connect(deadline)
                    self._validate_group(deadline)
                    current = self._describe(deadline)
                    matches = [r for r in current if r.get("SecurityGroupRuleId") == rule.rule_id]
                    if matches:
                        if len(matches) != 1:
                            raise ValidationError("EC2 native route has duplicate rule IDs")
                        self._validate_rule(matches[0], rule)
                        self._remaining(deadline)
                        try:
                            self._client.revoke_security_group_ingress(
                                GroupId=self._group,
                                SecurityGroupRuleIds=[rule.rule_id],
                            )
                        except Exception as error:
                            if error_code(error) != "InvalidSecurityGroupRuleId.NotFound":
                                raise
                    # Fresh positive absence is separate from revoke acknowledgment.
                    if any(r.get("SecurityGroupRuleId") == rule.rule_id for r in self._describe(deadline)):
                        complete = False
                        continue
                    rule.removed = True
                rule.resolving = True
                obligation.resolve()
            return complete
        except BaseException as error:
            if not isinstance(error, Exception):
                control = error
            raise
        finally:
            self._finish(deadline, control)
