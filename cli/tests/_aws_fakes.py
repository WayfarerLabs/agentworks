"""In-process boto3 fakes for the EC2 platform tests: no live AWS.

Mirrors the azure suite's ``_install_fakes`` pattern: patch the SDK entry point
the platform imports function-locally (``boto3.session.Session``) with a
recording fake, so ``EC2Platform`` builds sessions and clients against
controllable stand-ins. The one ``Recorder`` returned captures every session
build (with the credentials it was handed) and every client call. The fake EC2
client also tracks per-security-group ingress state so the exposure tests can
assert what is authorized and revoked, and reproduce the shared-tuple
concurrency behavior (a duplicate authorize is ``InvalidPermission.Duplicate``,
a missing revoke is ``InvalidPermission.NotFound``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError, EndpointConnectionError

if TYPE_CHECKING:
    import pytest


def client_error(code: str, message: str = "boom", operation: str = "Op") -> ClientError:
    """A botocore ``ClientError`` carrying ``code`` in the structured
    ``response['Error']['Code']`` slot the platform classifies on."""
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


def unreachable() -> EndpointConnectionError:
    """A representative transport-level failure (endpoint unreachable), the
    ``BotoCoreError`` branch the runup classifier warns-and-continues on."""
    return EndpointConnectionError(endpoint_url="https://ec2.example")


@dataclass
class Controls:
    """Knobs the fakes read to drive each test's scenario."""

    # STS get_caller_identity: None passes; an Exception is raised.
    identity_error: Exception | None = None
    # describe_instances(InstanceIds=...) state name (status / native_transport).
    instance_state: str = "running"
    instance_public_ip: str = "203.0.113.10"
    # Whether a describe_instances(Filters=...) collision preflight finds one.
    collision: bool = False
    # describe_instance_types SupportedArchitectures for the arch cross-check.
    supported_archs: tuple[str, ...] = ("arm64",)
    # describe_subnets: the VpcId to report, or an Exception to raise.
    subnet_vpc_id: str = "vpc-abc"
    subnet_error: Exception | None = None
    # The default subnet resolved when no subnet is configured; no_default_subnet
    # models a default-VPC-less account (the lookup returns nothing).
    default_subnet_id: str = "subnet-default"
    default_subnet_az: str = "us-east-1a"
    no_default_subnet: bool = False
    # An explicit multi-subnet default-for-az listing (out of AZ order) for the
    # deterministic-pick test; overrides the single default_subnet_id when set.
    default_subnets: list[dict[str, str]] | None = None
    # SSM get_parameter value for the Debian AMI resolution.
    ssm_ami: str = "ami-ssm-debian"
    # delete_security_group: exceptions to raise on successive calls before it
    # finally succeeds (drives the DependencyViolation retry test).
    sg_delete_errors: list[Exception] = field(default_factory=list)


@dataclass
class Recorder:
    """What the fakes captured, for assertions."""

    sessions: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def methods(self, service: str | None = None) -> list[str]:
        return [m for s, m, _ in self.calls if service is None or s == service]

    def kwargs_for(self, method: str) -> dict[str, Any]:
        for _s, m, kw in self.calls:
            if m == method:
                return kw
        raise AssertionError(f"{method} was not called")

    def _cidrs(self, method: str) -> list[str]:
        out: list[str] = []
        for _s, m, kw in self.calls:
            if m != method:
                continue
            for perm in kw.get("IpPermissions", []):
                out.extend(r["CidrIp"] for r in perm.get("IpRanges", []))
        return out

    def authorized_cidrs(self) -> list[str]:
        return self._cidrs("authorize_security_group_ingress")

    def revoked_cidrs(self) -> list[str]:
        return self._cidrs("revoke_security_group_ingress")


class _FakeWaiter:
    def wait(self, **_kwargs: Any) -> None:
        return None


class _FakeEC2:
    def __init__(self, recorder: Recorder, controls: Controls, region: str) -> None:
        self._rec = recorder
        self._c = controls
        self._region = region
        self._sg_delete_attempts = 0
        # sg_id -> {cidr: rule_id}; the per-SG ingress the exposure tests read.
        self.ingress: dict[str, dict[str, str]] = {}
        self._next_rule = 0

    def _record(self, method: str, **kwargs: Any) -> None:
        self._rec.calls.append(("ec2", method, kwargs))

    def describe_instances(self, **kwargs: Any) -> dict[str, Any]:
        self._record("describe_instances", **kwargs)
        if "Filters" in kwargs:  # collision preflight
            if self._c.collision:
                return {"Reservations": [{"Instances": [{"InstanceId": "i-existing"}]}]}
            return {"Reservations": []}
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-123",
                            "State": {"Name": self._c.instance_state},
                            "PublicIpAddress": self._c.instance_public_ip,
                        }
                    ]
                }
            ]
        }

    def describe_instance_types(self, **kwargs: Any) -> dict[str, Any]:
        self._record("describe_instance_types", **kwargs)
        return {"InstanceTypes": [{"ProcessorInfo": {"SupportedArchitectures": list(self._c.supported_archs)}}]}

    def describe_subnets(self, **kwargs: Any) -> dict[str, Any]:
        self._record("describe_subnets", **kwargs)
        if self._c.subnet_error is not None:
            raise self._c.subnet_error
        if "Filters" in kwargs:  # default-for-az resolution (no subnet configured)
            if self._c.no_default_subnet:
                return {"Subnets": []}
            if self._c.default_subnets is not None:
                return {"Subnets": list(self._c.default_subnets)}
            return {
                "Subnets": [
                    {
                        "SubnetId": self._c.default_subnet_id,
                        "VpcId": self._c.subnet_vpc_id,
                        "AvailabilityZone": self._c.default_subnet_az,
                    }
                ]
            }
        subnet_id = kwargs.get("SubnetIds", ["subnet-configured"])[0]
        return {"Subnets": [{"SubnetId": subnet_id, "VpcId": self._c.subnet_vpc_id}]}

    def create_security_group(self, **kwargs: Any) -> dict[str, Any]:
        self._record("create_security_group", **kwargs)
        self.ingress.setdefault("sg-123", {})  # created with NO ingress
        return {"GroupId": "sg-123"}

    def authorize_security_group_ingress(self, **kwargs: Any) -> dict[str, Any]:
        self._record("authorize_security_group_ingress", **kwargs)
        ing = self.ingress.setdefault(kwargs["GroupId"], {})
        for perm in kwargs["IpPermissions"]:
            for ip_range in perm["IpRanges"]:
                cidr = ip_range["CidrIp"]
                if cidr in ing:
                    raise client_error("InvalidPermission.Duplicate", "exists", "AuthorizeSecurityGroupIngress")
                self._next_rule += 1
                ing[cidr] = f"sgr-{self._next_rule}"
        return {}

    def revoke_security_group_ingress(self, **kwargs: Any) -> dict[str, Any]:
        self._record("revoke_security_group_ingress", **kwargs)
        ing = self.ingress.setdefault(kwargs["GroupId"], {})
        if "SecurityGroupRuleIds" in kwargs:
            ids = set(kwargs["SecurityGroupRuleIds"])
            for cidr, rid in list(ing.items()):
                if rid in ids:
                    del ing[cidr]
            return {}
        for perm in kwargs["IpPermissions"]:
            for ip_range in perm["IpRanges"]:
                cidr = ip_range["CidrIp"]
                if cidr not in ing:
                    raise client_error("InvalidPermission.NotFound", "gone", "RevokeSecurityGroupIngress")
                del ing[cidr]
        return {}

    def describe_images(self, **kwargs: Any) -> dict[str, Any]:
        self._record("describe_images", **kwargs)
        return {"Images": [{"RootDeviceName": "/dev/xvda"}]}

    def run_instances(self, **kwargs: Any) -> dict[str, Any]:
        self._record("run_instances", **kwargs)
        return {"Instances": [{"InstanceId": "i-123"}]}

    def get_waiter(self, name: str) -> _FakeWaiter:
        self._record("get_waiter", name=name)
        return _FakeWaiter()

    def terminate_instances(self, **kwargs: Any) -> dict[str, Any]:
        self._record("terminate_instances", **kwargs)
        return {}

    def start_instances(self, **kwargs: Any) -> dict[str, Any]:
        self._record("start_instances", **kwargs)
        return {}

    def stop_instances(self, **kwargs: Any) -> dict[str, Any]:
        self._record("stop_instances", **kwargs)
        return {}

    def delete_security_group(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_security_group", **kwargs)
        idx = self._sg_delete_attempts
        self._sg_delete_attempts += 1
        if idx < len(self._c.sg_delete_errors):
            raise self._c.sg_delete_errors[idx]
        return {}


class _FakeSTS:
    def __init__(self, recorder: Recorder, controls: Controls) -> None:
        self._rec = recorder
        self._c = controls

    def get_caller_identity(self, **kwargs: Any) -> dict[str, Any]:
        self._rec.calls.append(("sts", "get_caller_identity", kwargs))
        if self._c.identity_error is not None:
            raise self._c.identity_error
        return {"Account": "111122223333", "Arn": "arn:aws:iam::111122223333:user/agw"}


class _FakeSSM:
    def __init__(self, recorder: Recorder, controls: Controls) -> None:
        self._rec = recorder
        self._c = controls

    def get_parameter(self, **kwargs: Any) -> dict[str, Any]:
        self._rec.calls.append(("ssm", "get_parameter", kwargs))
        return {"Parameter": {"Value": self._c.ssm_ami}}


class _FakeSession:
    def __init__(self, recorder: Recorder, controls: Controls, **kwargs: Any) -> None:
        self._rec = recorder
        self._c = controls
        recorder.sessions.append(kwargs)
        # One EC2 client per (session, region), so ingress state persists across
        # the platform's cached client lookups within a test.
        self._ec2: dict[str, _FakeEC2] = {}

    def client(self, service: str, region_name: str | None = None, **_kwargs: Any) -> Any:
        region = region_name or ""
        if service == "ec2":
            return self._ec2.setdefault(region, _FakeEC2(self._rec, self._c, region))
        if service == "sts":
            return _FakeSTS(self._rec, self._c)
        if service == "ssm":
            return _FakeSSM(self._rec, self._c)
        raise AssertionError(f"unexpected client service {service!r}")


def install_fakes(monkeypatch: pytest.MonkeyPatch, controls: Controls | None = None) -> Recorder:
    """Patch ``boto3.session.Session`` with the recording fake and return the
    ``Recorder``. ``controls`` drives the scenario (defaults to all
    happy-path)."""
    recorder = Recorder()
    resolved = controls if controls is not None else Controls()

    def _factory(**kwargs: Any) -> _FakeSession:
        return _FakeSession(recorder, resolved, **kwargs)

    monkeypatch.setattr("boto3.session.Session", _factory)
    return recorder


def stub_egress(monkeypatch: pytest.MonkeyPatch, ip: str = "203.0.113.9") -> str:
    """Stub the shared operator egress detection (and reset its per-process
    cache) so no test hits the network; returns the detected /32 prefix."""
    from agentworks.capabilities.vm_platform import ssh_exposure

    monkeypatch.setattr(ssh_exposure, "_egress_ip_cache", None)
    monkeypatch.setattr(ssh_exposure, "detect_egress_ip", lambda: ip)
    return f"{ip}/32"
