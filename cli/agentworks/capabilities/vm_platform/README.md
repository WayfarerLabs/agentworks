# VM Platforms

> The detailed companion to the capability overview in [`../README.md`](../README.md), focused on
> the `vm-platform` kind. This guide covers the available platforms, the contract each
> implementation must honor, and the lifecycle, security, and resource-management patterns behind
> the shared interface.

## What Is a VM Platform?

A VM platform is a backend that provisions and manages virtual machines (VMs) for Agentworks. It
abstracts the underlying infrastructure, allowing operators to create and manage VMs on different
environments without changing the way they interact with Agentworks. The platform handles the
creation, starting, stopping, and deletion of VMs, as well as providing a transport for executing
commands on the VMs. On the full-control cloud platforms it also owns the VM's network exposure,
locking it down by default and opening access only in a controlled, scoped way; on operator-managed
hosts the existing perimeter stays authoritative and the platform does not touch it (see Security
Posture below).

### Relationship to VM Sites

VM platforms represent general capabilities. VM sites, declared as YAML manifests, bind a platform
to a specific configuration. The exact configuration surface depends on the platform, but generally
includes location, credentials, and other platform-specific settings.

## Available Platforms

Five platforms ship today. This list can change, so `agw resource describe-kind vm-platform` is the
definitive set on any given install (it reads no config, so it answers even on a host that cannot
load one), and `agw resource describe-kind vm-platform/<name>` is the definitive config for one.

- **`lima`** (built in) runs fast local VMs on the operator's machine (commonly macOS, but any host
  Lima supports). It can also connect to a remote Linux host over SSH and drive `limactl` there,
  creating and managing Lima VMs on infrastructure the operator administers.
- **`wsl2`** (built in) leverages the Windows Subsystem for Linux 2 to run Agentworks VMs on Windows
  hosts. It is a local platform that does not require external infrastructure and is available only
  on Windows hosts with WSL2 installed.
- **`proxmox`** (via the `proxmox` system plugin) runs VMs on a Proxmox hypervisor, a popular
  open-source virtualization platform. It is suitable for operators who want agent VMs on Proxmox
  infrastructure they administer.
- **`azure-vm`** (via the `azure` system plugin) runs VMs on the Azure Virtual Machines service,
  placing the workload on managed cloud infrastructure in an operator-selected subscription,
  resource group, and region.
- **`aws-ec2`** (via the `aws` system plugin) runs VMs on Amazon EC2 in an operator-selected region
  and optional subnet.

Per the Agentworks model, the choice of platform largely disappears once a VM is up and running. All
VMs run the same base OS (Debian Bookworm) and are accessible via SSH over Tailscale.

## Security Posture

On the full-control cloud platforms (`azure-vm` and `aws-ec2`), Agentworks provisions not just the
VM but its whole network exposure surface, and it locks that surface down by default. The baseline
is no standing inbound access at all: a freshly provisioned cloud VM has nothing open to the
internet. When Agentworks genuinely needs to reach the VM (to bring it up, or to open a shell for
the operator), it opens a narrowly scoped hole for the workstation's detected public IPv4 address
(as a `/32`) plus any configured `operator.ssh_allow_cidrs`. The hole exists only for that operation
and closes as soon as the work is done; this scoped, ephemeral behavior is the cloud-platform
default.

On platforms where the host is not Agentworks' to control (`proxmox` and remote Lima on
operator-administered hosts, or a local `lima` / `wsl2` VM on the operator's machine), the existing
perimeter stays authoritative and Agentworks does not touch it. The mechanism behind both cases is
described below.

## VM Platform Obligations

A vm-platform stands up a machine and hands Agentworks an administrative foothold on it. It:

- **MUST** provision a VM running the standard base operating system, Debian Bookworm, at the
  operator-configured site. (An externally administered backend that clones an operator-supplied
  template inherits this from the template today; that deviation is being closed under
  [#368](https://github.com/WayfarerLabs/agentworks/issues/368).)
- **MUST** create the admin user with the operator-configured name, holding full passwordless `sudo`
  over the machine, reachable by the operator's installed SSH public key and never by password.
- **MUST** provide a transport that runs arbitrary commands as the admin user: the single foothold
  every later provisioning step is driven through.
- **MUST** join the VM to the operator's Tailscale tailnet when given an auth key (or otherwise make
  it reachable), giving it a stable address for the life of the VM.
- **MUST** provision the VM to the requested cpus, memory, and disk, rounding up to the nearest
  available shape where the backend sells only fixed shapes. A backend that structurally cannot
  honor a per-VM shape (WSL2, whose limits are global) is the exception, and it **MUST** at least
  warn that the requested resources are being ignored
  ([#369](https://github.com/WayfarerLabs/agentworks/issues/369)).
- **MUST** support the lifecycle Agentworks drives, create, start, stop, and delete, and report the
  VM's status; `create` **MUST** be collision-checked and either fail loudly on a name that already
  exists or pick and persist a new, collision-free backend name, never impacting an existing VM
  outside its control.
- **MUST** provide a stop that preserves all system state for a later resume. Snapshotting and
  restoring running state is preferable, but a platform **MAY** implement stop as a full OS
  shutdown/restart, since Agentworks is built to be robust against restarts and the loss of running
  processes.
- **SHOULD** take reasonable steps to reduce the cost and resource usage of a stopped VM, releasing
  billable or heavy resources (compute, memory) for the duration of the stop where the backend
  allows it. Some standing costs are over that line and accepted: Azure keeps its permanent public
  IP attached (and billing) while stopped, because detaching it would incur unnecessary complexity.
- **MUST** roll back its own partial backend state before letting a failure or an operator interrupt
  propagate out of `create`, and **MUST NOT** report a `delete` as successful unless the backend VM
  is actually gone. A delete that cannot remove the VM **MUST** raise so Agentworks retains its row
  for a retry rather than orphaning a backend resource.
- **MUST NOT** leave billed or orphaned backend resources behind after a delete or a rolled-back
  create: every resource it creates is scoped to exactly one VM and shares that VM's lifecycle.
- **MUST NOT** touch, reconfigure, or tear down anything it did not create for this VM, whether
  another VM's resources, another site's state, or the operator's shared infrastructure (a resource
  group, VPC, subnet, or bridge), which it reads and existence-checks but never creates or deletes.
- On a full-control cloud host it **MUST** default to zero standing inbound exposure, opening only a
  narrowly scoped, ephemeral hole for the one operation that needs it and closing it after, failing
  closed rather than open; on an externally administered or local host it **MUST NOT** manage the
  host's or network's security at all.
- **MUST NOT** share host filesystem paths into the guest by default (a VM is self-contained), and
  **MUST NOT** log or persist resolved secret values (its metadata carries only backend
  identifiers).

Notably, VM platforms do not create agent users, workspaces, groups, sessions, or inject secrets.
Those are managed by the Agentworks core system through platform-agnostic mechanisms.

## Technical Overview

Everything above this line is for operators. Everything below it is for engineers implementing or
extending a VM platform: the platform surface each backend implements, how an op gets its
dependencies, the exposure and credential machinery on the cloud platforms, the provisioning
timeline, and implementation pitfalls. If you are selecting or configuring a platform rather than
writing one, you can stop here.

Five platforms ship today and are the working references throughout this guide: `lima` (`lima.py`)
and `wsl2` (`wsl2.py`) as core built-ins, plus `proxmox`, `azure-vm`, and `aws-ec2`, which ship in
opt-in system plugins (`agentworks/plugins/proxmox/platform.py`, with its REST client in `api.py`;
`agentworks/plugins/azure/platform.py`, with its network mechanics in `network.py`; and
`agentworks/plugins/aws/platform.py`, likewise split from its `network.py`). The rules below apply
to a plugin-shipped platform exactly as to a core one; each plugin re-seats its class into
`VM_PLATFORM_REGISTRY` at import, so authoring a platform is the same either way. When a rule below
has a concrete example, it names the platform and file that demonstrates it.

### Technical Definition of a VM Platform

A VM platform is the code capable of running VMs on a specific VM provider. Each subclasses
`VMPlatform` (`base.py`), registers in `VM_PLATFORM_REGISTRY` (`__init__.py`), and publishes as a
read-only `vm-platform` capability resource. Operators never invoke a platform directly: a
declarable `vm-site` binds a platform to a config blob (one tagged `spec.platform` table whose
`name` key selects the platform), and all invocation goes through site resolution
(`agentworks.vms.sites`). ADR 0016 records the capability/declarable split; ADR 0019 records the
orchestration layer that now drives the lifecycle (below).

### Host Control and Platform Obligations

Platform patterns depend on who controls the machine and its surrounding network. The exposure model
below (baseline deny, ephemeral scoped allows) is a category-1 obligation, not a universal one;
replicating it in another category would cross the platform's ownership boundary.

1. **Full-control cloud platforms** (`azure-vm` and `aws-ec2` today). Agentworks provisions the host
   AND its network exposure surface (Azure: public IP, NSG, VNet, NIC; EC2: public IP, security
   group, ENI, launched into an existing subnet), so it owns the security posture end to end. The
   locked-down-by-default exposure model below applies in full, as do the rollback obligations for
   the remotely billed resources a failed create must not leak.
2. **Externally administered hosts** (`proxmox`; Lima via a site's `ssh` placement). The hypervisor
   belongs to the operator's infrastructure. Agentworks does not control host or network security
   there and must not try to: no firewall management, no exposure toggling. The operator's own
   perimeter is authoritative (Proxmox VMs get `ipconfig0: ip=dhcp` on the operator's bridge and the
   platform touches nothing else network-side). Agentworks' obligations shrink to the artifacts it
   created (the cloned VM, the Lima instance) and their cleanup. Do not cargo-cult the cloud
   exposure machinery onto a platform in this category; it would be overreach.
3. **Local platforms** (local Lima, `wsl2`). The host is the operator's own machine and exposure is
   inherently host-local (Lima forwards guest SSH to a local port, `localPort: 0` in
   `LIMA_TEMPLATE`; WSL2 traffic rides `wsl.exe` into a NAT'd virtual network). There is nothing to
   arm or lift, which is why `secure_failed_vm` and `probe_failure_hint` correctly stay at their
   defaults here.

### The Platform Surface

The authoritative contract is `base.py`. An implementation supplies the ops, overrides only the
hooks its backend needs, and fills in the class-level contract methods consumed by the site decoder
and DB migration.

**Ops** (the mutation surface). Every op except `display_backend_name` takes the op-start
`RunContext` after `vm` (see the next section for what that is and how to read from it):

- `create(request, ctx) -> ProvisionResult` is deliberately **not** `@idempotent_op`: it runs a
  pre-flight collision check, then either raises `StateError` (all five in-tree platforms) or, for a
  soft-name backend, selects a different collision-free backend name and records that identifier in
  `platform_metadata`. A re-run must never target or replace an existing VM.
- `start(vm, ctx)`, `stop(vm, ctx)`, `delete(vm, ctx)` are flagged `@idempotent_op` and must land in
  the same place run twice as run once. The marker is inherited through the MRO, so an override does
  not restate the decorator. `reinit` re-applies everything and failed commands are retried, so the
  guarantee has to be real: `start`/`stop` on Lima and Proxmox and `stop` on WSL2 check `status()`
  first and short-circuit, because the backend verb is not reliably a no-op on an already-in-state
  instance; WSL2's `start` needs no guard because running a command boots a stopped distro and is a
  plain exec on a running one, and Azure and EC2 need no guard because their SDK start/stop calls
  are themselves idempotent on an already-in-state instance; `delete` treats already-gone as success
  on all five. `delete` is NOT unconditionally best-effort though: a delete that cannot remove the
  backend VM must raise a typed error (the manager deletes the DB row only on success, so a
  swallowed backend failure orphans the VM; #329). Azure enforces this with a post-teardown
  existence probe (`verify_vm_deleted`); only auxiliary-resource stragglers (its NIC/IP/NSG/disk
  sweep) stay warn-and-continue. Lima, WSL2, Proxmox, and EC2 do not yet verify; their teardown
  verbs remain fire-and-forget (tracked in #356).
- `status(vm, ctx) -> VMStatus` is a read-only query.
- `display_backend_name(vm) -> str` is pure display and takes no `ctx`.

**Transport and lifecycle hooks** (sensible defaults on `VMPlatform`; implementations override only
what their backends need). All are entered by callers that gate first, so on entry the VM is running
or was just started. The three transport hooks take `ctx: RunContext` for the same reason the ops
do: opening a route to a cloud VM is a backend call, so a platform reads any credential it needs
from `ctx.secret(name)` here exactly as in an op. Lima and WSL2 accept and ignore it (their
transports are local); Azure and EC2 use it:

- `native_transport(vm, ctx, *, config=None) -> Transport | None` (default `None`). The
  `agentworks.transports.native_transport` factory wraps the call in `transient_route`, probes
  reachability with an `echo ok` retry loop, and raises a typed `StateError` (using
  `no_native_transport_hint`) when a platform returns `None`. Lima returns a `limactl shell`
  transport, Azure and EC2 an `SSHTransport` against the VM's current public IP (Azure reads its
  persistent address live off the NIC; EC2 reads its address live off a fresh `describe_instances`,
  because EC2 reassigns the auto-assigned IP across stop/start, so it is never cached), WSL2 a
  `wsl.exe`-backed transport. Proxmox deliberately returns the default `None` and sets
  `no_native_transport_hint` to point the operator at the Proxmox web-UI serial console, because its
  guest-agent exec is one-shot and cannot host an interactive shell.
- `transient_route(vm, ctx, *, config=None) -> context manager` (default `nullcontext()`). Azure
  opens a scoped SSH route on enter (heals a missing public IP, converges the NSG onto the
  baseline-deny model, pokes this operation's own ephemeral allow rule scoped to the operator's
  egress prefixes) and deletes exactly that rule in a `finally`, bounding the exposure window to the
  transport's lifetime; concurrent native ops on one VM each own an independent rule, so they never
  cross-remove. EC2 does the same with a security-group ingress rule and needs no public-IP heal (a
  running instance receives an address automatically), but its rule model forces a divergence: an
  EC2 ingress rule's identity is its `(protocol, port, cidr)` tuple, not a name, so two concurrent
  routes from one operator egress share ONE rule rather than owning independent ones. The poke is
  therefore idempotent (tolerate `InvalidPermission.Duplicate`) and the per-op remove tolerant
  (tolerate `InvalidPermission.NotFound`), failing CLOSED (the deny baseline), never open; see
  `plugins/aws/network.py`. Those calls read the credential from `ctx`, so a credentials-configured
  site authenticates as itself with no ambient fallback.
- `vm_active(vm, *, config=None) -> context manager` (default `nullcontext()`). WSL2 returns a
  keepalive that holds the distro against Windows' idle-shutdown for the span of a command, with
  Win32 Job-Object orphan-proofing for a hard-killed `agw`. No `ctx`: every hold that exists is
  local and makes no backend call. A cloud hold that did would thread it like the transport hooks.
- `post_tailscale_ready(vm, ctx) -> None` (default no-op). The contract is "close provisioning
  access": it fires the instant Tailscale is reachable. Azure deletes the ephemeral bootstrap SSH
  allow rule here, leaving the VM with zero inbound exposure behind its permanent deny-all-inbound
  baseline (the public IP itself stays attached for the VM's whole lifetime); EC2 revokes exactly
  the bootstrap allow's tuples (recorded in platform_metadata at create) to the same end, so a
  concurrent native route's distinct allow survives. The asymmetry with `transient_route` is
  intentional: the bootstrap ingress opens inside `create()` (cloud-init needs inbound SSH from the
  operator), and neither that nor this closing point is context-manager-shaped.
- `secure_failed_vm(vm, ctx) -> None` (default no-op). Same contract as `post_tailscale_ready`, for
  the paths where a create is kept without completing Phase A: the bootstrap or Tailscale
  verification died (row marked FAILED) or the operator interrupted it mid-bootstrap (row status
  untouched); the success-only hook never fired on either. Azure deletes the fixed-name bootstrap
  allow and EC2 revokes the recorded bootstrap tuples, so the VM defaults to zero inbound exposure;
  debugging survives via `vm shell --platform` (a fresh per-operation allow) and the platform's
  serial console (not firewall-gated).

The two closing hooks and `transient_route` take `ctx` because opening or closing the firewall route
(an Azure NSG rule, an EC2 security-group rule) is a backend call; the caller (Phase A's
`bootstrap_vm` for the two closing hooks, the transports factory for `transient_route`) passes the
create/op's own scoped context, whose secrets are already resolved before Phase A begins, so even
the interrupt path never resolves a secret for the first time.

Callers must pass the context their composition root already built for the platform's ops
(`gated_vm_boundary` and `_live_vm_boundary` in `agentworks.vms.manager.boundary` both hand one out;
the activation gate builds its own from the gate's scoped reader), never a freshly constructed empty
one. `vm shell --platform` is the case that makes this load-bearing: on a running VM the gate's
happy path is a pure Tailscale reachability probe, so nothing has touched the platform with a
context before the transport is built. A platform must never depend on an earlier op in the same
process having warmed a credential cache.

**Gates** (cheap, offline, distinct from preflight):

- `unsupported_reason()` is a class-level, zero-arg classmethod run at every registry build. It
  answers "could any config of this platform ever work on this host," and is the **platform node's
  own** readiness in the fold. WSL2 is the only platform that overrides it (`"Windows only"` off
  Windows). Lima deliberately does not: an ssh-placed site runs `limactl` on the placement host over
  SSH and needs nothing locally.
- `not_ready(config) -> Readiness` (inherited from `Capability`, default ready) is a
  **non-constructing classmethod**: "is a site with THIS config ready," host-introspection only, no
  network or secrets, no instance built. A local Lima site with no `limactl` is not-ready; a remote
  one is not. WSL2 reports a site with no `wsl` on PATH not-ready even on Windows. The fold calls it
  off the graph-carried impl to fold into the vm-site's verdict.

**Class-level contract**. `contract_version`, `config_model`, `name`, and `description` are all
REQUIRED and none is defaulted, because a default would let an unmigrated implementation inherit a
claim it never made; registration refuses an implementation missing any of them, naming the plugin.
`contract_version` must match exactly the version the vm-platform descriptor declares supported, so
a contract change is a hard cutover rather than a silent re-certification.

- `config_model` declares what the platform's config IS (the keys a site writes beside `name` inside
  `spec.platform`), as an `AgwModel` carrying the platform's own name as a `Literal` tag plus one
  field per accepted key. The core validates against it (closed-world) and extracts the references
  its `SecretRef` / `ResourceRef` markers imply (total, never raising); no platform code runs for
  either. Proxmox marks its `token_secret` field; the marker is what later lets the op read that
  secret (below).
- `legacy_platform_metadata(cls, row, legacy) -> dict[str, str]` maps pre-migration DB rows into the
  `platform_metadata` shape, consumed only by the one-shot DB migration.

**Inputs and outputs** are uniform. Every `create` receives the same `ProvisionRequest` and returns
a `ProvisionResult` whose `platform_metadata` is written verbatim to `vms.platform_metadata` and
read back only by the owning platform (Lima stores `instance_name`, WSL2 `distro_name`, Azure
`resource_id`, Proxmox `vmid` + `node`, EC2 `instance_id` + `security_group_id` + `region` +
`backend_name`, and never the public IP, which it reads live). Add a platform-specific **input** by
adding a field to `ProvisionRequest`, not by changing the protocol. But note the opposite pattern is
also right: purely internal translation stays inside the platform. Azure's VM-size selection
(mapping the request's `cpus`/`memory_gib`/`disk_gib` onto a concrete SKU, with a site-level
`vm_sizes` override, per ADR 0018) lives entirely in `plugins/azure/platform.py` and adds nothing to
`ProvisionRequest`.

### How an Op Gets Its Dependencies

This is the part the orchestration-layer refactor (ADR 0019) changed most, and the part a platform
author most needs to get right.

**A platform instance never holds a value source of its own.** Construction binds only
`(owner_name, config)`. There is no resolver parameter, no bound secret reader, no client bridge
(all retired by ADR 0019). Everything a stage needs arrives through the `RunContext` handed to it.

`RunContext` (`../base.py`) is a frozen dataclass, rebuilt fresh per stage (never mutated, never
`replace()`'d). It carries `config` and `operation_scope` as plain fields, and grants power through
accessor methods rather than bare fields so a future permission model can gate them without changing
signatures: `admin_target()` / `agent_target()` return execution `Transport`s, and `secret(name)`
returns a resolved secret value. `ctx.secret(name)` raises a typed `ConfigError` if the context was
assembled without a resolve pass, and it is scoped: an op can read only the names its config model
marked.

What differs between stages is timing, not shape. `preflight` gets the command-start slice (existing
targets only, no resolved secrets, which is what makes it structurally dependency-blind); `runup`
and the ops get the op-start slice (current targets, resolved secrets). Central secret-resolvability
prediction happens above the platform AND above the `vm-site` node that holds it, in the operation's
preflight sweep (`orchestration.readiness.preflight_all`), which is why neither
`VMPlatform.preflight`/`runup` nor the node touches secret machinery. Whether a declared secret can
be attempted depends on the run (the active source chain and exact interaction policy), not on the
platform that named it. One visible consequence, and it is the intended one: `agw doctor` invokes
the node's preflight per row without a sweep, so a site whose credential is only obtainable by
prompting reads ok in the VM sites group, and resolvability is reported once, on that secret's own
row in the Secrets group.

**The pattern for a backend client:** memoize the _derived client_, never the raw secret. Proxmox's
`_api(ctx)` builds a `ProxmoxAPI` from `ctx.secret(token_secret)` on first need and caches the
client (`self._api_cached`), never the token. Any future platform with an API token (a hypothetical
GCP or AWS backend) should follow that shape.

### Credentials on a Cloud Platform: The Reference Shape

Azure is the worked example, and a new cloud platform should copy it rather than invent a variant.
The `aws-ec2` platform (`plugins/aws/platform.py`) is the first copy of it: its `access-key` arm is
the AWS analogue named below, with `access_key_id` as the plain identifier and `access_key_secret`
naming the secret that holds the secret access key (plus an optional `assume_role_arn`). Read it
alongside azure when adding the third. Four rules, in `plugins/azure/platform.py`:

**1. Authentication is a REQUIRED tagged union, one arm per mechanism.** The site's platform block
carries an `auth` table whose `mode` selects the arm:

```yaml
spec:
  platform:
    name: azure-vm
    subscription_id: "..."
    auth:
      mode: service-principal
      tenant_id: "..." # plain config: an identifier, not a secret
      client_id: "..." # plain config
      secret: azure-client-secret # the NAME of a secret, and the default
```

```yaml
spec:
  platform:
    name: azure-vm
    subscription_id: "..."
    auth: { mode: ambient } # the host's own credential chain, said out loud
```

Four deliberate choices. The field is REQUIRED with no omission alias, because the previous shape
(an optional credential table) expressed the choice by ABSENCE, and no document could tell "I
deliberately borrow the host's identity" from "nobody configured this yet"; neither could a
reviewer, `doctor`, or the dependency graph. The tag is a string `Literal` rather than a boolean,
because each mode carries its own fields and further mechanisms (azure managed identity, an AWS
profile or web identity) are new arms rather than another shape change. The identifiers are plain
config because they are identifiers, not credentials. And the field is `secret` and holds a NAME,
not `client_secret` holding a value, so nothing invites an operator to paste a live credential into
a plaintext file; the value resolves through the framework secret system like proxmox's
`token_secret`.

A union rather than an `auth_mode` enum beside nullable blocks, and the reason is mechanical:
pydantic emits a discriminated union directly as `oneOf` with a `discriminator` mapping and a
`const` per arm, so the loader and the emitted schema agree by construction. The enum alternative
needs a cross-field validator, and pydantic does not derive a validator's body into the schema it
emits, so the emitted schema would accept mixed-arm configs the loader rejects. The limit is that
derivation and not JSON Schema, which states such a constraint fine; it just has to be declared
rather than written as code. Under `manifests/emit.py`'s contract a schema may be more permissive
than the loader, so that is not unsound; what it forfeits is the DIAGNOSTIC, since a `tenant_id`
written under `mode: ambient` would draw no editor complaint and fail only at load.

Proxmox is deliberately NOT in this shape: it has one authentication mechanism, so it keeps its
required token fields with no mode selector. Add a union when there are two mechanisms to choose
between, not before.

**2. Mark the field, and let the core do both halves.** Extraction is total and non-throwing, so it
emits the edge whenever it can derive the secret NAME (even if the table's other fields are
malformed) and omits it only when the name itself is underivable. Validation is where every shape
error surfaces, including unknown keys inside the table. Marking the field is what puts the secret
in the site node's `secret_refs`, which is what gets it into the boundary resolve and therefore
delivered to `ctx.secret`, and the marker's `default_template` is where the well-known default name
lives.

**3. An explicit credential never falls back.** `_get_credential(ctx)` forks on the declared arm,
not on runtime luck: a `service-principal` site builds exactly that credential and a failure is
fatal; an `ambient` site gets the ambient chain (`DefaultAzureCredential` plus the browser
fallback). Falling back from a configured identity to an ambient one would run the operator's
command as somebody else, which is worse than failing. Cache the credential per instance: its
identity is fixed by the bound config, so one build and one probe serve every op.

**4. Probe once at build, and let runup pay for it.** Both credential paths make one live token
request when built, but they answer a failed probe differently, and the difference is the point. On
the SERVICE-PRINCIPAL path the probe is purely diagnostic: there is no fallback to choose, so a
failure becomes a typed error naming the site and the secret at the point of construction, rather
than a raw SDK exception from whichever call happened to be first. On the AMBIENT path the probe is
the fallback DECISION: a failure means nothing in the chain can authenticate, so it answers with an
unprobed interactive-browser credential and raises nothing. The platform's `runup` is where the
credential arm's error lands on the provisioning timeline, ahead of `create`, so a wrong credential
aborts `vm create` with nothing realized.

Two placement rules go with it. Client construction stays OUTSIDE the `try` that wraps SDK calls in
the platform error type: a typed credential failure is already the answer, and re-wrapping it strips
the hint (worse, a `status()` that degrades to UNKNOWN on any exception would swallow it entirely).
But keep the credential's own construction INSIDE the try that produces that typed error, alongside
its probe: SDKs validate constructor arguments eagerly, and a resolved secret that comes back empty
is reachable in a way config validation cannot catch.

Read `AzureAuth`, `_build_service_principal_credential`, and `_get_credential` together: that trio
is the whole pattern. On EC2 the analogous trio is `AwsAuth`, `_build_access_key_session`, and
`_get_session`; two things differ deliberately, both because the SDKs differ. First, `_get_session`
does NOT probe at build (boto3 sessions are inert), so the runup and status classify a definitive
credential rejection apart from an unreachable endpoint: azure-identity collapses an Entra rejection
and an unreachable Entra into one `ClientAuthenticationError`, so azure must treat every credential
failure as fatal, but botocore surfaces a rejection as a `ClientError` with an auth error code and
an outage as an `EndpointConnectionError`, so `aws-ec2` follows proxmox (runup makes a rejection
fatal and warns-and-continues on indeterminacy) and its `status` re-raises a rejection typed rather
than degrading to UNKNOWN, so a misconfigured site never reads as UNKNOWN in `vm describe` (the
exact #303 hole). Second, an `assume_role_arn` builds AUTO-REFRESHING credentials (botocore's
`AssumeRoleCredentialFetcher` + `DeferredRefreshableCredentials`) rather than a one-shot assume, so
a long op cannot fail with `ExpiredToken` from a frozen cache. See `test_platform_runup.py` and
`test_aws_ec2_ops.py` (directly under `cli/tests/`) for the halves.

### Exposure on a Cloud Platform: Baseline Deny, Ephemeral Scoped Allows

This is the category-1 obligation from the host-control categories above: a full-control cloud
platform owns the exposure surface, so it owns keeping it shut. An externally administered or local
platform has no business here and does none of it.

Azure and EC2 share the firewall model, but not the address lifetime. Azure keeps a persistent
public IP for the VM's whole lifetime. EC2 receives an auto-assigned public IP while running,
releases it on stop, and receives a different one on start, so Agentworks always reads it live. In
both cases, inbound exposure is controlled by firewall rules rather than using address attachment as
an access switch. The baseline is deny-all-inbound; SSH happens only through ephemeral rules on
TCP/22 scoped to the operator's detected public IPv4 address as a `/32`, plus any addresses or CIDR
ranges in `operator.ssh_allow_cidrs`. The rules open for the bootstrap window and each
native-transport session and close afterward. The shared operator-egress detection and the
`ssh_allow_cidrs` fold live in `capabilities/vm_platform/ssh_exposure.py` so both platforms use one
detector and one policy; the per-platform rule mechanics stay in each plugin's `network.py`.

One asymmetry is worth calling out because it drives the EC2 code. Azure must INSTALL an explicit
`deny-all-inbound` rule (an NSG carries permissive defaults, and the deny has to outrank any allow),
so its baseline is a rule it writes. An EC2 security group is the opposite: a group with no ingress
rules already denies all inbound, so EC2's baseline is the group's NATURAL empty state, with nothing
to install. That is why `plugins/aws/network.py`'s `create_security_group` authorizes no ingress at
all. The close hooks revoke exactly the bootstrap allow's recorded prefixes (not a blanket
revoke-all), so a concurrent `vm shell --platform` route's distinct allow survives (nothing
serializes commands per VM); the prefixes are recorded in platform_metadata at create rather than
recomputed, which would drift if the operator's egress or `ssh_allow_cidrs` changed. The other
EC2-native divergence (tuple-identity rules, so concurrent same-egress routes share one rule and the
poke/remove are idempotent/tolerant and fail closed) is covered under `transient_route` above and in
`network.py`.

### Resources on a Cloud Platform: Per-VM Lifecycle, Shared State Stays Ambient

One rule governs what a cloud platform creates: every Agentworks-managed resource should be scoped
to exactly one VM and share that VM's lifecycle. It is created during that VM's `create` and torn
down when the VM is deleted or when create rolls back, and nothing Agentworks makes outlives the VM
it belongs to. The shipped platforms hold to this. Azure gives each VM its own NIC, public IP, NSG,
OS disk, and even its own VNet (`{name}-vnet`, its own `10.0.0.0/16`), and the delete and rollback
sweep removes exactly that set; EC2 gives each VM its own security group, instance, and ENI. Per-VM
scoping is what makes teardown and rollback total: there is no shared thing a delete could
half-break.

Shared infrastructure gets the opposite treatment: assume it, do not manage it. The resource group a
VNet lives in, the VPC and subnet an instance launches into, are the operator's to provision, and
the platform only READS them. Azure requires a `resource_group` in config and its `runup` checks
that the group EXISTS, failing with a hint to create it rather than creating it silently; EC2 takes
an optional `subnet_id` (falling back to the account's default subnet) and existence-checks it the
same way, deriving the VPC from it. Neither creates or deletes shared infrastructure, because
deleting one VM must never risk something another VM, or the operator, still depends on.

If a platform genuinely must manage a shared resource itself (none of the shipped ones do; a future
backend might), two rules keep it safe:

1. **Agentworks owns it outright, and it is marked as such.** It carries the same `owner=agentworks`
   tag (or the backend's equivalent) that the per-VM resources carry, so it is unambiguously
   Agentworks-created and a future `doctor` sweep can find it. Never half-adopt a resource the
   operator also manages.
2. **It is re-ensured idempotently on every create, never created-once-and-remembered.** Every
   `vm create` runs the same get-or-create step, so a fresh account, a manually deleted shared
   resource, or a half-provisioned environment all converge on the next create. It is the same
   convergence shape Azure already runs for the NSG baseline on each transient route, just hoisted
   to a shared resource. Do NOT tear such a resource down on a single VM delete (another VM likely
   needs it); shared-resource teardown is an explicit operator action, not part of the per-VM
   lifecycle.

### The Provisioning Timeline: Create-Time Bootstrap vs Initialization

Standing up a VM splits into two stages with different owners and, crucially, different re-run
behavior. (These are a provisioning-timeline concept, orthogonal to the capability lifecycle stages
and the operator-facing command banners that the rest of the codebase calls "phases.")

- **Create-time bootstrap** is `create()` plus whatever the backend runs at creation time to get the
  VM reachable over Tailscale. It is baked into the backend's own create mechanism: Lima's
  `provision` block, Azure's and Proxmox's cloud-init user-data. The shared payload is
  `bootstrap_script.py` (admin user, packages, SSH key, swap, hostname, the Apple-vz SVE grub mask,
  Tailscale), delivered natively by Lima and via `cloud_init.py`'s `#cloud-config` wrapper by Azure.
  Lima is a deliberate split delivery: its retained provision script installs Tailscale but contains
  no resolved auth key, then `create()` sends the key through a fixed post-start guest command on
  stdin. The value is absent from provider-retained configuration and host-side argv; the guest
  `tailscale` process necessarily receives its `--auth-key` argument transiently. This is required
  because Lima copies provision scripts into its instance YAML, which `limactl list --json` can
  render. WSL2 is the exception: with no cloud-init-like mechanism, it runs the same bootstrap
  script over the provisioning transport during initialization instead, and structurally never joins
  Tailscale at create time (its `create()` does not branch on `tailscale_auth_key` at all). **This
  stage runs once, at create.**
- **Initialization** is `run_initialization` (`agentworks.vms.initializer`) plus VM hardening
  (`agentworks.vms.hardening`), run over a `Transport` against the created VM. It is
  platform-agnostic. **It is re-runnable and is exactly what `agw vm reinit` re-runs.** (The Phase A
  bootstrap/connectivity driver `bootstrap_vm` is provisioning, not this stage, and runs only at
  create.)

`request.tailscale_auth_key` is the seam control: when present, the platform joins Tailscale during
create-time bootstrap; when `None`, every platform defers the join to initialization.

A platform-native configuration that the provider retains must never contain the resolved key.
Transporting a template over stdin does not satisfy that rule when the provider copies the parsed
template into durable instance state. The provider-faithful test is the submitted configuration, not
Agentworks' temporary input path.

The seam between the two stages is the source of the most important gotcha below.

### What `reinit` Reaches and What It Does Not

Because create-time bootstrap is baked into the backend's create mechanism, **a change to it reaches
new VMs only.** `agw vm reinit` (`manager.reinit_vm`) re-runs initialization (`run_initialization`
-> the platform-agnostic setup and hardening) over a transport; it does not call `platform.create()`
again or re-run the backend's create-time user-data. So decide deliberately where a fix belongs:

- **Must reach already-provisioned VMs?** Put it in initialization, as an idempotent reconciliation
  step. The models are `initializer._preserve_ssh_host_keys` and the sysctl / `hidepid` steps in
  `agentworks.vms.hardening` (ADR 0012): each is written at create and re-applied on `reinit`,
  content-diffed so a steady-state VM produces no change. Initialization is platform-agnostic, so
  weigh whether the fix is truly generic before putting it there.
- **Genuinely platform-specific, and new-VMs-only is acceptable?** Put it in the platform's
  create-time provisioning and remediate existing VMs out of band. The Lima `subuid` cap took this
  route on purpose (below).

A subtler instance of the same seam: `skel.py`'s shell rc content is seeded into the admin user's
home exactly once by `bootstrap_script.py` at create, but written to `/etc/skel` on every `reinit`
by `initializer._write_skel_seeds` so future `useradd -m` inherits it. Same content, two writers,
two different re-run behaviors, on purpose.

A platform-specific fix that must also reach existing VMs via `reinit` signals that the initializer
may need a platform hook. None exists today; adding the hook is preferable to placing
platform-specific logic in the shared initialization path.

### Things to Keep in Mind

#### The Backend Is Not a Blank Slate: Watch What It Injects

The single biggest surprise with a new platform is that the backend creates its own users, groups,
ID ranges, mounts, and network config before Agentworks touches the VM, and those can collide with
assumptions Agentworks makes. A platform cannot assume a clean Debian image containing only its
bootstrap changes. Initial platform development should inventory what the backend injects and check
it against what Agentworks needs (notably: agent-user creation allocates a subordinate uid/gid block
per agent).

**Worked example (Lima `subuid` exhaustion).** Lima creates a guest user matching the host username
and, in its `rootless-base` boot script, grants that user a **1 GiB** (`1073741824`) subordinate
uid/gid range for `rootless` container tooling:

```sh
# Lima's boot.Linux/20-rootless-base.sh (abridged)
grep -qw "${LIMA_CIDATA_USER}" "$f" || echo "${LIMA_CIDATA_USER}:${subuid_begin}:1073741824" >>"$f"
```

That single entry starts at `524288` and runs past `login.defs`' `SUB_UID_MAX` (`600100000`),
swallowing essentially the entire allocatable space. Agentworks creates each agent as its own Linux
user with a plain `useradd`, which auto-allocates a `65536` subordinate block; once Lima's giant
range has eaten the space, `useradd` can no longer find a free block and **agent creation fails**.
The symptom is far from the cause: a VM that provisioned fine simply stops being able to add agents
after a handful.

The fix Agentworks ships caps any oversized range back to the standard `65536` in `lima.py`'s create
provision block (see the `subuid`-cap step in `LIMA_TEMPLATE`). The general lessons, which apply to
any future platform:

- Enumerate backend-injected users and ID ranges early. `cat /etc/subuid /etc/subgid` and
  `cat /etc/passwd` on a freshly created VM with no agents yet is a five-minute check that would
  have caught this at design time.
- A working first VM does not prove the platform is correct. This bug only appears after N agents.
- Corrections to backend state must account for the backend's own re-run behavior so the fix sticks.
  Lima's `grep -qw <user>` guard means a corrected entry is not re-added on reboot; a different
  backend might overwrite the correction on every start, which changes where the fix has to live.

#### A Create-Time Step That Needs a Reboot: The Restart Sentinel

Some bootstrap steps only take effect after a reboot, and rebooting mid-provision is unreliable.
Currently only the Apple-vz SVE grub mask needs this. The convention (`bootstrap_script.py`'s
`REBOOT_SENTINEL_PATH`) is that such a step drops a sentinel file on tmpfs; the platform's
`create()` probes for it after provisioning and restarts the instance once if present (Lima does
this). The probe stays why-agnostic: the host restarts on the sentinel without needing to know which
step set it, and the sentinel clears itself on the restart. A backend step that only lands after a
reboot should reuse this convention rather than introduce a second one.

#### No Host File Sharing by Default

Agentworks VMs are self-contained. Do not mount host directories into a guest unless there is a
concrete need: it is an attack surface and a portability trap. Lima defaults to sharing the host
home; `LIMA_TEMPLATE` sets `mounts: []` explicitly to guarantee none. Hold the same line on any new
platform, and prefer an explicit "no sharing" over relying on a backend default.

#### Platform Cleanup on Failure or Interrupt Is Not the Orchestrator's Unwind

A platform's `create()` may build several backend resources before one fails. The platform cleans up
its partial work in a best-effort sweep and re-raises (Azure's `create()` wraps NIC / IP / NSG /
VNet / disk creation and calls `cleanup_vm_resources` on any exception). The same obligation covers
an operator interrupt (`KeyboardInterrupt`) across the whole create span, including any inline
readiness or bootstrap wait: warn with "Ctrl-C again to abandon" guidance, tear down what this
create made, and re-raise the ORIGINAL interrupt; a second Ctrl-C abandons the cleanup loudly,
naming the exact manual removal (Azure's `rollback_create_on_interrupt`; Proxmox, Lima, WSL2, and
EC2 do the same over their single VM / instance / distro). That is distinct from, and composes
under, the orchestrator's DB-row unwind (ADR 0019's `RealizationLog` / node `teardown`), which rolls
back the persisted VM row. The platform's sweep undoes backend-side resources created inside
`create()`; the orchestrator undoes the Agentworks-side record on top of it.

#### Quoting and Escaping in Embedded Scripts

Platforms embed shell into YAML or cloud-init, sometimes through several layers (Python `.format`,
YAML block scalar, remote shell). Two traps that have already occurred:

- `str.format` templates: any literal `{` / `}` in embedded shell (an `awk` program, a `${VAR}`)
  must be doubled to `{{` / `}}`, or `.format` will treat it as a field.
- Render and parse before trusting it: a quick `yaml.safe_load` of the rendered template in a test
  catches brace and indentation mistakes that are otherwise found only at provision time. See
  `cli/tests/vms/test_lima_template.py` for the pattern.

### Adding a New Platform

1. A new implementation subclasses `VMPlatform` and implements the ops. Every op except
   `display_backend_name` takes `ctx: RunContext`, as do the three transport hooks; declared secrets
   are read via `ctx.secret(name)`, and the instance never holds a resolver or raw reader. A backend
   with a persistent client memoizes the derived client, not the secret (the Proxmox `_api`
   pattern). `create` is intentionally not `@idempotent_op`; the idempotent ops must land in-state
   themselves.
2. `config_model` declares the shape of the platform's own config block, with a `SecretRef` marker
   on each field naming a secret the implementation reads. Marking a field authorizes the op to read
   that secret later.
3. The class is registered in `VM_PLATFORM_REGISTRY` (`__init__.py`).
4. `unsupported_reason` identifies platforms that cannot run on some hosts (WSL2 off Windows), while
   the non-constructing `not_ready(config)` handles per-site tool checks (Lima with no `limactl`).
   `legacy_platform_metadata` is needed only when pre-migration rows must be mapped.
5. Only the transport and lifecycle hooks required by the backend override their defaults.
6. `bootstrap_script.py` / `cloud_init.py` supply the create-time payload instead of a
   platform-specific reinvention.
7. Dispatch, idempotency, and, where applicable, template-render tests belong under
   `cli/tests/vms/`; the next section lists the existing references.
8. The implementation is checked against the preceding platform-development considerations before it
   is considered complete.

### Testing

The existing tests under `cli/tests/vms/` are the templates to copy from:

- `test_platform_config_contract.py`: table-driven validation and edge extraction across all
  platforms, through the core entry points, plus a registry-name/class parity check. A good template
  for a new platform's registration test.
- `test_platform_support.py`: `unsupported_reason` (host-wide) vs. `not_ready` (per-site config) vs.
  the graph's stored `readiness_of` verdict (the fold composes the first two into the last). Uses
  the `stub_platform_support` fixture to pin platforms ready regardless of host, so dispatch-shape
  tests do not depend on local tooling.
- `test_platform_idempotency_guards.py`: patches `status` to an already-in-state value and asserts
  the backend verb is never called, proving the `@idempotent_op` contract.
- `test_platform_runup.py`: Proxmox's authenticated pre-check, distinguishing a definitive 401/403
  (fatal) from a transient error (warn and continue unverified). The template for any platform with
  a credential to verify.
- `test_create_vm_dispatch.py` and `test_create_reinit_orchestrated.py`: the `ProvisionRequest`
  shape handed to the platform, the persisted row, and the orchestrated create/reinit graph
  including the `RealizationLog` unwind and the activation gate.
- `test_lima_create_flow.py`: create-time provisioning wiring with mocked `limactl` and transport,
  the pattern for pinning platform create steps without a real VM.
- `test_lima_template.py`: `yaml.safe_load` over a rendered template with tripwires for the baked-in
  hardening rules (`mounts: []`, subuid cap present and first).

A new `Transport` subclass belongs under `cli/tests/transports/` alongside the platform.

### Cross-References

- [`../README.md`](../README.md): the capability lifecycle contract (read this first).
- `base.py`: the `VMPlatform` ABC, `ProvisionRequest`, `ProvisionResult`.
- `../base.py`: the `Capability` base and `RunContext`.
- `bootstrap_script.py`, `cloud_init.py`, `skel.py`: shared create-time payload.
- `agentworks.vms.initializer`: the two-phase init driver (`bootstrap_vm` for Phase A provisioning
  bootstrap/connectivity, `run_initialization` for Phase B initialization).
- `agentworks.vms.hardening`: the hardening steps, and the model for idempotent reconciliation that
  reaches existing VMs via `reinit`.
- `agentworks.vms.sites`: how a `vm-site` binds a platform to config.
- `agentworks.vms.nodes`: the `vm-site` / live-VM nodes that hold and drive a platform instance
  under the orchestration layer.
- `agentworks.transports`: the `Transport` ABC and the `native_transport` factory that wraps
  `transient_route`.
- `docs/guides/idempotency.md`: the canonical table of what `vm reinit` reconciles.
- ADR 0012: VM hardening at init.
- ADR 0016: the `vm-platform` capability / `vm-site` declarable split.
- ADR 0018: Azure VM size from spec.
- ADR 0019: the orchestration layer (command plans over node graphs) that drives the lifecycle.
