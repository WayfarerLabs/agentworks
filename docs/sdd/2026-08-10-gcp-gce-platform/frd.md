# GCP GCE VM platform: functional requirements

## Context

Agentworks ships cloud VM platforms for Azure and AWS but no Google Cloud implementation. Operators
need a Compute Engine site with the same declared capability, secret-source, bootstrap, lifecycle,
and discovery behavior as its siblings. Existing configuration and platform contracts remain
compatible. This effort also corrects the shared secret-value boundary so structured credentials can
retain their ordinary multiline representation while line-oriented consumers keep enforcing their
own transport constraints.

The implementation is a new opt-in `gcp` system plugin publishing `vm-platform/gcp-gce` and an
optional guest-side Google Cloud CLI install command. The plugin name identifies the vendor bundle;
the capability name identifies Compute Engine rather than reserving all future GCP mechanisms. The
existing `aws` vendor plugin also gains an optional guest-side AWS CLI install command in the same
publication correction.

Operator ruling, 2026-08-10: vendor plugins may grow beyond their first capability implementation.
Their vendor-level identity is the composition boundary; each service-specific implementation keeps
its own capability contract, model, and name.

## Requirements

### R1: plugin and capability identity

The shipped `gcp` system plugin is installed but disabled by default. It is a vendor bundle, not a
one-service architectural boundary: this release contributes one contract-v2 VM platform named
`gcp-gce` and one bundled `system-install-command` named `gcloud-cli`, while future GCP capability
implementations may join the same plugin under their own service-specific names. Disabled
contributions remain present with normal system-plugin provenance and tell the operator to enable
plugin `gcp` before use.

The `gcloud-cli` declarable installs the current Google Cloud CLI in a guest VM only when a template
references it. GCE provisioning itself remains SDK-driven and does not require `gcloud`; the guest
installer neither installs nor authenticates the operator host CLI.

The existing `aws` vendor plugin contributes one optional `system-install-command` named `aws-cli`.
It installs the current AWS CLI v2 in a guest VM only when a template references it. EC2
provisioning remains boto3-driven and does not require `aws`; the installer does not run
`aws configure`, create a guest credential profile, or alter operator-host authentication. Bundling
both cloud CLIs follows the established Azure CLI precedent and the operator's explicit request for
consistent optional guest tooling across shipped cloud-provider plugins.

### R2: declared site schema

The `gcp-gce` platform config has this closed-world shape:

- required `project_id` and `zone`;
- optional `subnet`, whose omission selects the project's `default` network;
- optional non-empty `machine_types` catalog override;
- an `auth` tagged union defaulting to `{mode: ambient}`.

Blank location strings and an empty catalog are invalid. Omitted outer `auth` selects ambient,
matching the existing vm-platform union convention; explicit-null `auth` is rejected. In the
service-account arm, omitted or explicit-null `secret` selects the well-known
`gcp-service-account-key` secret name; a blank name is invalid.

Each machine-type entry declares positive `cpus` and `memory`, a non-empty Compute Engine `type`,
and `arch` of `x86_64` or `arm64`. The built-in catalog starts with the x86 E2 shared-core
`e2-small` and `e2-medium` sizes, then continues through the E2 standard ladder. Selection is
order-independent and chooses the smallest entry satisfying both requested axes, using provider type
and architecture as deterministic tie-breakers when CPU and memory are equal. Shared-core entries
declare the two vCPUs exposed to the guest; their documented sustained aggregate CPU is lower and
must be taught as burstable capacity.

### R3: authentication modes

`auth.mode: ambient` uses Application Default Credentials and never reads a declared secret.

`auth.mode: service-account` has exactly one additional field, `secret`. It names a framework secret
whose value is one complete Google service-account JSON document. Client email, private key, token
URI, and every other credential field come from that document; none is duplicated in plain site
config. The site's common `project_id` remains the target project, not part of the credential arm.

The secret accepts the JSON document exactly as Google downloads it, including ordinary LF or CRLF
formatting. Operators do not have to compact, base64-encode, split, or otherwise rewrite the
credential before storing it. Secret sources do not trim its terminal line ending. Secret resolution
treats line breaks as opaque value content and continues to reject NUL. Consumers whose own syntax
is line-oriented, including environment, credential-line, and HTTP-header injection, reject
incompatible values at that consumer boundary with value-free diagnostics instead of imposing that
restriction on every secret. Operation composition performs that pure validation immediately after
delivery, preserving each path's existing resolve-to-mutation ordering; final sinks may repeat it as
defense in depth.

An explicit service-account rejection fails as that identity. It never falls back to ambient
credentials. Malformed JSON, missing service-account fields, SDK errors, logs, diagnostics, and the
full exception graph must not reflect the secret value.

### R4: authenticated runup

Before the database row or any GCP resource is created, runup builds the configured credential and
performs authenticated, read-only checks for the target project/zone and selected network:

- a configured subnet must exist in the zone's region;
- omitted subnet requires the project's `default` network;
- a missing default network is a typed pre-mutation configuration failure.

Runup requires `AFTER_CLASSIC_FIREWALL` network-policy order and rejects universal classic VPC
priority-zero ingress allows plus a universal priority-zero deny that blocks the operator's TCP/22
route. Create repeats those checks for the derived Agentworks tag before mutation. Organization and
folder firewall policies are not reliably discoverable with the site's Compute permissions; this
first implementation supports only projects without a higher-level terminal ingress allow or a
conflicting operator-SSH deny and teaches that prerequisite explicitly.

Secret resolvability remains owned by the operation's central preflight sweep. The platform does not
resolve or probe secrets in `not_ready` or `preflight`.

### R5: complete-or-raise create

`GCEPlatform.create()` conforms to vm-platform contract v2. It receives the required resolved
Tailscale key and progress sink, creates the backend, completes its declared bootstrap and stdin
join, and returns an optional discovered Tailscale IP. Readiness, bootstrap, join, or cleanup
failure raises within the create rollback window. It cannot return an incomplete state or select a
generic fallback.

### R6: provider-retained payload stays credential-free

The final `instances.insert` request may retain the admin public key, a startup script, network
metadata, and non-secret identifiers. It must not contain the Tailscale key or service-account JSON.
The startup script installs and configures the machine without a Tailscale credential. A durable
success marker makes it run once: later boots exit before replaying package or account mutations,
while an interrupted/failed first run without the marker may retry. After the script reports local
completion, the shared ephemeral bootstrap helper delivers the Tailscale key exactly once through
fixed stdin over the provisioning SSH transport.

The retained request explicitly attaches no guest service account or OAuth scopes. Explicit auth is
for the operator-side client only and never widens the guest's authority.

The UTF-8 encoded startup-script metadata value must remain at or below GCE's 256 KiB limit. Request
construction fails before mutation if the credential-free wrapper exceeds that bound.

GCP becomes the sixth provider-shaped durable-surface test beside Lima YAML, WSL2 staging, Proxmox
guest-agent staging, Azure custom data, and EC2 user data.

### R7: networking and exposure

Create attaches one ephemeral external IPv4 access config and retains it for the VM lifetime so
ordinary internet egress, Tailscale, initialization downloads, and workloads do not depend on Cloud
NAT. The instance carries an Agentworks-owned network tag. An owned priority-1 deny overrides the
default network's SSH, RDP, ICMP, and internal ingress allows for that tag, while an owned
priority-0 allow admits TCP/22 only from the operator's resolved SSH prefixes. The permanent rule
denies all ingress, not only SSH, so the public address does not weaken the VM platform's
zero-inbound baseline. The interface is explicitly IPv4-only and the deny covers `0.0.0.0/0` with
protocol `all`.

Before mutation, the platform requires classic VPC firewall rules to be evaluated before global and
regional network firewall policies, rejects every applicable priority-0 ingress allow that would
bypass the owned deny, and rejects an applicable priority-0 deny that overlaps operator TCP/22. A
higher-level organization or folder terminal rule remains an explicit unsupported project
prerequisite because ordinary site credentials cannot reliably inspect it.

After Tailscale verification, the success hook removes only its scoped allow. The deny rule and
external access config remain with the VM. A platform-native shell creates a unique per-operation
priority-0 allow, leaves concurrent route allows untouched, and removes only its own rule on exit. A
kept failed VM is secured through the same best-effort close behavior and retains console/manual
recovery guidance.

### R8: lifecycle and identity

The platform supports create, start, stop, delete, status, display name, collision checks, native
transport, transient platform shell, `post_tailscale_ready`, and `secure_failed_vm` with the same
idempotency and error taxonomy as Azure/AWS. Platform metadata contains stable GCP identifiers and
owned firewall names, never credentials or a cached public IP.

Create uses the smallest selected machine type, then verifies its provider-reported CPU and memory
before choosing the matching Debian 12 image. When GCE populates its optional output architecture,
that value must also match the declaration; omission is unknown and leaves the declared catalog
authoritative. A live type with a populated zero Persistent Disk capacity or required accelerators
fails with actionable configuration guidance before the first provider mutation; an omitted
output-only capacity field is unknown and proceeds to the provider insert. GCE exposes no read-only
complete machine/disk-pair validator, so a residual incompatibility rejected by `instances.insert`
fails definitively with fixed prerequisite plus machine/`pd-balanced` guidance and runs the normal
bounded rollback. Create uses a balanced persistent boot disk sized from the VM template and
explicitly marked `auto_delete`, instance-metadata SSH keys with project keys blocked, and
deterministic GCE-valid instance, tag, and firewall names. Exact bounded SHA-256-based derivations
make every retained identity collision-safe for underscores, leading digits, case, invalid runs,
suffix reservation, and truncation. Start/stop guard on live state to enforce idempotency, and
public IP reads always use live provider state.

### R9: failure and interrupt semantics

Every mutation is inside the create rollback span. Ordinary failure removes the partial instance and
owned firewall rules while preserving the original typed, secret-free error. The scoped allow is
always closed. The deny is deleted only after instance absence is proven; if deletion fails or is
indeterminate, it remains protecting the surviving VM. The first operator interrupt performs the
same bounded rollback and re-raises the original interrupt. A second interrupt abandons cleanup
promptly and names the project, zone, instance, firewall rules, and manual removal actions.

A completed Google operation with a structured failure is definitive, even when the SDK surfaces an
HTTP-shaped exception from `result()`. The observed `ZONE_RESOURCE_POOL_EXHAUSTED` code becomes a
typed capacity failure. A zonal instance operation names the selected zone and tells the operator to
retry later or choose another zone; a global operation says only to retry later rather than falsely
attributing its capacity to the VM zone. Other completed failures remain definitive and cannot be
reconciled into success. Inspect-before-retry guidance is reserved for waits whose completion and
outcome cannot be established. Provider messages, details, credentials, and exception objects never
enter the safe diagnostic graph.

Deletion first makes a best-effort close of the provisioning allow, then attempts VM deletion even
if that close fails, and deletes the deny only after VM absence is proven. It is idempotent for
already-gone resources. A surviving or indeterminate VM is a typed failure so its database row
remains available for retry. Auxiliary firewall residue after proven VM absence may be reported
precisely without replacing a more important primary failure.

### R10: operator discovery and documentation

`resource list`, `describe-kind`, schema emission, guide topics, samples, and plugin enablement show
the new plugin, `gcp-gce` platform, and both `gcloud-cli` and `aws-cli` install commands from their
authoritative descriptors and manifests. Permanent docs teach both auth modes, the default-network
behavior, the service-account secret format, provisioning exposure, required IAM/API setup, an exact
downloaded-key-to-secret workflow without compaction, optional guest CLI use, AWS guest-tooling
boundaries, and recovery. Shared secret docs teach multiline values as ordinary opaque content and
locate line-safety enforcement at the consumers that need it. Shell completion remains
registry-driven; tests prove the new names are discoverable without a bespoke completion branch.

### R11: verification and live acceptance

Offline tests cover registration, multi-contribution plugin publication, `gcloud-cli` and `aws-cli`
payloads plus disabled/enabled recipe gating, schema discrimination/defaults, secret references,
auth failure, client caching, size/image selection, request retention, fixed stdin, lifecycle,
rollback, interrupts, cleanup survivors, exposure hooks, output/log/exception non-reflection, guide
rendering, startup-script size enforcement, indeterminate firewall inserts, pre-classic policy
ordering, priority-zero allow/deny conflicts, exact LF/CRLF service-account JSON through the real
secret resolver, NUL rejection, sink-local line-safety failures for environment, Git credential,
Proxmox HTTP-header, and Tailscale consumers, and full repository gates. Create and Tailscale rekey
tests prove an incompatible line-oriented secret fails before any DB, provider, daemon, or
durable-material mutation.

Provider-shaped operation tests distinguish DONE HTTP 503 failures carrying the exact structured
`ZONE_RESOURCE_POOL_EXHAUSTED` code, DONE failures carrying an unknown or malformed structured
shape, longer strings containing the capacity token, and timeout/non-DONE waits. They prove exact
equality is required for capacity classification, only the last case is indeterminate, definitive
failures cannot reconcile to insert success, zonal capacity guidance names the zone while global
guidance does not, no path reflects provider text, and every resulting exception graph is detached
and secret-free. Rollback tests interrupt an ordinary-failure cleanup only after at least one owned
resource is removed, then prove the second idempotent pass converges with the first interrupt
object's identity or, on a second interrupt, reports exact retained provider identities and manual
recovery actions.

One operator-approved live acceptance run creates and initializes a bounded VM, verifies Tailscale
reachability and platform lifecycle, queries the realized instance to prove that no guest service
account or OAuth scopes were attached, deletes it, and confirms zero GCP firewall, instance, disk,
and external-address residue. Live credentials and cloud state are never used without that explicit
gate.

## Non-goals

- Other GCP compute mechanisms, managed instance groups, GPUs, Spot VMs, reservations, shared VPC
  host-project indirection, OS Login, custom images, IPv6, static external IPs, or service account
  attachment to the guest.
- Binary secret values, a new secret encoding, secret persistence, or a new secret-lifecycle
  framework. This correction keeps the existing string-valued resolver and moves only the over-broad
  CR/LF rejection to the line-oriented consumers that require it.
- A provider-specific Agentworks CLI command family or imperative site configuration. The bundled
  guest-side `gcloud-cli` and `aws-cli` install commands are ordinary plugin data, not new
  Agentworks command families.
- Supporting a project without the Compute Engine API, target network, or required IAM permissions
  by mutating around the missing prerequisite.
- Projects where an organization/folder firewall policy terminal-allows ingress before VPC rules or
  denies the operator's SSH route, or where any priority-zero VPC ingress allow applies to every
  instance or the Agentworks tag. The setup guide states this security boundary and the platform
  rejects the VPC-level conflicts it can inspect.

## Definition of done

`vm-platform/gcp-gce` and `system-install-command/gcloud-cli` are normal disabled-by-default
contributions of the extensible vendor plugin `gcp`, and `system-install-command/aws-cli` is an
equivalent contribution of the existing `aws` vendor plugin; both auth modes and the reviewed schema
are enforced; create is complete-or-raise with credential-free retained metadata and one fixed-stdin
join; lifecycle and rollback are provider-shaped and secret-free; neither guest CLI is a
provisioning or authentication dependency; the exact downloaded multiline service-account JSON is
accepted without rewriting while line-oriented sinks fail safely; docs, samples, guide, and
completions agree; offline gates and reviews pass; operator-gated live acceptance leaves zero
residue; the SDD is locked truthfully.

-- agw-ns-gcp-platform (effort lead)
