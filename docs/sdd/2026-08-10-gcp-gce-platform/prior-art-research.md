# GCP GCE VM platform: provider prior-art research

## Purpose

This artifact records the external provider semantics that constrain `gcp-gce`. It exists so the
schema and state machine are reviewed against current GCP behavior before implementation, rather
than rediscovered while mutating live resources.

Research date: 2026-08-10. Sources are official Google Cloud, Google Auth, and PyPI references.

## SDK and authentication

- [`google-cloud-compute` 1.50.0](https://pypi.org/project/google-cloud-compute/) is the latest
  stable Compute client release reviewed for this effort. `InstancesClient.insert` returns an
  `ExtendedOperation` and accepts a typed `Instance` body.
- [`google-auth` 2.56.3](https://pypi.org/project/google-auth/) is the latest stable authentication
  release reviewed for this effort.
- [`google-api-core` 2.34.0](https://pypi.org/project/google-api-core/) is imported directly for
  provider exception categories and is therefore a direct dependency rather than an undeclared
  transitive implementation detail.
- [Application Default Credentials](https://docs.cloud.google.com/docs/authentication/application-default-credentials)
  is the ambient mechanism supported by Google client libraries.
- [`Credentials.from_service_account_info`](https://google-auth.readthedocs.io/en/latest/reference/google.oauth2.service_account.html)
  constructs a service-account credential from one parsed JSON mapping. That makes a whole-document
  secret the natural explicit arm and removes any reason to split private key, email, token URI, or
  project fields across config.

Decision: declare all three imported packages directly, use ADC only for the ambient arm, use one
complete JSON secret only for the explicit arm, and never fall back across arms.

## Instance metadata and bootstrap

- [Linux startup scripts](https://docs.cloud.google.com/compute/docs/instances/startup-scripts/linux)
  are provider-retained instance metadata, run as root by the guest agent, and run on every boot.
  Direct `startup-script` content is limited to 256 KB.
- Google public Compute images include the guest environment that executes metadata scripts.
- [Metadata-managed SSH keys](https://docs.cloud.google.com/compute/docs/connect/add-ssh-keys)
  create the named Linux user and authorized key when OS Login is disabled. OS Login causes the
  guest agent to ignore metadata SSH keys.
- [Predefined metadata keys](https://docs.cloud.google.com/compute/docs/metadata/predefined-metadata-keys)
  include `ssh-keys`, `block-project-ssh-keys`, and `enable-oslogin`.
- [Debian image details](https://docs.cloud.google.com/compute/docs/images/os-details) list Debian
  12 families `debian-12` for x86 and `debian-12-arm64` for Arm.

Decisions:

- retain a credential-free startup script in metadata;
- wrap it in a durable run-once marker checked before any mutation and written only after full
  success;
- measure the UTF-8 `startup-script` value before mutation, accept at most 256 KiB, and pin that
  exact boundary in retained-request tests;
- block project SSH keys and disable OS Login at instance metadata for the supported path;
- use a bounded local-marker polling command before the fixed-stdin Tailscale join;
- reject/roll back when an organization policy still forces OS Login and provisioning SSH cannot
  become ready.

## Instance names and machine types

- The
  [`instances.insert` reference](https://docs.cloud.google.com/compute/docs/reference/rest/v1/instances/insert)
  constrains instance names to lowercase RFC1035 form, while Agentworks VM names may contain
  underscores and begin with digits.
- [`MachineType.architecture`](https://docs.cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.types.MachineType)
  is an output-only proto field alongside guest CPU and memory fields. Live GCE responses may omit
  it, which the Python model exposes as an empty string.
- Google's
  [E2 machine-type table](https://docs.cloud.google.com/compute/docs/general-purpose-machines#e2_machine_types)
  reports that `e2-small` and `e2-medium` expose two guest vCPUs and 2 GiB and 4 GiB respectively,
  while sustaining an aggregate 0.5 and 1 vCPU with automatic bursting. E2 is available across all
  regions and zones and supports `pd-balanced`.
- The
  [Persistent Disk compatibility table](https://docs.cloud.google.com/compute/docs/disks/persistent-disks#machine_series_support)
  shows that some current series, including N4, do not support Persistent Disk. The live
  [`MachineType` resource](https://docs.cloud.google.com/compute/docs/reference/rest/v1/machineTypes)
  exposes presence-tracked `maximumPersistentDisks` and required guest accelerators, which identify
  known incompatibilities without a brittle machine-name allowlist. An omitted output-only scalar
  reads as zero through the Python property and must be distinguished through proto presence.
  Neither that resource nor the zonal `DiskType` resource validates a complete machine/disk pair
  before `instances.insert`.

Decisions:

- normalize invalid Agentworks names deterministically, prefix a leading non-letter, and append a
  stable original-name hash whenever normalization changes or truncates the input;
- select from the declared catalog, then read the live machine type and verify CPU and memory before
  mutation; verify architecture only when the provider populates it, otherwise retain the declared
  catalog value without deriving architecture from the machine-type name;
- keep the standard E2 ladder as the built-in default because the catalog's `cpus` field records
  guest-visible vCPUs and cannot also express sustained shared-core capacity; teach `e2-small` and
  `e2-medium` as explicit site `machine_types` overrides with their sustained/burst behavior;
- reject a selected live type before mutation when it populates zero Persistent Disk capacity or
  required accelerators, naming the current CPU-only `pd-balanced` support boundary; accept omitted
  capacity as unknown rather than interpreting its proto scalar default; keep the literal catalog
  extensible and add Hyperdisk through a future explicit storage profile rather than series-name
  inference;
- treat those fields as known-incompatibility filters, not complete pair proof; give every residual
  definitive instance-insert rejection a fixed, provider-text-free hint to verify IAM, quota, and
  request prerequisites before the selected machine type's CPU-only Debian 12 and `pd-balanced`
  compatibility;
- map `x86_64` to `debian-12` and `arm64` to `debian-12-arm64`.
- resolve both families from the public `debian-cloud` image project, never the target project.

## Network and external IPv4 behavior

- In
  [`instances.insert`](https://docs.cloud.google.com/compute/docs/reference/rest/v1/instances/insert),
  omitted network and subnet select `global/networks/default`; a project without that network must
  name another network/subnet. A `ONE_TO_ONE_NAT` access config supplies an ephemeral external IPv4.
- [External IP guidance](https://docs.cloud.google.com/compute/docs/ip-addresses) requires Cloud NAT
  or another route for ordinary public IPv4 egress when a VM has no external IP.

Decisions:

- an omitted `subnet` explicitly resolves and verifies the default network before mutation;
- a configured subnet is a name in the region derived from the zone and its network URL is used for
  firewall rules;
- retain the ephemeral external access config for the VM lifetime so default-network sites keep
  Tailscale and ordinary internet egress without an undeclared Cloud NAT prerequisite;
- read its address live because an ephemeral address can change across stop/start.

## Firewall evaluation

- [VPC firewall rules](https://docs.cloud.google.com/compute/docs/reference/rest/v1/firewalls) can
  target instance network tags and source ranges.
- [Firewall priority/evaluation order](https://docs.cloud.google.com/firewall/docs/firewall-policies-rule-eval-order)
  evaluates lower numeric priorities first. Organization/folder policy can terminate before VPC
  rules, and global/regional network firewall policies can precede classic VPC rules when the
  network uses `BEFORE_CLASSIC_FIREWALL`.
- The [default-network rule table](https://docs.cloud.google.com/firewall/docs/firewalls) includes
  broad SSH, RDP, ICMP, and internal ingress allows, commonly at low precedence such as 65534.

Decisions:

- create an owned priority-1 all-ingress deny for a unique Agentworks instance tag;
- create unique priority-0 TCP/22 allows only for operator SSH source prefixes;
- require `AFTER_CLASSIC_FIREWALL` and reject `BEFORE_CLASSIC_FIREWALL` before mutation;
- reject every inspectable classic VPC priority-0 ingress allow that applies to every instance or
  the derived tag, because any protocol would bypass the permanent deny;
- reject an applicable classic VPC priority-0 deny whose source/protocol overlaps operator TCP/22,
  because equal-priority deny wins;
- treat a higher-level terminal ingress allow or conflicting operator-SSH deny as an explicit
  unsupported project prerequisite rather than adding organization-policy permissions and machinery
  to this plugin;
- keep the deny while the VM may survive, and remove it only after instance absence is proven;
- use a unique allow per native route so concurrent routes do not close one another.

## Disk and guest authority

- The
  [`instances.insert` disk contract](https://docs.cloud.google.com/compute/docs/reference/rest/v1/instances/insert)
  exposes attached-disk `autoDelete`; it must be explicit to make instance deletion own the boot
  disk's lifecycle.
- The same request exposes `serviceAccounts`; attaching one grants the guest provider authority.

Decisions: set the balanced boot disk `auto_delete=True`, inspect it in the retained-request test,
and explicitly attach no guest service account or scopes.

## Operations and idempotency

- Compute mutation methods return bounded operations. The client library exposes operation result
  and error fields rather than making a submitted request synonymous with completion.
- [Stop](https://docs.cloud.google.com/compute/docs/reference/rest/v1/instances/stop) and
  [start](https://docs.cloud.google.com/compute/docs/reference/rest/v1/instances/start) describe
  real state transitions. CLI behavior tolerates an already-stopped stop, but the API reference does
  not establish both transitions as universally idempotent.

Decisions:

- wait for every mutation operation and inspect its terminal error;
- guard start/stop with live status so Agentworks idempotency does not depend on undocumented API
  tolerance;
- after delete timeout/failure, query the instance: a survivor/indeterminate result raises and keeps
  the deny; proven absence permits deny cleanup;
- already-not-found remains idempotent success.

The same possible-resource rule applies to firewall inserts, but exact name and shape do not prove
ownership. Each attempt supplies a unique `requestId`. A pre-response indeterminate call is retried
once with that same ID; the accepted operation must expose it as `clientOperationId`, identify an
insert of the expected target link, and expose a `targetId` equal to the realized firewall's
provider ID. Cleanup requires that provider ID plus the full network, direction, target, priority,
source, and allow/deny request shape. Missing proof or a mismatch is retained and reported as a
collision. Because firewall delete has no provider-ID precondition, the verification-to-delete gap
cannot protect against hostile delete/recreate replacement and is not represented as atomic.

## Optional guest Google Cloud CLI

- Google's [Google Cloud CLI install guide](https://docs.cloud.google.com/sdk/docs/install-sdk)
  publishes a signed apt repository for supported Debian and Ubuntu releases and installs the
  `google-cloud-cli` package. That package provides `gcloud`, `gsutil`, and `bq`; extra components
  such as `kubectl` remain separate packages.
- Agentworks already models third-party signed repositories and their packages as `apt-source` and
  `apt-package` resources. Those declared resources fit the supported Debian guest without embedding
  the repository setup as a shell script.

Decisions:

- publish one `google-cloud-cli` apt source and one dependent `gcloud-cli` apt package from the
  `gcp` plugin;
- bundle the optional guest CLI for parity with the established Azure plugin surface and because the
  operator explicitly requested consistent cloud-provider guest tooling;
- use the existing apt resources rather than embedding repository, key, or installer logic in a
  command string;
- keep the resources optional and guest-scoped: GCE operations continue to use the Python SDK, and
  ambient mode may obtain host ADC through any supported ADC source while optional host recovery
  tooling remains separate from this declarable;
- do not bundle optional Google Cloud components until a concrete template requires one.

## Resulting shared seam

Azure/AWS use the shared post-boot stdin join after `cloud-init status --wait`; GCP uses a startup
script marker. The smallest honest seam is a non-secret readiness command/label parameter on
`EphemeralTailscaleBootstrap`, defaulted to the existing cloud-init behavior. GCP passes a fixed
blocking marker poll. Secret delivery, retry bounds, stdin command, IP discovery, and errors remain
shared.

No broader callback, provider branch in the manager, guest-attribute protocol, Cloud NAT manager, or
general bootstrap engine is justified by the reviewed requirements.

-- agw-ns-gcp-platform (effort lead)
