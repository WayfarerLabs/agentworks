"""EC2 create flow, exposure model, and power ops against the in-process boto3
fakes: the deny-baseline security group, the scoped ephemeral SSH allows and
their close hooks, the transient route, create rollback (failure and operator
interrupt), status mapping, and the idempotent delete.
"""

from __future__ import annotations

import gzip
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from agentworks.capabilities.base import RunContext
from agentworks.capabilities.vm_platform import ProvisionRequest
from agentworks.capabilities.vm_platform.tailscale_join import BootstrapCompletion, EphemeralTailscaleBootstrap
from agentworks.db import VMStatus
from agentworks.errors import ConfigError, StateError
from agentworks.plugins.aws.network import EC2Error, poke_ssh_allow, remove_ssh_allow
from agentworks.plugins.aws.platform import EC2Platform
from agentworks.ssh import SSHError
from agentworks.transports import SSHTransport
from agentworks.vms.initializer import driver as initializer_driver
from tests._aws_fakes import Controls, client_error, install_fakes, stub_egress, unreachable

if TYPE_CHECKING:
    from tests._aws_fakes import Recorder
    from tests.conftest import CapturedOutput

_DETECTED = "203.0.113.9"
_DETECTED_PREFIX = f"{_DETECTED}/32"
_SENTINEL = "tskey-aws-readiness-'sentinel"


def _assert_exception_graph_is_value_free(failure: BaseException) -> None:
    pending = [failure]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        assert _SENTINEL not in repr(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)


@pytest.fixture(autouse=True)
def _stub_egress_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs with the shared operator egress detection stubbed (never
    a live probe) and a clean per-process cache."""
    stub_egress(monkeypatch, _DETECTED)


def _platform(**extra: object) -> EC2Platform:
    """A platform on a site that selects the ambient credential chain
    explicitly; ``auth`` is required, so no site can leave it unsaid."""
    return EC2Platform("aws-site", {"region": "us-east-1", "auth": {"mode": "ambient"}, **extra})


def _request(
    *,
    cpus: int = 2,
    memory: int = 8,
    tailscale: str | None = None,
    disk: int = 50,
    swap: int = 4,
    ssh_key: str = "ssh-ed25519 AAAA test",
) -> ProvisionRequest:
    """A request in the shape the orchestrated path builds: every
    hardware field resolved, never None (the vm-template layer owns the
    defaults, and ``disk`` / ``swap`` here carry its values)."""
    return ProvisionRequest(
        vm_name="dev",
        hostname="dev",
        system_slug=None,
        admin_username="agw",
        ssh_public_key=ssh_key,
        ssh_private_key=None,
        tailscale_auth_key=tailscale,
        cpus=cpus,
        memory_gib=memory,
        disk_gib=disk,
        swap_gib=swap,
    )


def _config(allow_cidrs: list[str] | None = None) -> Any:
    """A config stand-in carrying just operator.ssh_allow_cidrs (the only
    operator field the exposure path reads for scope)."""
    return SimpleNamespace(operator=SimpleNamespace(ssh_allow_cidrs=allow_cidrs or []))


def _vm(**metadata: str) -> Any:
    base = {
        "instance_id": "i-123",
        "region": "us-east-1",
        "backend_name": "dev",
        "security_group_id": "sg-123",
        "bootstrap_ssh_prefixes": _DETECTED_PREFIX,
    }
    return SimpleNamespace(name="dev", admin_username="agw", platform_metadata={**base, **metadata})


def _ec2(platform: EC2Platform) -> Any:
    """The cached fake EC2 client for the platform's region, whose ``ingress``
    dict is the live per-SG ingress state the exposure tests read."""
    return platform._client("ec2", "us-east-1", RunContext())


class TestCreate:
    def test_provisions_and_records_metadata_without_public_ip_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The minimal (no-tailscale) create path launches an instance, creates
        a deny-baseline security group, and records only the identifiers later
        ops need. No Elastic IP / allocation metadata exists: the public IP is
        read live per use, never persisted."""
        rec = install_fakes(monkeypatch)
        result = _platform().create(_request(), RunContext(config=_config()))

        assert result.platform_metadata == {
            "instance_id": "i-123",
            "security_group_id": "sg-123",
            "region": "us-east-1",
            "backend_name": "dev",
            # Exactly the prefixes the bootstrap allow was poked with, so the
            # close hooks revoke those tuples and nothing else.
            "bootstrap_ssh_prefixes": _DETECTED_PREFIX,
        }
        assert "allocation_id" not in result.platform_metadata
        assert "allocate_address" not in rec.methods("ec2")

    def test_security_group_is_created_with_no_ingress(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A fresh EC2 security group IS the deny-all-inbound baseline, so
        create_security_group installs no ingress; the ONLY ingress that appears
        is the scoped bootstrap allow poked afterward (an authorize call, not a
        create-time rule)."""
        rec = install_fakes(monkeypatch)
        platform = _platform()
        platform.create(_request(), RunContext(config=_config()))

        # No ingress at group-create time; the ONLY ingress is the bootstrap
        # allow poked afterward (an authorize call), scoped to the detected IP.
        assert "authorize_security_group_ingress" in rec.methods("ec2")
        assert _ec2(platform).ingress["sg-123"] == {_DETECTED_PREFIX: "sgr-1"}

    def test_bootstrap_allow_scoped_to_detected_and_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The bootstrap poke is scoped to the detected operator egress plus the
        operator.ssh_allow_cidrs extras, never the world."""
        rec = install_fakes(monkeypatch)
        _platform().create(_request(), RunContext(config=_config(["198.51.100.0/24"])))

        assert rec.authorized_cidrs() == [_DETECTED_PREFIX, "198.51.100.0/24"]

    def test_launch_pins_a_permanent_auto_public_ip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The primary network interface pins AssociatePublicIpAddress=True (a
        permanent auto-assigned IP; exposure is the SG's job), with the security
        group and the resolved subnet moved INTO the interface block."""
        rec = install_fakes(monkeypatch)
        _platform(subnet_id="subnet-abc").create(_request(), RunContext(config=_config()))

        kw = rec.kwargs_for("run_instances")
        assert "SecurityGroupIds" not in kw
        assert "SubnetId" not in kw
        (nic,) = kw["NetworkInterfaces"]
        assert nic["AssociatePublicIpAddress"] is True
        assert nic["Groups"] == ["sg-123"]
        assert nic["SubnetId"] == "subnet-abc"

    def test_resolves_default_subnet_when_unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = install_fakes(monkeypatch, Controls(default_subnet_id="subnet-default"))
        _platform().create(_request(), RunContext(config=_config()))
        (nic,) = rec.kwargs_for("run_instances")["NetworkInterfaces"]
        assert nic["SubnetId"] == "subnet-default"
        assert rec.kwargs_for("create_security_group")["VpcId"] == "vpc-abc"

    def test_no_default_subnet_is_typed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fakes(monkeypatch, Controls(no_default_subnet=True))
        with pytest.raises(EC2Error, match="no default subnet") as exc:
            _platform().create(_request(), RunContext(config=_config()))
        assert exc.value.hint is not None and "subnet_id" in exc.value.hint

    def test_default_subnet_pick_is_deterministic_lowest_az(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The region-wide default-for-az listing is arbitrary-ordered, so the
        pick is made deterministic (lowest AZ) rather than taking the API's
        first, so repeated creates for a site land in the same AZ."""
        subnets = [
            {"SubnetId": "subnet-b", "VpcId": "vpc-1", "AvailabilityZone": "us-east-1b"},
            {"SubnetId": "subnet-a", "VpcId": "vpc-1", "AvailabilityZone": "us-east-1a"},
        ]
        rec = install_fakes(monkeypatch, Controls(default_subnets=subnets))
        _platform().create(_request(), RunContext(config=_config()))
        (nic,) = rec.kwargs_for("run_instances")["NetworkInterfaces"]
        assert nic["SubnetId"] == "subnet-a"

    def test_user_data_is_gzipped_and_under_cap_with_a_large_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The user-data is gzipped (cloud-init decompresses natively) and stays
        under EC2's 16 KiB raw cap even with an RSA-4096-sized key on the
        Tailscale path (the largest payload). The resolved key is absent from
        this payload and crosses the later stdin boundary instead."""
        rec = install_fakes(monkeypatch)
        monkeypatch.setattr(
            EphemeralTailscaleBootstrap,
            "complete",
            lambda self, auth_key: BootstrapCompletion(True, "100.64.0.5"),
        )
        rsa_key = "ssh-rsa " + ("A" * 716) + " agw@host"
        _platform().create(_request(tailscale="tskey-abc", ssh_key=rsa_key), RunContext(config=_config()))
        user_data = rec.kwargs_for("run_instances")["UserData"]
        assert isinstance(user_data, bytes)
        assert user_data[:2] == b"\x1f\x8b"  # gzip magic
        assert len(user_data) < 16384

    def test_user_data_over_cap_is_typed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The pre-launch guard raises a typed EC2Error naming the actual and
        max sizes rather than letting AWS reject the launch opaquely."""
        install_fakes(monkeypatch)
        monkeypatch.setattr("agentworks.plugins.aws.platform._MAX_USER_DATA_BYTES", 10)
        with pytest.raises(EC2Error, match="user-data") as exc:
            _platform().create(_request(), RunContext(config=_config()))
        assert "10-byte" in str(exc.value)

    def test_always_resolves_debian_ami_from_ssm_for_arch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SSM is the ONLY image source (no operator pin): the Debian bookworm
        release parameter for the selected arch is always resolved at create."""
        rec = install_fakes(monkeypatch, Controls(ssm_ami="ami-from-ssm"))
        _platform().create(_request(), RunContext(config=_config()))
        assert rec.kwargs_for("get_parameter")["Name"] == "/aws/service/debian/release/bookworm/latest/arm64"
        assert rec.kwargs_for("run_instances")["ImageId"] == "ami-from-ssm"

    def test_rejects_a_name_collision(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fakes(monkeypatch, Controls(collision=True))
        with pytest.raises(StateError, match="already exists"):
            _platform().create(_request(), RunContext(config=_config()))

    def test_collision_check_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The collision preflight fails CLOSED: a describe failure surfaces
        typed rather than being read as "no collision" (the agentworks:vm tag is
        not uniqueness-enforced)."""
        install_fakes(monkeypatch)
        monkeypatch.setattr(
            "tests._aws_fakes._FakeEC2.describe_instances",
            lambda self, **kw: (_ for _ in ()).throw(client_error("RequestLimitExceeded", "throttled", "Describe")),
        )
        with pytest.raises(EC2Error):
            _platform().create(_request(), RunContext(config=_config()))

    def test_arch_cross_check_rejects_a_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fakes(monkeypatch, Controls(supported_archs=("x86_64",)))
        with pytest.raises(ConfigError, match="t4g.large") as exc:
            _platform().create(_request(), RunContext(config=_config()))
        assert "arm64" in str(exc.value)

    def test_disk_request_sizes_the_root_volume(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = install_fakes(monkeypatch)
        _platform().create(_request(disk=40), RunContext(config=_config()))
        (bdm,) = rec.kwargs_for("run_instances")["BlockDeviceMappings"]
        assert bdm["DeviceName"] == "/dev/xvda"
        assert bdm["Ebs"]["VolumeSize"] == 40

    def test_every_create_sizes_the_root_volume(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """There is no unsized create: ``disk_gib`` is resolved by the
        vm-template layer, so the AMI's own root size never stands. This
        replaces a test that pinned the opposite, for a None branch the
        request shape no longer admits."""
        rec = install_fakes(monkeypatch)
        _platform().create(_request(), RunContext(config=_config()))
        (bdm,) = rec.kwargs_for("run_instances")["BlockDeviceMappings"]
        assert bdm["Ebs"]["VolumeSize"] == 50

    def test_disk_describe_images_failure_is_typed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fakes(monkeypatch)
        monkeypatch.setattr(
            "tests._aws_fakes._FakeEC2.describe_images",
            lambda self, **kw: (_ for _ in ()).throw(client_error("UnauthorizedOperation", "denied", "DescribeImages")),
        )
        with pytest.raises(EC2Error, match="size the disk") as exc:
            _platform().create(_request(disk=40), RunContext(config=_config()))
        assert exc.value.hint is not None and "ec2:DescribeImages" in exc.value.hint

    def test_tailscale_path_waits_for_bootstrap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fakes(monkeypatch)
        monkeypatch.setattr(
            EphemeralTailscaleBootstrap,
            "complete",
            lambda self, auth_key: BootstrapCompletion(True, "100.64.0.5"),
        )
        result = _platform().create(_request(tailscale="tskey-abc"), RunContext(config=_config()))
        assert result.bootstrap_complete is True
        assert result.tailscale_ip == "100.64.0.5"


class TestCreateRollback:
    def test_rolls_back_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A failure after the instance launches (here the bootstrap poke)
        sweeps the partial work: terminate the instance, delete the security
        group. The original failure propagates wrapped."""
        rec = install_fakes(monkeypatch)
        monkeypatch.setattr(
            "tests._aws_fakes._FakeEC2.authorize_security_group_ingress",
            lambda self, **kw: (_ for _ in ()).throw(
                client_error("RulesPerSecurityGroupLimitExceeded", "full", "Auth")
            ),
        )
        with pytest.raises(EC2Error):
            _platform().create(_request(), RunContext(config=_config()))
        methods = rec.methods("ec2")
        assert "terminate_instances" in methods
        assert "delete_security_group" in methods

    def test_non_ssherror_after_launch_rolls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-SSHError escaping the post-launch region (here a raw OSError
        from the inline bootstrap wait, e.g. a missing ssh binary) is inside the
        guarded region now, so it sweeps the partial instance + SG instead of
        leaking them past both rollback arms."""
        rec = install_fakes(monkeypatch)
        monkeypatch.setattr(
            EphemeralTailscaleBootstrap,
            "complete",
            lambda self, auth_key: (_ for _ in ()).throw(OSError("ssh: command not found")),
        )
        with pytest.raises(EC2Error):
            _platform().create(_request(tailscale="tskey-abc"), RunContext(config=_config()))
        methods = rec.methods("ec2")
        assert "terminate_instances" in methods
        assert "delete_security_group" in methods

    @pytest.mark.parametrize(
        ("failure_command", "message", "expected_commands"),
        [
            ("echo ok", "SSH did not become ready", ["echo ok"] * 30),
            (
                "cloud-init status --wait",
                "cloud-init did not complete",
                ["echo ok", "cloud-init status --wait"],
            ),
        ],
        ids=("ssh-readiness-exhaustion", "cloud-init-wait-failure"),
    )
    def test_readiness_failure_is_safe_and_rolls_back_before_key_delivery(
        self,
        monkeypatch: pytest.MonkeyPatch,
        captured_output: CapturedOutput,
        failure_command: str,
        message: str,
        expected_commands: list[str],
    ) -> None:
        """Provider-retained user-data stays credential-free and readiness
        failures remove the instance and security group without stdin join or
        generated-script staging."""
        rec = install_fakes(monkeypatch)
        calls: list[tuple[str, dict[str, object]]] = []

        def _readiness_failure(self: SSHTransport, command: str, **kwargs: object) -> object:
            calls.append((command, kwargs))
            if command == failure_command:
                raise SSHError(f"safe failure for {command}")
            return SimpleNamespace(stdout="", returncode=0)

        monkeypatch.setattr(SSHTransport, "run", _readiness_failure)
        monkeypatch.setattr("agentworks.capabilities.vm_platform.tailscale_join.time.sleep", lambda _seconds: None)

        with pytest.raises(EC2Error, match=message) as caught:
            _platform().create(_request(tailscale=_SENTINEL), RunContext(config=_config()))

        assert [command for command, _kwargs in calls] == expected_commands
        assert all("input_text" not in kwargs for _command, kwargs in calls)
        assert not any("agentworks-bootstrap" in command for command, _kwargs in calls)
        retained_user_data = gzip.decompress(rec.kwargs_for("run_instances")["UserData"]).decode()
        assert _SENTINEL not in retained_user_data
        assert _SENTINEL not in repr(calls)
        methods = rec.methods("ec2")
        assert "terminate_instances" in methods
        assert "delete_security_group" in methods
        _assert_exception_graph_is_value_free(caught.value)
        assert _SENTINEL not in repr(captured_output.lines)

    @pytest.mark.parametrize(
        ("failure_command", "expected_commands"),
        [
            ("echo ok", ["echo ok"]),
            ("cloud-init status --wait", ["echo ok", "cloud-init status --wait"]),
        ],
        ids=("ssh-readiness", "cloud-init-wait"),
    )
    def test_readiness_interrupt_rolls_back_before_key_delivery_or_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        captured_output: CapturedOutput,
        failure_command: str,
        expected_commands: list[str],
    ) -> None:
        rec = install_fakes(monkeypatch)
        interrupt = KeyboardInterrupt(f"interrupted during {failure_command}")
        calls: list[tuple[str, dict[str, object]]] = []

        def _interrupt_readiness(self: SSHTransport, command: str, **kwargs: object) -> object:
            calls.append((command, kwargs))
            if command == failure_command:
                raise interrupt
            return SimpleNamespace(stdout="", returncode=0)

        monkeypatch.setattr(SSHTransport, "run", _interrupt_readiness)
        fallback = MagicMock(side_effect=AssertionError("Phase A fallback reached"))
        monkeypatch.setattr(initializer_driver, "_run_bootstrap_script", fallback)

        with pytest.raises(KeyboardInterrupt) as caught:
            _platform().create(_request(tailscale=_SENTINEL), RunContext(config=_config()))

        assert caught.value is interrupt
        assert [command for command, _kwargs in calls] == expected_commands
        assert all("input_text" not in kwargs for _command, kwargs in calls)
        assert not any("agentworks-bootstrap" in command for command, _kwargs in calls)
        fallback.assert_not_called()
        retained_user_data = gzip.decompress(rec.kwargs_for("run_instances")["UserData"]).decode()
        assert _SENTINEL not in retained_user_data
        assert _SENTINEL not in repr(calls)
        assert _SENTINEL not in repr(captured_output.lines)
        assert rec.kwargs_for("terminate_instances") == {"InstanceIds": ["i-123"]}
        assert rec.kwargs_for("delete_security_group") == {"GroupId": "sg-123"}

    def test_rolls_back_on_keyboard_interrupt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An operator Ctrl-C during the inline bootstrap wait rolls back the
        partial resource set (terminate + delete SG) and re-raises the
        interrupt for the caller's row unwind."""
        rec = install_fakes(monkeypatch)

        interrupt = KeyboardInterrupt("first")

        def _interrupt(self: SSHTransport, command: str, **kwargs: object) -> object:
            del self, command, kwargs
            raise interrupt

        monkeypatch.setattr(SSHTransport, "run", _interrupt)
        with pytest.raises(KeyboardInterrupt) as caught:
            _platform().create(_request(tailscale="tskey-abc"), RunContext(config=_config()))
        assert caught.value is interrupt
        assert rec.kwargs_for("terminate_instances") == {"InstanceIds": ["i-123"]}
        assert rec.kwargs_for("delete_security_group") == {"GroupId": "sg-123"}

    def test_second_interrupt_abandons_cleanup_loudly(
        self, monkeypatch: pytest.MonkeyPatch, captured_output: CapturedOutput
    ) -> None:
        """A second Ctrl-C during the rollback abandons it (naming the region
        and tag for manual cleanup) instead of wedging; the ORIGINAL interrupt
        still propagates."""
        install_fakes(monkeypatch)
        interrupt = KeyboardInterrupt("first")

        def _interrupt_readiness(self: SSHTransport, command: str, **kwargs: object) -> object:
            del self, command, kwargs
            raise interrupt

        cleanup_calls: list[dict[str, object]] = []

        def _interrupt_cleanup(self: object, **kwargs: object) -> object:
            del self
            cleanup_calls.append(kwargs)
            raise KeyboardInterrupt("second")

        monkeypatch.setattr(SSHTransport, "run", _interrupt_readiness)
        monkeypatch.setattr("tests._aws_fakes._FakeEC2.terminate_instances", _interrupt_cleanup)
        with pytest.raises(KeyboardInterrupt) as caught:
            _platform().create(_request(tailscale="tskey-abc"), RunContext(config=_config()))
        assert caught.value is interrupt
        assert cleanup_calls == [{"InstanceIds": ["i-123"]}]
        abandoned = [warning for warning in captured_output.warnings if "Cleanup abandoned" in warning]
        assert abandoned == [
            "Cleanup abandoned: EC2 resources tagged 'agentworks:vm=dev' may remain in region "
            "'us-east-1'; terminate the instance and delete its security group there manually."
        ]


class TestCloseProvisioningHooks:
    def test_post_tailscale_ready_revokes_the_bootstrap_tuples(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Once Tailscale is up the bootstrap hole closes: exactly the recorded
        bootstrap prefixes are revoked, restoring the deny baseline."""
        install_fakes(monkeypatch)
        platform = _platform()
        platform.create(_request(), RunContext(config=_config()))
        assert _ec2(platform).ingress["sg-123"] == {_DETECTED_PREFIX: "sgr-1"}

        platform.post_tailscale_ready(_vm(), RunContext())
        assert _ec2(platform).ingress["sg-123"] == {}

    def test_secure_failed_vm_revokes_the_bootstrap_tuples(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fakes(monkeypatch)
        platform = _platform()
        platform.create(_request(), RunContext(config=_config()))

        platform.secure_failed_vm(_vm(), RunContext())
        assert _ec2(platform).ingress["sg-123"] == {}

    def test_close_leaves_a_distinct_concurrent_route_intact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The close revokes ONLY the recorded bootstrap tuple, so a concurrent
        vm-shell route's distinct allow (nothing serializes commands per VM)
        survives, where a blanket revoke-all would have swept it out."""
        install_fakes(monkeypatch)
        platform = _platform()
        platform.create(_request(), RunContext(config=_config()))  # pokes the bootstrap tuple
        ec2 = _ec2(platform)
        # A concurrent native route from a DIFFERENT egress pokes a distinct tuple.
        poke_ssh_allow(ec2, "sg-123", ["198.51.100.7/32"])
        assert set(ec2.ingress["sg-123"]) == {_DETECTED_PREFIX, "198.51.100.7/32"}

        platform.post_tailscale_ready(_vm(), RunContext())
        assert set(ec2.ingress["sg-123"]) == {"198.51.100.7/32"}

    def test_hook_tolerates_missing_security_group(self, monkeypatch: pytest.MonkeyPatch, captured_output: Any) -> None:
        """A row with no recorded security group is warned and skipped, not an
        error (the hooks are best-effort)."""
        install_fakes(monkeypatch)
        vm = SimpleNamespace(name="dev", admin_username="agw", platform_metadata={"instance_id": "i-123"})
        _platform().post_tailscale_ready(vm, RunContext())  # no raise
        assert any("no EC2 security_group_id" in w for w in captured_output.warnings)


class TestTransientRoute:
    def test_pokes_on_enter_and_removes_on_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fakes(monkeypatch)
        platform = _platform()
        with platform.transient_route(_vm(), RunContext(), config=_config(["198.51.100.0/24"])):
            assert _ec2(platform).ingress["sg-123"] == {_DETECTED_PREFIX: "sgr-1", "198.51.100.0/24": "sgr-2"}
        # Exit revoked exactly what it poked.
        assert _ec2(platform).ingress["sg-123"] == {}

    def test_removes_on_body_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fakes(monkeypatch)
        platform = _platform()
        with pytest.raises(RuntimeError), platform.transient_route(_vm(), RunContext(), config=_config()):
            raise RuntimeError("boom")
        assert _ec2(platform).ingress["sg-123"] == {}

    def test_partial_poke_failure_is_still_swept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A poke that fails on the second prefix still has the first prefix
        revoked by the finally (the poke sits inside the guarded region)."""
        install_fakes(monkeypatch)
        platform = _platform()

        real_authorize = platform._client("ec2", "us-east-1", RunContext()).authorize_security_group_ingress

        def _authorize(**kw: Any) -> Any:
            cidr = kw["IpPermissions"][0]["IpRanges"][0]["CidrIp"]
            if cidr == "198.51.100.0/24":
                raise client_error("RulesPerSecurityGroupLimitExceeded", "full", "Authorize")
            return real_authorize(**kw)

        monkeypatch.setattr(_ec2(platform), "authorize_security_group_ingress", _authorize)
        route = platform.transient_route(_vm(), RunContext(), config=_config(["198.51.100.0/24"]))
        with pytest.raises(EC2Error), route:
            pass
        # The detected prefix that WAS authorized got revoked on the way out.
        assert _ec2(platform).ingress["sg-123"] == {}

    def test_requires_a_security_group_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fakes(monkeypatch)
        vm = SimpleNamespace(name="dev", admin_username="agw", platform_metadata={"instance_id": "i-123"})
        route = _platform().transient_route(vm, RunContext(), config=_config())
        with pytest.raises(StateError, match="security_group_id"), route:
            pass


class TestSharedTupleConcurrency:
    """EC2 keys an ingress rule by its (protocol, port, cidr) tuple, not a name
    or description, so two routes from one operator egress share one rule. The
    poke is idempotent (tolerate Duplicate) and the remove tolerant (tolerate
    NotFound), failing CLOSED rather than open."""

    def test_poke_is_idempotent_on_a_duplicate_tuple(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fakes(monkeypatch)
        platform = _platform()
        ec2 = _ec2(platform)
        poke_ssh_allow(ec2, "sg-123", [_DETECTED_PREFIX])
        poke_ssh_allow(ec2, "sg-123", [_DETECTED_PREFIX])  # duplicate: no error
        assert ec2.ingress["sg-123"] == {_DETECTED_PREFIX: "sgr-1"}

    def test_remove_tolerates_an_already_gone_tuple(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fakes(monkeypatch)
        platform = _platform()
        ec2 = _ec2(platform)
        poke_ssh_allow(ec2, "sg-123", [_DETECTED_PREFIX])
        remove_ssh_allow(ec2, "sg-123", [_DETECTED_PREFIX])
        remove_ssh_allow(ec2, "sg-123", [_DETECTED_PREFIX])  # already gone: no error
        assert ec2.ingress["sg-123"] == {}


class TestPowerOps:
    def test_start_calls_start_instances(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = install_fakes(monkeypatch)
        _platform().start(_vm(), RunContext())  # type: ignore[arg-type]
        assert rec.kwargs_for("start_instances")["InstanceIds"] == ["i-123"]

    def test_stop_calls_stop_instances(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = install_fakes(monkeypatch)
        _platform().stop(_vm(), RunContext())  # type: ignore[arg-type]
        assert rec.kwargs_for("stop_instances")["InstanceIds"] == ["i-123"]

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            ("running", VMStatus.RUNNING),
            ("stopping", VMStatus.STOPPED),
            ("stopped", VMStatus.STOPPED),
            ("pending", VMStatus.UNKNOWN),
            ("shutting-down", VMStatus.UNKNOWN),
            ("terminated", VMStatus.UNKNOWN),
        ],
    )
    def test_status_mapping(self, monkeypatch: pytest.MonkeyPatch, state: str, expected: VMStatus) -> None:
        install_fakes(monkeypatch, Controls(instance_state=state))
        assert _platform().status(_vm(), RunContext()) is expected  # type: ignore[arg-type]

    def test_status_raises_typed_on_auth_rejection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A definitive credential rejection during status re-raises typed
        rather than degrading to UNKNOWN (the azure #303 hole)."""
        install_fakes(monkeypatch)
        monkeypatch.setattr(
            "tests._aws_fakes._FakeEC2.describe_instances",
            lambda self, **kw: (_ for _ in ()).throw(client_error("AuthFailure", "denied", "DescribeInstances")),
        )
        with pytest.raises(EC2Error):
            _platform().status(_vm(), RunContext())  # type: ignore[arg-type]

    def test_status_unknown_on_unreachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fakes(monkeypatch)
        monkeypatch.setattr(
            "tests._aws_fakes._FakeEC2.describe_instances",
            lambda self, **kw: (_ for _ in ()).throw(unreachable()),
        )
        assert _platform().status(_vm(), RunContext()) is VMStatus.UNKNOWN  # type: ignore[arg-type]

    def test_status_unknown_without_instance_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fakes(monkeypatch)
        vm = SimpleNamespace(name="dev", admin_username="agw", platform_metadata={})
        assert _platform().status(vm, RunContext()) is VMStatus.UNKNOWN  # type: ignore[arg-type]

    def test_display_backend_name_is_instance_at_region(self) -> None:
        assert _platform().display_backend_name(_vm()) == "i-123@us-east-1"  # type: ignore[arg-type]

    def test_native_transport_reads_public_ip_live(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fakes(monkeypatch, Controls(instance_public_ip="203.0.113.55"))
        transport = _platform().native_transport(_vm(), RunContext())  # type: ignore[arg-type]
        assert transport is not None
        assert getattr(transport, "host", None) == "203.0.113.55"


class TestDelete:
    def _sg_deleted(self, rec: Recorder) -> int:
        return rec.methods("ec2").count("delete_security_group")

    def test_terminates_and_deletes_the_security_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = install_fakes(monkeypatch)
        _platform().delete(_vm(), RunContext())  # type: ignore[arg-type]
        methods = rec.methods("ec2")
        assert "terminate_instances" in methods
        assert "delete_security_group" in methods

    def test_is_idempotent_when_already_gone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        install_fakes(monkeypatch)
        monkeypatch.setattr(
            "tests._aws_fakes._FakeEC2.terminate_instances",
            lambda self, **kw: (_ for _ in ()).throw(client_error("InvalidInstanceID.NotFound", "gone", "Terminate")),
        )
        monkeypatch.setattr(
            "tests._aws_fakes._FakeEC2.delete_security_group",
            lambda self, **kw: (_ for _ in ()).throw(client_error("InvalidGroup.NotFound", "gone", "DeleteSg")),
        )
        _platform().delete(_vm(), RunContext())  # type: ignore[arg-type]  # no raise

    def test_already_gone_terminate_is_silent(self, monkeypatch: pytest.MonkeyPatch, captured_output: Any) -> None:
        """An already-terminated instance is silent success: no misleading warn
        (only UNEXPECTED terminate failures warn)."""
        install_fakes(monkeypatch)
        monkeypatch.setattr(
            "tests._aws_fakes._FakeEC2.terminate_instances",
            lambda self, **kw: (_ for _ in ()).throw(client_error("InvalidInstanceID.NotFound", "gone", "Terminate")),
        )
        _platform().delete(_vm(), RunContext())  # type: ignore[arg-type]
        assert not any("terminate" in w for w in captured_output.warnings)

    def test_unexpected_terminate_failure_warns(self, monkeypatch: pytest.MonkeyPatch, captured_output: Any) -> None:
        """A real terminate failure (AuthFailure) warns loudly rather than being
        swallowed and misattributed to the later security-group cleanup."""
        install_fakes(monkeypatch)
        monkeypatch.setattr(
            "tests._aws_fakes._FakeEC2.terminate_instances",
            lambda self, **kw: (_ for _ in ()).throw(client_error("AuthFailure", "denied", "Terminate")),
        )
        _platform().delete(_vm(), RunContext())  # type: ignore[arg-type]
        assert any("could not terminate instance" in w for w in captured_output.warnings)

    def test_retries_sg_through_dependency_violation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("agentworks.plugins.aws.network.time.sleep", lambda _s: None)
        rec = install_fakes(
            monkeypatch,
            Controls(sg_delete_errors=[client_error("DependencyViolation", "eni attached", "DeleteSg")]),
        )
        _platform().delete(_vm(), RunContext())  # type: ignore[arg-type]
        assert self._sg_deleted(rec) == 2  # one DependencyViolation, then success

    def test_without_instance_id_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = install_fakes(monkeypatch)
        vm = SimpleNamespace(name="dev", admin_username="agw", platform_metadata={})
        _platform().delete(vm, RunContext())  # type: ignore[arg-type]
        assert rec.methods("ec2") == []
