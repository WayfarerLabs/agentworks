"""The EC2 VM platform: creates and manages VMs via the AWS SDK (boto3).

This is the second cloud platform, and it copies the azure-vm reference shape
deliberately (see ``cli/agentworks/capabilities/vm_platform/README.md``): an
required ``auth`` tagged union whose credential arm names a secret, an
instance-type catalog
with smallest-that-fits selection, and the baseline-deny / ephemeral-scoped-allow
exposure model azure's redesign established (a security
group that denies all inbound by default; SSH via ephemeral rules scoped to the
operator's egress prefixes; ``post_tailscale_ready`` / ``secure_failed_vm`` /
``transient_route`` close hooks; create rollback on both failure and operator
interrupt). Two deliberate divergences from azure are documented where they
live: ``runup`` / ``status`` classify credential rejection, authorization
denial, and an unreachable endpoint separately, and the security-group
mechanics in ``network.py`` note the SG-natural-deny asymmetry and the
shared-tuple concurrency behavior EC2's rule model forces.
"""

from __future__ import annotations

import contextlib
import gzip
from typing import TYPE_CHECKING, Any, ClassVar

from agentworks import output
from agentworks.capabilities.vm_platform.base import ProvisionRequest, ProvisionResult, VMPlatform
from agentworks.capabilities.vm_platform.bootstrap_script import generate_bootstrap_script
from agentworks.capabilities.vm_platform.cloud_init import PROVISIONING_PACKAGES
from agentworks.capabilities.vm_platform.debian_release import (
    code_owned_release_value,
)
from agentworks.capabilities.vm_platform.ssh_exposure import config_allow_cidrs, operator_ssh_prefixes
from agentworks.capabilities.vm_platform.tailscale_join import EphemeralTailscaleBootstrap
from agentworks.db import VMStatus
from agentworks.debian import DebianRelease
from agentworks.errors import AuthorizationError, ConfigError, NotFoundError, StateError, TokenRejectedError
from agentworks.plugins.aws.auth import _build_access_key_session, _build_ambient_session
from agentworks.plugins.aws.config import (
    AwsAmbientAuth,
    AwsEC2Config,
    _generate_ec2_user_data,
    _instance_catalog,
    _InstanceType,
    _select_instance_type,
)

# The network-resource plumbing (the security-group exposure mechanics, the
# cleanup / rollback sweeps) and the shared SDK-error wrapper live in the
# sibling network module; this module keeps the VMPlatform capability surface.
from agentworks.plugins.aws.network import (
    SUBNET_NOT_FOUND_CODES,
    EC2Error,
    cleanup_partial,
    create_security_group,
    error_code,
    first_instance_public_ip,
    first_instance_state,
    is_authorization_denial,
    is_credential_rejection,
    poke_ssh_allow,
    remove_ssh_allow,
    rollback_create_on_interrupt,
    terminate_and_cleanup_strict,
    vm_tag,
    wrap_ec2_error,
)
from agentworks.topics import TopicProse
from agentworks.transports import SSHTransport

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from agentworks.capabilities.base import RunContext
    from agentworks.config import Config
    from agentworks.db import VMRow
    from agentworks.transports import Transport


# Debian's image naming for the EC2 architecture vocabulary. The mapping
# stays internal: operators write the EC2 spelling in the catalog and
# never see Debian's ``amd64``.
_DEBIAN_ARCH_SEGMENT = {"x86_64": "amd64", "arm64": "arm64"}
_DEBIAN_SSM_RELEASES: dict[DebianRelease, str] = {
    DebianRelease.TRIXIE: "13",
}

# EC2 caps RunInstances user-data at 16 KiB in RAW form (before boto3's base64).
# The payload is gzipped (cloud-init decompresses natively), so this bounds the
# COMPRESSED bytes; a pre-launch guard raises typed if even that is over.
_MAX_USER_DATA_BYTES = 16384


class EC2Platform(VMPlatform):
    """Runs VMs on Amazon EC2 via the AWS Python SDK (boto3). Named ``aws-ec2``,
    not ``aws``: the capability is one specific AWS service, and other AWS
    services could plausibly back platforms of their own someday (the same
    one-service rationale ``azure-vm`` follows for Azure)."""

    contract_version: ClassVar[int] = 1
    name: ClassVar[str] = "aws-ec2"
    description: ClassVar[str] = "Amazon EC2 instances (region + optional VPC subnet)"
    config_model: ClassVar[type[AwsEC2Config]] = AwsEC2Config
    prose: ClassVar[TopicProse | None] = TopicProse(
        title="Amazon EC2",
        overview="""
        Launches EC2 instances in one region. Declare one vm-site per region you target.
        Omit the subnet to use the region's default VPC; naming one puts instances there
        and creates the security group in its VPC.

        The image is the concrete Debian release requested by core for the instance
        type's architecture, resolved from the public SSM release parameter at create
        time; there is no image override or platform-local default. Instance types come from a built-in Graviton
        catalog unless the site overrides it, and `vm create` picks the smallest entry
        that satisfies the vm-template's request.

        An instance keeps a permanent public IP. SSH exposure is not permanent: the
        security group denies all inbound except ephemeral rules scoped to your detected
        egress IP (plus `operator.ssh_allow_cidrs`) during bootstrap and each native
        route.

        `auth` says how the site authenticates and defaults to `{mode: ambient}`, the
        ambient AWS credential chain (environment, shared config, instance profile,
        SSO), which is what boto3 does when told nothing. `auth: {mode: access-key,
        ...}` replaces that chain for this site; add `assume_role_arn` to layer an STS
        AssumeRole on top.

        EC2 retains credential-free UserData. After cloud-init is ready, creation sends
        the required Tailscale key through one fixed SSH command on stdin and returns
        only after join succeeds. Failure terminates the new instance and security group.

        Ships as the opt-in `aws` system plugin, so a site stays not-ready until
        `[plugins] system` lists it.
        """,
    )

    # Warned by the transports factory when every reachability probe fails: the
    # ephemeral SSH allow is scoped to the DETECTED egress IP, so an operator
    # whose SSH traffic leaves through a different address (VPN split tunnel,
    # proxy, CGNAT) gets a hole that does not match and every probe times out.
    probe_failure_hint: ClassVar[str | None] = (
        "The transient EC2 SSH allow is scoped to your detected public IP; "
        "if your SSH traffic egresses elsewhere (VPN split tunnel, proxy, "
        "CGNAT), add your address(es) to operator.ssh_allow_cidrs in your "
        "agentworks config."
    )

    def __init__(self, owner_name: str, config: Mapping[str, object]) -> None:
        super().__init__(owner_name, config)
        # The boto3 session (credentials) and per-(service, region) clients,
        # built on FIRST need by the accessors below and reused for the
        # instance's remaining ops. The session's identity is fixed by the
        # bound config (whichever arm the site's auth.mode names), so it caches
        # once per instance. Clients key by
        # (service, region): the config names one region, but power ops read
        # each VM's stored region, and rows created under an older region must
        # keep operating regardless of what the config says today.
        self._session_cached: Any | None = None
        self._clients: dict[tuple[str, str], Any] = {}

    # No preflight override, on either credential path, for the same reasons
    # spelled out on azure's platform: whether a declared secret resolves is
    # the operation sweep's prediction, not this class's, and a credential
    # probe before the resolve stage would fork behavior on where the
    # credential comes from. Credential and reachability failures surface at
    # runup and at the op with typed errors.

    @property
    def config(self) -> AwsEC2Config:
        """This site's validated aws-ec2 config."""
        return self._config_as(AwsEC2Config)

    # legacy_platform_metadata: no pre-v27 EC2 rows ever existed (this platform
    # ships after the migration), so the base no-op return of {} is correct.

    def _get_session(self, ctx: RunContext) -> Any:
        """The boto3 session, built on first need and reused for the instance's
        remaining ops.

        Which credential is a config decision, not a runtime one, and the site
        states it outright: ``auth.mode`` selects the arm. An ``access-key``
        site gets exactly that credential, built from the secret access key
        ``ctx.secret`` delivers (then STS AssumeRole when ``assume_role_arn``
        is set), and NEVER falls back to the ambient chain if it fails; an
        ``ambient`` site gets boto3's default chain (env vars, shared config,
        instance profile, SSO). Falling back would authenticate as a different
        identity than the operator configured, which is worse than failing.

        Caching stays correct under the fork because the fork's inputs are
        fixed per instance: the mode, the access key id, and the secret NAME
        come from the bound config, so a given instance resolves the same
        session every time. See :func:`_build_access_key_session`.
        """
        session = self._session_cached
        if session is None:
            auth = self.config.auth
            region = self.config.region
            if isinstance(auth, AwsAmbientAuth):
                session = _build_ambient_session(region)
            else:
                session = _build_access_key_session(auth, ctx.secret(auth.access_key_secret), self.site_name, region)
            self._session_cached = session
        return session

    def _client(self, service: str, region: str, ctx: RunContext) -> Any:
        """The ``service`` client for ``region``, built on first need from the
        cached session and reused for the instance's remaining ops against that
        (service, region). Client construction is inert (no network), so the
        credential's only live cost is the session build in :meth:`_get_session`,
        which sits here OUTSIDE every op's error-wrapping try: a typed
        credential failure is already the answer and re-wrapping it would strip
        the site-and-secret hint (worse, a ``status`` that degrades to UNKNOWN
        on any exception would swallow it entirely)."""
        key = (service, region)
        client = self._clients.get(key)
        if client is None:
            client = self._get_session(ctx).client(service, region_name=region)
            self._clients[key] = client
        return client

    def runup(self, ctx: RunContext) -> None:
        """Provisioning-phase runup: an authenticated, read-only check before
        ``create`` mutates anything. It probes the credential with STS
        ``GetCallerIdentity`` and, when a subnet is configured, confirms the
        subnet exists in the region (analogous to azure's resource-group
        existence check).

        This is where BOTH credential paths first pay: building the client
        resolves the session, so on the explicit path a bad or empty secret, or
        a failed AssumeRole, aborts ``vm create`` here with a typed, secret-free
        error before any DB row or AWS resource exists.

        The classification DELIBERATELY differs from azure's fatal-only stance,
        and it can because botocore distinguishes the cases azure cannot.
        azure-identity collapses a definitive Entra rejection and an unreachable
        Entra into one ``ClientAuthenticationError``, so azure must treat every
        credential failure as fatal. botocore instead answers a real rejection
        with a ``ClientError`` carrying an auth error code (``AuthFailure``,
        ``InvalidClientTokenId``, ...) and an unreachable endpoint with an
        ``EndpointConnectionError`` and friends, so EC2 follows proxmox: a
        definitive credential rejection is fatal (a ``TokenRejectedError``
        before any VM exists), an authorization denial is a distinct
        ``AuthorizationError``, and a transient or unreachable failure warns
        and continues unverified.
        """
        from botocore.exceptions import BotoCoreError, ClientError

        region = self.config.region
        subnet_id = self.config.subnet_id
        auth = self.config.auth
        if isinstance(auth, AwsAmbientAuth):
            cred_label = "the ambient AWS credentials (env, shared config, instance profile, SSO)"
            cred_hint = "check the credentials in the active AWS credential chain"
        else:
            cred_label = f"the AWS credentials for access key {auth.access_key_id} (secret '{auth.access_key_secret}')"
            cred_hint = f"check auth.access_key_id and the value / permissions of the '{auth.access_key_secret}' secret"

        output.info(f"Performing runup test for vm-site/{self.site_name}...")
        # Client build sits OUTSIDE the try: it is where the credential is
        # resolved, and its typed failures (an empty resolved secret, a failed
        # AssumeRole) are already the answer.
        sts = self._client("sts", region, ctx)
        ec2 = self._client("ec2", region, ctx)

        def _reject_or_warn(exc: ClientError) -> None:
            """Raise definitive credential and permission failures."""
            if is_credential_rejection(exc):
                raise TokenRejectedError(
                    f"AWS rejected {cred_label} for vm-site '{self.site_name}' ({error_code(exc)})",
                    entity_kind="vm-site",
                    entity_name=self.site_name,
                    hint=cred_hint,
                ) from exc
            if is_authorization_denial(exc):
                raise AuthorizationError(
                    f"AWS denied {cred_label} permission for vm-site '{self.site_name}' ({error_code(exc)})",
                    entity_kind="vm-site",
                    entity_name=self.site_name,
                    hint=cred_hint,
                ) from exc
            output.warn(f"could not verify {cred_label} for '{self.site_name}' ({exc}); continuing unverified")

        # 1. Identity probe (validates the credential on both paths).
        try:
            sts.get_caller_identity()
        except ClientError as exc:
            _reject_or_warn(exc)
            return  # warned: nothing more to verify unverified
        except BotoCoreError as exc:
            output.warn(f"could not reach AWS for '{self.site_name}' ({exc}); continuing unverified")
            return

        # 2. A configured subnet that does not exist in the region is a
        # definitive misconfiguration (fatal), like azure's missing group.
        if isinstance(subnet_id, str) and subnet_id:
            try:
                ec2.describe_subnets(SubnetIds=[subnet_id])
            except ClientError as exc:
                if error_code(exc) in SUBNET_NOT_FOUND_CODES:
                    raise NotFoundError(
                        f"EC2 subnet '{subnet_id}' does not exist in region '{region}' (vm-site '{self.site_name}')",
                        entity_kind="subnet",
                        entity_name=subnet_id,
                        hint=(
                            f"create the subnet or point vm-site '{self.site_name}' at an existing subnet_id "
                            f"in region {region}"
                        ),
                    ) from exc
                _reject_or_warn(exc)
            except BotoCoreError as exc:
                output.warn(f"could not reach AWS for '{self.site_name}' ({exc}); continuing unverified")

    def create(self, request: ProvisionRequest, ctx: RunContext) -> ProvisionResult:
        region = self.config.region
        subnet_id = self.config.subnet_id
        ssm_release = code_owned_release_value(
            _DEBIAN_SSM_RELEASES,
            request.debian_release,
            platform_name=self.name,
        )

        # Select the smallest EC2 instance type that satisfies the template's
        # compute/memory request (the standard cross-platform model); the
        # catalog is the built-in Graviton ladder or the site's
        # instance_types override.
        catalog = _instance_catalog(self.config)
        req_cpus = request.cpus
        req_memory = request.memory_gib
        selected = _select_instance_type(catalog, cpus=req_cpus, memory_gib=req_memory)
        size_summary = f"{selected.type} ({selected.cpus} vCPU / {selected.memory_gib} GiB)"
        if selected.cpus > req_cpus or selected.memory_gib > req_memory:
            output.warn(
                f"Rounded up to {selected.type} "
                f"({selected.cpus} vCPU / {selected.memory_gib} GiB) "
                f"for requested {req_cpus} vCPU / {req_memory} GiB."
            )
        # Root-volume sizing is driven by request.disk_gib, which the
        # vm-template layer always resolves (FR15), so the root volume is
        # always sized explicitly rather than left at the AMI's own.
        disk = request.disk_gib
        swap = request.swap_gib

        # Platform-owned naming with the slug as the namespacing token; the
        # backend name is the tag agentworks stamps on every resource, so a
        # collision is an error.
        backend_name = f"{request.system_slug}-{request.vm_name}" if request.system_slug else request.vm_name

        # Resolve the SSH allow scope BEFORE any resource exists: a detection
        # failure with no operator.ssh_allow_cidrs escape hatch is a typed error
        # while there is still nothing to roll back.
        ssh_allow_prefixes = operator_ssh_prefixes(config_allow_cidrs(ctx.config))

        output.detail("Connecting to AWS...")
        ec2 = self._client("ec2", region, ctx)

        if self._backend_name_in_use(ec2, backend_name):
            raise StateError(
                f"an EC2 instance tagged agentworks:vm={backend_name} already exists in region '{region}'",
                entity_kind="vm",
                entity_name=request.vm_name,
                hint="delete it first or pick a different VM name",
            )

        # Cross-check the declared arch against the real silicon BEFORE any
        # mutation: the declared arch is the source of truth for the SSM image
        # segment, so a catalog entry that lies about it would boot the wrong
        # image. This is a pre-mutation verification, not inference.
        self._verify_instance_arch(ec2, selected)

        ami = self._resolve_ami(ctx, region, selected.arch, ssm_release=ssm_release)
        # The primary network interface carries a permanent auto-assigned public
        # IP (see the launch block); a network interface must live in a subnet,
        # so the launch subnet is always concrete: the configured one, or the
        # account's default subnet. Resolving it also yields the VPC the security
        # group must live in.
        launch_subnet_id, vpc_id = self._resolve_launch_subnet(ec2, subnet_id, region)
        # Resolve any disk sizing BEFORE the mutating region: DescribeImages is
        # read-only, and a failure here must abort with a typed error, not be
        # re-wrapped by the create cleanup path (nothing is realized yet).
        block_device_mappings = self._disk_block_devices(ec2, ami, disk)

        output.info(f"Provisioning EC2 instance '{backend_name}' in {region}: {size_summary}...")
        if swap > 0:
            output.detail(f"Swap: {swap} GiB")

        # EC2 retains UserData. The SSH key is installed once by the cloud-init
        # users block. When create-time bootstrap is requested, embed the full
        # script with no Tailscale key and deliver the resolved key once over
        # the post-boot provisioning transport below.
        bootstrap = generate_bootstrap_script(
            admin_username=request.admin_username,
            ssh_public_key="",
            provisioning_packages=PROVISIONING_PACKAGES,
            tailscale_auth_key=None,
            hostname=request.hostname,
            swap=swap,
        )
        user_data = _generate_ec2_user_data(
            admin_username=request.admin_username,
            ssh_public_key=request.ssh_public_key,
            hostname=request.hostname,
            bootstrap_script=bootstrap,
        )
        # gzip the user-data: EC2 caps raw user-data at 16 KiB, which an
        # uncompressed payload with a large (RSA) key can exceed; cloud-init
        # detects and decompresses a gzipped payload natively, and boto3
        # base64-encodes the bytes for RunInstances. The guard raises typed if
        # even the compressed payload is over the cap (an unusually large key or
        # bootstrap), rather than letting AWS reject the launch opaquely.
        user_data_gz = gzip.compress(user_data.encode())
        if len(user_data_gz) > _MAX_USER_DATA_BYTES:
            raise EC2Error(
                f"cloud-init user-data for '{backend_name}' is {len(user_data_gz)} bytes compressed, "
                f"over EC2's {_MAX_USER_DATA_BYTES}-byte user-data limit",
                detail="the gzipped user-data exceeds the RunInstances raw user-data cap",
                entity_kind="vm-site",
                entity_name=self.site_name,
                hint="use a shorter admin SSH key (ed25519), or reduce vm-template config that grows the bootstrap",
            )

        security_group_id: str | None = None
        instance_id: str | None = None
        prov_transport: Transport | None = None
        tailscale_ip: str | None = None
        # The WHOLE fallible region is guarded by both arms of the create
        # rollback contract: a failure (including a non-SSHError from transport
        # construction or the inline bootstrap wait) AND an operator interrupt
        # each sweep the partial backend state before propagating. The caller's
        # unwind deletes only the DB row, so anything left here would be orphaned.
        try:
            # The security group is created with NO ingress rules: an empty EC2
            # group IS the deny-all-inbound baseline (see network.py's
            # create_security_group). The scoped bootstrap allow is poked AFTER
            # the instance is running, below.
            output.detail("Creating security group...")
            security_group_id = create_security_group(ec2, backend_name, vpc_id)

            output.detail("Launching instance...")
            # Launch with a permanent auto-assigned public IP
            # (AssociatePublicIpAddress=True). Exposure is controlled by the
            # security group's ingress rules, NOT by the presence of the IP, so
            # the IP stays for the VM's lifetime (it may change across stop/start
            # and is always read live, never cached). SubnetId and the security
            # group live INSIDE the interface block: RunInstances forbids them at
            # the top level once NetworkInterfaces is set.
            network_interface: dict[str, Any] = {
                "DeviceIndex": 0,
                "AssociatePublicIpAddress": True,
                "Groups": [security_group_id],
                "SubnetId": launch_subnet_id,
            }
            run_kwargs: dict[str, Any] = {
                "ImageId": ami,
                "InstanceType": selected.type,
                "MinCount": 1,
                "MaxCount": 1,
                "UserData": user_data_gz,
                "NetworkInterfaces": [network_interface],
                "TagSpecifications": [
                    {
                        "ResourceType": "instance",
                        "Tags": [vm_tag(backend_name), {"Key": "Name", "Value": backend_name}],
                    },
                    {"ResourceType": "volume", "Tags": [vm_tag(backend_name)]},
                ],
            }
            if block_device_mappings:
                run_kwargs["BlockDeviceMappings"] = block_device_mappings
            run_result = ec2.run_instances(**run_kwargs)
            instance_id = str(run_result["Instances"][0]["InstanceId"])

            output.detail("Waiting for instance to run...")
            ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])

            # Open the scoped bootstrap SSH hole (the group is deny-baseline
            # until now); post_tailscale_ready / secure_failed_vm close exactly
            # these prefixes, recorded in platform_metadata below.
            output.detail("Opening scoped SSH bootstrap access...")
            poke_ssh_allow(ec2, security_group_id, ssh_allow_prefixes)

            # Read the auto-assigned public IP LIVE (never cached).
            public_ip = self._public_ip(ec2, instance_id)
            output.detail(f"EC2 instance '{backend_name}' provisioned (IP: {public_ip})")

            import sys

            prov_transport = SSHTransport(
                host=public_ip,
                user=request.admin_username,
                identity_file=request.ssh_private_key,
                force_tty=sys.platform == "win32",
            )

            # The provider-retained bootstrap installed Tailscale without a
            # credential. Wait for it, then join once over stdin.
            tailscale_ip = EphemeralTailscaleBootstrap(prov_transport).complete(request.tailscale_auth_key)
        except KeyboardInterrupt:
            rollback_create_on_interrupt(ec2, region, backend_name, instance_id, security_group_id)
            raise
        except Exception as exc:
            output.detail("Cleaning up resources...")
            cleanup_partial(ec2, instance_id, security_group_id)
            if isinstance(exc, (AuthorizationError, StateError, TokenRejectedError)):
                raise
            raise wrap_ec2_error(exc) from exc

        # Reached only when the guarded region completed (both except arms
        # re-raise), so the transport was built.
        assert prov_transport is not None
        metadata = {
            "instance_id": instance_id,
            "security_group_id": security_group_id,
            "region": region,
            "backend_name": backend_name,
            # Exactly the prefixes the bootstrap allow was poked with, so the
            # close hooks revoke those tuples and nothing else (a concurrent
            # native route's distinct allow survives).
            "bootstrap_ssh_prefixes": ",".join(ssh_allow_prefixes),
        }
        return ProvisionResult(
            native_transport=prov_transport,
            platform_metadata=metadata,
            tailscale_ip=tailscale_ip,
        )

    def start(self, vm: VMRow, ctx: RunContext) -> None:
        # Idempotent by construction (the ABC flags start): EC2 start_instances
        # returns the current state without error on an already-running
        # instance, so no status guard is needed (as with azure).
        output.info(f"Starting EC2 instance '{vm.name}'...")
        ec2 = self._client("ec2", self._region_of(vm), ctx)
        try:
            ec2.start_instances(InstanceIds=[self._instance_id(vm)])
            ec2.get_waiter("instance_running").wait(InstanceIds=[self._instance_id(vm)])
        except Exception as exc:
            raise wrap_ec2_error(exc) from exc
        output.info(f"EC2 instance '{vm.name}' started")

    def stop(self, vm: VMRow, ctx: RunContext) -> None:
        # Idempotent by construction (the ABC flags stop): EC2 stop_instances
        # returns the current state without error on an already-stopped
        # instance.
        output.info(f"Stopping EC2 instance '{vm.name}'...")
        ec2 = self._client("ec2", self._region_of(vm), ctx)
        try:
            ec2.stop_instances(InstanceIds=[self._instance_id(vm)])
            ec2.get_waiter("instance_stopped").wait(InstanceIds=[self._instance_id(vm)])
        except Exception as exc:
            raise wrap_ec2_error(exc) from exc
        output.info(f"EC2 instance '{vm.name}' stopped")

    def delete(self, vm: VMRow, ctx: RunContext) -> None:
        output.info(f"Deleting EC2 instance '{vm.name}'...")
        instance_id = vm.platform_metadata.get("instance_id")
        if not instance_id:
            output.warn("no EC2 instance id, skipping EC2 cleanup")
            return
        ec2 = self._client("ec2", self._region_of(vm), ctx)
        terminate_and_cleanup_strict(ec2, str(instance_id), vm.platform_metadata.get("security_group_id"))
        output.info(f"EC2 instance '{vm.name}' deleted")

    def status(self, vm: VMRow, ctx: RunContext) -> VMStatus:
        instance_id = vm.platform_metadata.get("instance_id")
        if not instance_id:
            return VMStatus.UNKNOWN
        # The _client build is outside the try so a credential ESTABLISHMENT
        # failure (an empty resolved secret, a failed AssumeRole) surfaces
        # typed. The describe call is inside it, but a status probe that
        # tolerates an unreachable backend must still NOT swallow a definitive
        # credential rejection: reporting UNKNOWN because the site's credential
        # was rejected would hide a misconfiguration behind a plausible-looking
        # answer, exactly in `vm describe` where an operator checks a broken
        # site. botocore lets us tell the two apart at the call site, so a
        # credential or authorization rejection re-raises typed (via the same
        # structured classifiers runup uses) and only a transient/unreachable
        # failure degrades to UNKNOWN.
        ec2 = self._client("ec2", self._region_of(vm), ctx)
        try:
            result = ec2.describe_instances(InstanceIds=[instance_id])
        except Exception as exc:
            if is_credential_rejection(exc) or is_authorization_denial(exc):
                raise wrap_ec2_error(exc) from exc
            return VMStatus.UNKNOWN
        state = first_instance_state(result)
        # Only stable provider states satisfy power-state preconditions.
        # Pending/stopping are transitions and shutting-down/terminated is a
        # deleted instance, all reported as UNKNOWN.
        if state == "running":
            return VMStatus.RUNNING
        if state == "stopped":
            return VMStatus.STOPPED
        return VMStatus.UNKNOWN

    def display_backend_name(self, vm: VMRow) -> str:
        instance_id = vm.platform_metadata.get("instance_id")
        if not instance_id:
            return vm.name
        region = vm.platform_metadata.get("region")
        return f"{instance_id}@{region}" if region else instance_id

    def native_transport(
        self,
        vm: VMRow,
        ctx: RunContext,
        *,
        config: Config | None = None,
    ) -> Transport | None:
        ec2 = self._client("ec2", self._region_of(vm), ctx)
        try:
            result = ec2.describe_instances(InstanceIds=[self._instance_id(vm)])
        except Exception as exc:
            raise wrap_ec2_error(exc) from exc

        # The public IP is read LIVE off a fresh describe every time (EC2
        # reassigns it across stop/start, so it is never cached). The SSH route
        # is opened by transient_route, which the transports factory wraps
        # around this call; a stopped instance has no public IP, which
        # propagates to SSHTransport(host="") and the factory's typed StateError.
        public_ip = first_instance_public_ip(result)
        import sys

        identity_file = None
        if config is not None:
            identity_file = getattr(getattr(config, "operator", None), "ssh_private_key", None)

        return SSHTransport(
            host=public_ip,
            user=vm.admin_username,
            identity_file=identity_file,
            force_tty=sys.platform == "win32",
        )

    def post_tailscale_ready(self, vm: VMRow, ctx: RunContext) -> None:
        """Close provisioning access now that Tailscale is up.

        The VM comes out of :meth:`create` reachable only through the ephemeral
        bootstrap SSH allow (scoped to the operator's egress prefixes; the empty
        security group is the deny-all-inbound baseline). This hook fires at the
        async Tailscale-ready point inside ``bootstrap_vm`` (Phase A) and revokes
        EXACTLY the bootstrap allow's tuples (recorded in platform_metadata at
        create), leaving zero inbound exposure; the public IP stays for the VM's
        lifetime.

        ``ctx`` is the create op's own scoped context (secrets already resolved
        before Phase A): revoking the allow is a network call and reads the
        credential through it, with no ambient fallback.
        """
        self._close_provisioning_access(vm, ctx)

    def secure_failed_vm(self, vm: VMRow, ctx: RunContext) -> None:
        """Fail closed after kept post-create verification failure.

        Mirrors :meth:`post_tailscale_ready`, which only fires on success: a VM
        whose Tailscale SSH verification died (marked FAILED) or was interrupted
        during verification (row status untouched by the abort) is kept for
        debugging, and this hook revokes exactly the bootstrap allow's tuples so
        it defaults to zero inbound exposure. Create-time bootstrap failures roll
        back inside :meth:`create` and never reach this hook. Debugging and
        recovery stay possible: ``vm shell --platform`` and ``vm delete`` poke a
        fresh transient allow via :meth:`transient_route`.

        ``ctx`` is the create op's own scoped context, whose secrets were
        resolved before Phase A began, so even the interrupt path never resolves
        a secret here for the first time; on an ``access-key`` site the
        network call authenticates as the configured access key, with no ambient
        fallback.
        """
        self._close_provisioning_access(vm, ctx)

    @contextlib.contextmanager
    def transient_route(self, vm: VMRow, ctx: RunContext, *, config: Config | None = None) -> Iterator[None]:
        """Open a scoped SSH route to the VM for the context's duration.

        Enter pokes this operation's SSH allow scoped to the operator's egress
        prefixes (widened by ``config.operator.ssh_allow_cidrs``); the finally
        revokes exactly those prefixes on every unwind path, so a concurrent
        native op's non-overlapping allows are untouched. A shared prefix across
        concurrent ops is idempotent to poke and tolerant to revoke (see
        network.py's poke_ssh_allow / remove_ssh_allow, which carry the full
        contract). Unlike azure there is no public-IP heal on enter: the EC2
        auto-assigned IP is permanent for the VM's lifetime.

        ``ctx`` is the op-start context threaded from the factory; ``config``
        carries OPERATOR settings (``operator.ssh_allow_cidrs``), mirroring
        azure and :meth:`native_transport`.

        ``poke_ssh_allow`` checks the matching revoke permissions before it
        mutates ingress and cleans up any partial poke itself. Once the poke
        succeeds, this context's ``finally`` owns the matching close.
        """
        ec2 = self._client("ec2", self._region_of(vm), ctx)
        security_group_id = self._security_group_id(vm)
        prefixes = operator_ssh_prefixes(config_allow_cidrs(config))
        poke_ssh_allow(ec2, security_group_id, prefixes)
        try:
            yield
        finally:
            remove_ssh_allow(ec2, security_group_id, prefixes)

    # -- Helpers ---------------------------------------------------------------

    def _instance_id(self, vm: VMRow) -> str:
        """The VM's EC2 instance id from platform metadata, or a typed error."""
        instance_id = vm.platform_metadata.get("instance_id")
        if not instance_id:
            raise StateError(
                f"VM '{vm.name}' has no EC2 instance_id in its platform metadata; the DB row is incomplete",
                entity_kind="vm",
                entity_name=vm.name,
            )
        return str(instance_id)

    def _security_group_id(self, vm: VMRow) -> str:
        """The VM's security-group id from platform metadata, or a typed error
        (the transient route cannot open without it)."""
        security_group_id = vm.platform_metadata.get("security_group_id")
        if not security_group_id:
            raise StateError(
                f"VM '{vm.name}' has no EC2 security_group_id in its platform metadata; the DB row is incomplete",
                entity_kind="vm",
                entity_name=vm.name,
            )
        return str(security_group_id)

    def _region_of(self, vm: VMRow) -> str:
        """The VM's region: the recorded one (decouples existing VMs from config
        edits) falling back to the site's configured region for rows that
        predate the metadata key."""
        return str(vm.platform_metadata.get("region") or self.config.region)

    def _close_provisioning_access(self, vm: VMRow, ctx: RunContext) -> None:
        """Revoke EXACTLY the bootstrap allow's tuples on the VM's per-VM
        security group (best-effort). Shared by the two close hooks.

        Revoking exactly the recorded bootstrap prefixes, rather than sweeping
        all ingress, is what lets a concurrent ``vm shell --platform`` route
        (nothing serializes commands per VM) keep its own distinct allow: a
        blanket revoke-all would tear it out. The prefixes come from
        platform_metadata, recorded by :meth:`create` as exactly what it poked;
        recomputing them here would drift if the operator's egress IP or
        ``ssh_allow_cidrs`` changed since create, revoking the wrong tuples.
        ``remove_ssh_allow`` is tolerant of an already-gone tuple, so a shared
        prefix another route revoked first is a no-op. A row with no recorded
        security group or prefixes is warned and skipped (the hooks are
        best-effort; ``delete`` removes the group outright regardless)."""
        security_group_id = vm.platform_metadata.get("security_group_id")
        if not security_group_id:
            output.warn(f"VM '{vm.name}' has no EC2 security_group_id; cannot close provisioning access")
            return
        recorded = vm.platform_metadata.get("bootstrap_ssh_prefixes")
        prefixes = [p for p in recorded.split(",") if p] if recorded else []
        if not prefixes:
            output.warn(f"VM '{vm.name}' has no recorded bootstrap SSH prefixes; cannot close provisioning access")
            return
        ec2 = self._client("ec2", self._region_of(vm), ctx)
        remove_ssh_allow(ec2, str(security_group_id), prefixes)

    def _public_ip(self, ec2: Any, instance_id: str) -> str:
        """The instance's live auto-assigned public IP (never cached)."""
        try:
            result = ec2.describe_instances(InstanceIds=[instance_id])
        except Exception as exc:
            raise wrap_ec2_error(exc) from exc
        return first_instance_public_ip(result)

    def _backend_name_in_use(self, ec2: Any, backend_name: str) -> bool:
        """Pre-flight: is a non-terminated instance already tagged with this
        backend name in the region?

        Fails CLOSED, like the collision probes on the other platforms
        (azure ``_vm_exists``, lima ``_instance_exists``): only a genuine
        absence answers "no collision", and any other error surfaces typed.
        The stakes are highest here, though: azure VM names are
        uniqueness-enforced by the platform, so a duplicate create fails at
        the API regardless of that guard, but the EC2 ``agentworks:vm`` tag
        is NOT unique-enforced, so this describe is the only thing standing
        between a describe failure and a silent duplicate instance. runup has
        just validated the credential, so a failure here is anomalous; the
        create contract wants collision checks loud, so it surfaces typed
        rather than being read as "no collision"."""
        try:
            result = ec2.describe_instances(
                Filters=[
                    {"Name": "tag:agentworks:vm", "Values": [backend_name]},
                    {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
                ]
            )
        except Exception as exc:
            raise wrap_ec2_error(exc) from exc
        return any(reservation.get("Instances") for reservation in result.get("Reservations", []))

    def _verify_instance_arch(self, ec2: Any, selected: _InstanceType) -> None:
        """One DescribeInstanceTypes call confirming the catalog entry's
        declared arch matches the real silicon of its EC2 instance type; a
        mismatch is a typed ConfigError naming the entry. The declared arch is
        the source of truth (it drives the SSM image segment), so this catches
        a catalog that lies rather than inferring the arch from EC2."""
        try:
            result = ec2.describe_instance_types(InstanceTypes=[selected.type])
        except Exception as exc:
            raise wrap_ec2_error(exc) from exc
        entries = result.get("InstanceTypes", [])
        supported = entries[0].get("ProcessorInfo", {}).get("SupportedArchitectures", []) if entries else []
        if selected.arch not in supported:
            raise ConfigError(
                f"instance type '{selected.type}' declares arch '{selected.arch}' but EC2 reports it supports "
                f"{', '.join(supported) or 'nothing'}",
                entity_kind="vm-site",
                entity_name=self.site_name,
                hint=f"correct the arch on the '{selected.type}' entry in the site's instance_types",
            )

    def _resolve_ami(self, ctx: RunContext, region: str, arch: str, *, ssm_release: str) -> str:
        """Resolve the requested Debian release AMI from public SSM.

        This is the only image source: core supplies the release and this
        platform translates it to the official provider selector. There is no
        operator image knob or platform-local default.
        An arbitrary AMI would break the provisioning contract mid-bootstrap
        rather than failing typed, so the platform does not accept one."""
        segment = _DEBIAN_ARCH_SEGMENT[arch]
        parameter = f"/aws/service/debian/release/{ssm_release}/latest/{segment}"
        ssm = self._client("ssm", region, ctx)
        try:
            result = ssm.get_parameter(Name=parameter)
        except Exception as exc:
            raise wrap_ec2_error(exc) from exc
        return str(result["Parameter"]["Value"])

    def _resolve_launch_subnet(self, ec2: Any, subnet_id: str | None, region: str) -> tuple[str, str]:
        """The subnet to launch the instance into and the VPC it belongs to.

        A configured subnet is used as is (its VPC read back so the security
        group lands in the same VPC). With none configured, the account's
        default subnet is resolved: the launch specifies a primary network
        interface (to carry the auto-assigned public IP), and a network
        interface must specify a subnet, so the zero-config path cannot rely on
        RunInstances' top-level default-subnet convenience (which does not apply
        once an interface is given). A default-VPC-less account with no
        configured subnet is a typed error pointing at ``subnet_id`` rather than
        an opaque RunInstances failure. The default-subnet listing is region-wide
        (one per AZ), so the choice is made DETERMINISTIC by taking the lowest
        availability zone rather than the API's arbitrary first, so repeated
        creates for a site land in the same AZ."""
        if subnet_id:
            try:
                result = ec2.describe_subnets(SubnetIds=[subnet_id])
            except Exception as exc:
                raise wrap_ec2_error(exc) from exc
            subnets = result.get("Subnets", [])
            if not subnets:
                raise NotFoundError(
                    f"EC2 subnet '{subnet_id}' does not exist in region '{region}' (vm-site '{self.site_name}')",
                    entity_kind="subnet",
                    entity_name=subnet_id,
                    hint=f"point vm-site '{self.site_name}' at an existing subnet_id in region {region}",
                )
            return subnet_id, str(subnets[0]["VpcId"])
        try:
            result = ec2.describe_subnets(Filters=[{"Name": "default-for-az", "Values": ["true"]}])
        except Exception as exc:
            raise wrap_ec2_error(exc) from exc
        subnets = result.get("Subnets", [])
        if not subnets:
            raise EC2Error(
                f"no default subnet in region '{region}' to launch vm-site '{self.site_name}' into",
                detail="describe_subnets found no default-for-az subnet and the site configured none",
                entity_kind="vm-site",
                entity_name=self.site_name,
                hint=f"set the site's subnet_id to a subnet in your VPC for region {region}",
            )
        # Deterministic pick: lowest AZ, then subnet id, independent of the
        # order describe_subnets happens to return.
        chosen = min(subnets, key=lambda s: (str(s.get("AvailabilityZone", "")), str(s.get("SubnetId", ""))))
        return str(chosen["SubnetId"]), str(chosen["VpcId"])

    def _disk_block_devices(self, ec2: Any, ami: str, disk_gib: int) -> list[dict[str, Any]]:
        """The BlockDeviceMappings to resize the root volume to ``disk_gib``.

        The AMI's real root device name is read from DescribeImages rather
        than hard-coded: Debian's published root device could change across
        image releases, and a wrong constant would attach the sized volume
        to the wrong device and silently drop the operator's request."""
        root_device = self._root_device_name(ec2, ami)
        return [
            {
                "DeviceName": root_device,
                "Ebs": {"VolumeSize": disk_gib, "VolumeType": "gp3", "DeleteOnTermination": True},
            }
        ]

    def _root_device_name(self, ec2: Any, ami: str) -> str:
        """The AMI's root device name (e.g. ``/dev/xvda``), read to size the root
        EBS volume. Raises a typed :class:`EC2Error` on a lookup failure or an
        image with no root device, rather than guessing a default: a wrong guess
        would size the wrong device and silently drop the disk request."""
        try:
            result = ec2.describe_images(ImageIds=[ami])
        except Exception as exc:
            mapped = wrap_ec2_error(exc)
            if isinstance(mapped, (AuthorizationError, TokenRejectedError)):
                raise mapped from exc
            raise EC2Error(
                f"could not read AMI '{ami}' to size the disk for vm-site '{self.site_name}'",
                detail=str(exc),
                entity_kind="vm-site",
                entity_name=self.site_name,
                hint="grant ec2:DescribeImages to the credential, or drop the vm-template's disk request",
            ) from exc
        images = result.get("Images", [])
        root_device = images[0].get("RootDeviceName") if images else None
        if not root_device:
            raise EC2Error(
                f"AMI '{ami}' reported no root device name; cannot size the disk for vm-site '{self.site_name}'",
                detail="describe_images returned no RootDeviceName for the image",
                entity_kind="vm-site",
                entity_name=self.site_name,
                hint="verify the AMI id, or drop the vm-template's disk request to keep the image's own root size",
            )
        return str(root_device)
