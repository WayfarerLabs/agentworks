# GCP GCE VM platform: high-level architecture

## Placement

The plugin lives at `cli/agentworks/plugins/gcp/` and uses the same vendor-bundle shape already
proven by Azure:

- `__init__.py`: one `Plugin(name="gcp")` descriptor contributing `GCEPlatform` and anchoring
  bundled manifests;
- `manifests/install-commands.yaml`: optional guest-side `gcloud-cli` system install command;
- `config.py`: auth union, site model, machine-type catalog and selection;
- `auth.py`: ambient/service-account credential construction and secret-free error mapping;
- `network.py`: Compute API error mapping, external access, firewall, rollback, and cleanup helpers;
- `platform.py`: contract-v2 VM lifecycle and `TopicProse`.

The installed plugin index imports `gcp` beside `aws` and `azure`. Shared bootstrap generation,
ephemeral stdin join, SSH-prefix detection, capability registration, and manager lifecycle stay in
core.

The plugin identity names the Google Cloud vendor bundle, while `gcp-gce` names one capability
implementation. The descriptor may later add independently named GCP implementations under existing
capability contracts without changing `GCEPlatform` or introducing a provider-wide base class. For
example, any future GCP secret backend must conform to the contracts designed in
`docs/sdd/2026-08-07-secret-sources/`; this effort does not design or reserve that implementation.
Plugin enablement remains bundle-wide; each contributed row is consumed only when an operator
resource references it.

## Reviewed schema

The expensive public surface is intentionally small:

```yaml
apiVersion: agentworks/v1
kind: vm-site
metadata:
  name: gcp-dev
spec:
  platform:
    name: gcp-gce
    project_id: my-project
    zone: us-central1-a
    subnet: app-subnet # optional; omit for the default network
    auth:
      mode: service-account
      secret: gcp-service-account-key
    machine_types: # optional override
      - cpus: 4
        memory: 16
        type: e2-standard-4
        arch: x86_64
```

The ambient form is either omitted or explicit:

```yaml
auth: { mode: ambient }
```

Pydantic models are:

- `GcpAmbientAuth(mode: Literal["ambient"])`;
- `GcpServiceAccountAuth(mode: Literal["service-account"], secret: SecretRef)` where the marker's
  `default_template` is `gcp-service-account-key`;
- discriminated `GcpAuth`, default `GcpAmbientAuth(mode="ambient")`;
- `GcpMachineType(cpus, memory, type, arch)`;
- `GcpGCEConfig(name, project_id, zone, subnet, machine_types, auth)`.

Exact field behavior:

| Field                   | Type/default                                             | Null and validation behavior                                       |
| ----------------------- | -------------------------------------------------------- | ------------------------------------------------------------------ |
| `name`                  | literal `gcp-gce`, required                              | null/other names rejected                                          |
| `project_id`            | non-empty string, required                               | null/blank rejected                                                |
| `zone`                  | non-empty string, required                               | null/blank rejected                                                |
| `subnet`                | non-empty string or null, default null                   | null/omitted selects `global/networks/default`; blank rejected     |
| `machine_types`         | non-empty list or null, default null                     | null/omitted selects built-in catalog; empty list rejected         |
| `auth`                  | discriminated union, default `{mode: ambient}`           | omitted selects ambient; null/unknown/mixed arms rejected          |
| `auth.secret`           | non-empty secret name, default `gcp-service-account-key` | null/omitted inside service-account selects default; blank rejects |
| machine `cpus`/`memory` | positive integers, required                              | zero, negative, null, and non-integers rejected                    |
| machine `type`          | non-empty string, required                               | null/blank rejected                                                |
| machine `arch`          | literal `x86_64` or `arm64`, required                    | null/other values rejected                                         |

No credential identifier is split out of the service-account document. The union's `secret` names
one secret value containing the complete JSON document. `project_id` is common because it identifies
where the site operates under either credential mode.

The field is named `subnet`, not `subnet_id`, because GCE resolves a subnetwork by name within the
region derived from `zone`; callers do not provide a provider-generated identifier.

The immutable built-in catalog is `(2, 8, e2-standard-2, x86_64)`, `(4, 16, e2-standard-4, x86_64)`,
`(8, 32, e2-standard-8, x86_64)`, `(16, 64, e2-standard-16, x86_64)`, and
`(32, 128, e2-standard-32, x86_64)`, with memory in GiB. An override may name Arm types and selects
the `debian-12-arm64` image family rather than `debian-12`. E2/x86 is the default because its broad
zone availability makes a portable built-in catalog; T2A/Arm availability is limited to a narrower
zone set and remains an explicit override. Unsupported size requests fail before mutation.

## Dependency and credential boundary

The reviewed latest stable floors are `google-cloud-compute>=1.50.0`, `google-auth>=2.56.3`, and
`google-api-core>=2.34.0`. The implementation imports all three directly, so all three are direct
dependencies and enter the lock file.

Ambient construction uses Application Default Credentials with the cloud-platform scope.
Service-account construction parses `ctx.secret(auth.secret)` as JSON and calls
`Credentials.from_service_account_info`. Parsing and credential validation happen inside a narrow
builder that does not log, cache, or chain the raw value or parser exception. It returns only the
derived credential, cached once per platform instance. One derived Compute client is cached per
concrete client kind; requests carry the site's project explicitly. The raw JSON string and parsed
mapping are not retained by the platform.

No fallback crosses modes. Client construction and runup failures name the site, mode, secret name
when applicable, and remediation, but never the value.

The env-var source returns the configured value unchanged rather than stripping terminal CR or LF.
Secret resolution then preserves carriage returns and line feeds as ordinary string content and
rejects NUL before a value becomes resolved. The explicit arm therefore receives the complete Google
JSON document through the ordinary secret-source chain exactly as stored. The path and document
never enter the site manifest, and no compaction or alternate encoding is required.

This is a shared contract correction rather than a GCP exception. `secrets/resolve.py` owns the
source boundary and rejects only the value shape that cannot be represented by the process and
string-oriented runtime, NUL. Consumers own narrower syntax constraints:

- environment composition and reveal reject a resolved secret containing CR, LF, or NUL before it
  can enter SSH `SetEnv`, tmux environment arguments, a shell assignment, or tabular output;
- Git credential material rejects CR, LF, or NUL before an authenticated header probe or
  `~/.git-credentials` line is built;
- Proxmox explicit auth rejects CR, LF, or NUL with fixed typed text before its API client can build
  the HTTP `Authorization` header, and its exception graph cannot retain the value;
- Tailscale bootstrap rejects CR, LF, or NUL before appending its one command-owned stdin newline;
- SDK credential consumers, including GCP service-account JSON, receive the opaque string unchanged.

Every rejection names only the secret reference or consumer and uses fixed remediation. No error,
outcome, log, render input, or exception graph includes the value. Secret verification may prove a
multiline value resolvable because it does not transport or reveal the value. The permanent secret
contract and the SSH environment ADR record this source-versus-sink ownership split.

Moving validation out of resolution does not move failure later than its existing ordering. Each
line-oriented path validates its delivered values through a pure consumer-owned helper immediately
after that path's resolve. VM create validates the Tailscale key and Git tokens before DB insertion
and platform create; rekey validates the Tailscale key before status-dependent daemon
restart/logout; eager Git and environment paths validate before their first mutation or transport;
Proxmox validates while constructing the runup client before create mutation. The sanctioned
conditional Tailscale repair keeps its lazy contract: a healthy or already-running path never
resolves or prompts for the repair key, while a stopped VM may start before the gate discovers that
repair is required. That late path validates immediately after its conditional delivery and before
any rejoin route, transport, install, or daemon action. Final Tailscale and Git material sinks
retain the same guard as defense in depth so direct callers cannot bypass the invariant.

## Runup and resolution

The operation graph extracts `auth.secret` from the model and resolves it before platform runup.
`GCEPlatform` has no secret-aware `preflight` or `not_ready` override.

Runup builds the selected credential and performs read-only Compute API calls:

1. validate the project and zone are addressable;
2. if `subnet` is set, resolve it in the region derived from the zone and retain its network URL;
3. otherwise resolve `global/networks/default` and fail typed if it does not exist.
4. require the network's `networkFirewallPolicyEnforcementOrder` to be `AFTER_CLASSIC_FIREWALL`;
5. list classic VPC firewall rules and reject a universal priority-zero ingress allow, plus a
   universal priority-zero deny whose source and protocol overlap the operator's TCP/22 provisioning
   route. Runup has no per-VM request, so create repeats the check against the derived tag before
   its first mutation.

Create repeats any live lookup it needs rather than relying on mutable runup cache state. Every
lookup that can reject configuration, including collision, machine type, image, network, operator
SSH prefixes, and firewall-name collision, finishes before the first mutation.

## Create sequence

The complete-or-raise sequence is:

1. Select the smallest machine type satisfying `ProvisionRequest.cpus` and `memory_gib`, ordered by
   `(cpus, memory, type, arch)` so equal-shape catalogs remain order-independent; resolve the live
   machine type and verify its CPU and memory match the catalog declaration. If GCE populates the
   output-only architecture field, it must also match; an omitted value is unknown and leaves the
   declaration authoritative. Resolve `projects/debian-cloud/global/images/family/debian-12` or
   `projects/debian-cloud/global/images/family/debian-12-arm64` and the zonal `pd-balanced` disk
   request.
2. Derive every retained identity from `request.hostname` with the exact formulas in the provider
   LLD. The instance name uses lowercase RFC1035 normalization plus a ten-hex SHA-256 suffix
   whenever normalization or truncation changes the input. The network tag and stable allow/deny
   names always reserve their role suffix and independent ten-hex digest inside their 63-character
   limits.
3. Build a credential-free startup wrapper around the shared bootstrap generator. It exits
   immediately when `/var/lib/agentworks/gce-bootstrap-v1.complete` exists, otherwise runs bootstrap
   and atomically writes the marker only after full success. Reject a UTF-8 encoded metadata value
   larger than 256 KiB before mutation.
4. Create a priority-1 deny for all ingress targeting the instance tag. Create a unique priority-0
   TCP/22 provisioning allow for only the operator prefixes.
5. Insert the instance with a unique request UUID, one lifetime ephemeral external IPv4 access
   config, the target tag, an explicit `IPV4_ONLY` interface, metadata SSH key, project-key
   blocking, OS Login disabled for this instance, and the key-free startup script. Accept only an
   operation whose client request ID and target identity match the realized instance. The boot disk
   is `auto_delete=True`; `service_accounts` is explicitly empty.
6. Wait for the operation and RUNNING state, then read the external IPv4 address live and construct
   the provisioning `SSHTransport`.
7. Use `EphemeralTailscaleBootstrap` to wait for SSH and the local GCP startup marker, deliver the
   required Tailscale key once through the shared fixed stdin command, and best-effort discover the
   Tailscale IP.
8. Return `ProvisionResult(native_transport, platform_metadata, tailscale_ip)` only after the join.

The final insert body is retained provider state, so a provider-shaped test inspects the fully built
request object. It must contain neither the Tailscale sentinel nor the service-account JSON
sentinel, must set boot-disk auto-delete, and must attach no guest service account or scopes. The
only Tailscale-bearing call is the fixed command's `input_text`. A boundary test pins the encoded
startup-script value at 256 KiB and rejects 256 KiB plus one byte before any API mutation.

## Narrow shared readiness seam

Azure and AWS wait for `cloud-init status --wait`; GCP uses the supported Compute Engine
`startup-script` metadata mechanism. The shared `EphemeralTailscaleBootstrap` therefore accepts a
non-secret readiness command/label, defaulting to its current cloud-init command. GCP supplies a
fixed blocking command that polls the durable local marker until the helper's existing timeout.
Tests pin SSH becoming ready before the marker, no key delivery during the wait, eventual delivery
after the marker, and typed timeout. Retry bounds, fixed stdin delivery, IP discovery, exception
behavior, and secret handling remain shared.

This is the only new shared seam. It represents a real provider readiness difference and does not
add a delivery mode, callback framework, or GCP branch to the manager. The schema-review gate should
approve this seam with the config shape before implementation begins.

GCP's create path is a multi-step in-create bootstrap, so it actively uses the required progress
sink for secret-free provider, readiness-marker, and stdin-join milestones and sanitized output. It
does not treat the sink as an unused compatibility argument or emit raw SDK/request objects.

## Exposure lifecycle

GCP's default network commonly contains priority-65534 broad SSH, RDP, ICMP, and internal ingress
allows. The instance therefore receives an Agentworks tag plus an owned priority-1 all-ingress deny.
The scoped priority-0 TCP/22 operator allow is evaluated first for its source ranges; every other
classic VPC ingress source reaches the deny. The interface is `IPV4_ONLY`; the deny uses source
`0.0.0.0/0` and protocol `all`. Runup rejects any applicable priority-0 ingress allow because it
could bypass the baseline. It also rejects an applicable priority-0 deny that overlaps an operator
prefix on TCP/22 because equal-priority deny wins and would make provisioning fail after mutation.

The VPC network must report `AFTER_CLASSIC_FIREWALL`, making the owned classic deny terminal before
global or regional network firewall policies. `BEFORE_CLASSIC_FIREWALL` is rejected before mutation.
Organization/folder firewall policies still evaluate before the VPC and can terminal-allow ingress
or deny operator SSH. This version does not add organization-policy discovery or permissions, so it
supports only projects without either higher-level conflict. Setup docs and live inventory make that
operator-owned prerequisite explicit and narrow the claim accordingly.

`post_tailscale_ready` deletes the provisioning allow. `secure_failed_vm` attempts the same close
without replacing the primary failure. The lifetime external access config remains for outbound
internet and Tailscale; the deny remains stable platform metadata and is removed only after VM
absence is proven.

`transient_route` creates a fresh UUID-suffixed priority-0 allow, yields bounded route state, and
removes only that rule in `finally`; `native_transport` then live-reads the external IP inside the
context. Concurrent native routes have distinct rules, so closing one cannot close another. A
partial open rolls back its own rule, and a partial close warns with exact manual coordinates. The
existing VM-platform interface is sufficient.

## Lifecycle mapping

- `status`: live `instances.get`, mapping provider states to Agentworks states; not-found is
  `VMStatus.UNKNOWN` because the shared enum has no deleted member.
- `start` / `stop`: live status guards avoid invalid-state provider calls and make already-running
  or already-stopped success; real transitions use bounded operation waits.
- `delete`: close the provisioning allow, request instance deletion, verify absence, then delete the
  deny. Already-gone is success. A surviving/indeterminate instance raises typed and keeps the deny;
  auxiliary rule residue after proven absence warns precisely.
- `display_backend_name`: `<instance>@<zone>`.
- `platform_metadata`: instance name and provider ID, project, zone, canonical network and optional
  subnet URLs, normalized provisioning SSH source prefixes, network tag, allow/deny rule names and
  provider IDs, and access-config name. It excludes guest/external IP addresses and credentials.
  Later lifecycle and cleanup use these persisted network, source-prefix, and provider identities
  rather than mutable site configuration or observed same-name resource shape.
- native transport: live external IP from the lifetime access config; `transient_route` owns only
  the per-operation scoped inbound allow.

The external IP is never cached because GCE may replace an ephemeral address across stop/start.

## Rollback and errors

Firewall creation and instance insertion are one guarded mutation region. Each insert carries a
unique request UUID. A pre-response indeterminate firewall failure is retried once with that same
UUID; a definite already-exists response is a collision. Success requires an operation whose
`clientOperationId`, operation type, and target link match this attempt, and whose `targetId` equals
the realized firewall provider ID. Operation ownership is retained before waiting so an interrupt
during the wait can still reconcile safely. Rollback deletes only when that provider ID and the
complete network, direction, target, priority, source, and allow/deny shape still match. A missing
rule is absent; missing ownership proof or any mismatch is retained and reported as a collision. GCE
has no firewall resource-ID precondition on name-based delete, so this closes the ordinary
concurrent insert race but does not claim atomic protection against a hostile delete/recreate
between the verification read and delete. Ordinary exceptions first close the scoped allow, then
request bounded instance deletion even when allow close fails. The deny is deleted only after
`instances.get` proves absence; deletion failure or indeterminate timeout retains it around the
possible survivor. Cleanup raises an existing Agentworks error category where one fits, preserving
secret-free diagnostics and exact retained identities. A first `KeyboardInterrupt` runs the same
rollback and re-raises the original object. A second interrupt stops waiting, reports
project/zone/instance and both firewall rules, retains the deny when needed, and preserves the first
interrupt. Manual delete commands are emitted only for resources whose provider IDs still match the
persisted owned IDs; collisions or unknown identities receive inspect/escalate guidance without a
name-based delete recommendation.

A `KeyboardInterrupt` raised during the ordinary-failure rollback is the first operator interrupt,
not an unhandled cleanup failure. The ordinary rollback helper routes that exact object into the
same idempotent bounded interrupt rollback path for one more cleanup attempt. A second interrupt in
that attempt is the only abandon signal and still reports exact provider-ID recovery coordinates.

Every later stable-allow cleanup reconstructs the original expected shape independently from the
persisted canonical network, target tag, normalized provisioning prefixes, and fixed firewall
contract. A live same-ID rule is not its own expectation; any shape change is retained as a
collision.

Google API authentication/permission, not-found, quota, capacity, collision, operation, readiness,
and cleanup failures map to existing Agentworks error categories where one fits. A completed
extended operation with a nonempty structured error is definitive even when `result()` raises an
HTTP transport-shaped exception. `GCEOperationError` represents a definitive completed-operation
failure, `GCECapacityError` is its typed capacity specialization, and
`GCEIndeterminateOperationError` alone represents a wait whose completion and outcome cannot be
established. The safe capacity allowlist initially contains exactly `ZONE_RESOURCE_POOL_EXHAUSTED`.
Classification requires the already-returned operation's cached
`operation.status == compute_v1.Operation.Status.DONE`; it must not call `operation.done()` or make
another provider refresh after the bounded `result()` wait. It then reads only
`operation.error.errors[*].code`; it never stringifies the provider message, details, error object,
or caught exception. That exact code maps to `GCECapacityError` with the caller-supplied zone and
retry-later-or-select-another-zone guidance. A DONE operation with an unknown code, missing entries,
or malformed structured shape maps to generic definitive `GCEOperationError`. A timeout or transport
failure with no DONE structured outcome maps to `GCEIndeterminateOperationError` and
inspect-before-retry guidance.

Instance and firewall insert callers reconcile only `GCEIndeterminateOperationError`; matching live
state may establish success for that outcome. They immediately propagate definitive
`GCEOperationError` and `GCECapacityError` into create rollback, even when a matching resource can
be read. Start and stop propagate every wait failure. Delete and rollback may inspect final provider
state after a wait failure because verified absence, rather than operation classification, is their
authoritative postcondition; they never turn a surviving or mismatched resource into success.
Provider exceptions are sanitized before chaining; errors that may retain request or credential
objects are not attached as cause/context. No provider error text is allowed to reflect
service-account JSON or the Tailscale key.

## Discovery and documentation

Registration drives capability rows, config schema, `describe-kind`, guide topics, and resource
samples. The same plugin manifest publishes `system-install-command/gcloud-cli` with weak
present-but-disabled semantics: a template reference finalizes while `gcp` is disabled, use is
refused with the enable-plugin hint, an operator declaration of the same name wins while disabled,
and enabling `gcp` enables both the platform and install command. The command installs the current
Google Cloud CLI into a Debian/Ubuntu guest from Google's signed apt repository, checks `gcloud` for
the completed-install fast path, reconciles an interrupted key/source setup without duplicate apt
entries, and is never used by provider lifecycle code.

The existing `aws` plugin anchors its own `manifests/install-commands.yaml` and publishes
`system-install-command/aws-cli` with the same disabled/enabled, operator-override, provenance, and
recipe-gate semantics. Its command selects AWS's current official CLI v2 archive for the guest's
`x86_64` or `aarch64` architecture, downloads the matching detached signature, imports the pinned
AWS CLI signing key into a private temporary GnuPG home, verifies its full fingerprint and the
archive signature, extracts it in that private directory, and installs to `/usr/local/aws-cli` with
the `aws` launcher in `/usr/local/bin`. The manifest deliberately omits the generic `test_exec`
probe because AWS CLI v1 and v2 share that executable name. The command's own completed-install fast
path parses `aws --version` and skips only an existing v2 installation; v1 continues through the v2
install path. An incomplete prior installation is reconciled with the official installer's explicit
`--update`, `--install-dir`, and `--bin-dir` options, and temporary artifacts are removed on success
or failure. Unsupported architectures, signing-key drift, and invalid signatures fail clearly. The
installer never runs `aws configure`, writes a credentials/profile file, or participates in EC2
lifecycle code, which continues to use boto3.

Permanent edits cover the installed-plugin list, VM platform list, command reference, resources
guide, plugin author contract, vm-platform author contract, capability durable-surface enumeration,
and GCP/AWS operator teaching. The guides distinguish optional guest installation from host-side ADC
or AWS credential sources and optional host recovery tooling. Completion code remains unchanged
because capability and declarable names are discovered from registries; tests pin the relevant names
on completion-adjacent surfaces.

## Verification structure

Offline fakes retain the final typed Compute request and record operations. Tests cover:

- plugin disabled/enabled registration, multi-contribution publication, `gcloud-cli` and `aws-cli`
  recipe gating, operator override, exact manifest payloads, AWS CLI v1/v2 fast-path distinction,
  signing-key/signature rejection, and contract-v2 conformance;
- exact schema, omission-only outer-auth default, in-arm secret null/default, `SecretRef`,
  schema/sample/guide projection;
- ambient and service-account construction, exact internal and terminal LF/CRLF downloaded JSON
  through the real env-var source and operation resolver, no fallback, caching, malformed JSON, NUL
  rejection, and secret-free full exception graphs;
- multiline secret resolution plus sink-local, value-free environment, reveal, Git credential,
  Proxmox HTTP-header, and Tailscale line-safety rejection;
- zero-mutation VM create and Tailscale rekey rejection for incompatible line-oriented values, plus
  direct-sink defense-in-depth coverage;
- runup default-network/subnet behavior and pre-mutation failures;
- network-policy enforcement order plus priority-zero allow/deny conflicts; exact retained-identity
  derivation/collision behavior; live machine CPU/memory/architecture verification; `debian-cloud`
  image-family and zonal disk request selection; run-once reboot behavior; encoded startup-script
  size boundary; boot-disk auto-delete and empty guest service-account fields;
- provider-retained sentinel absence plus exactly one fixed-stdin join;
- create success, every partial rollback point, first/second interrupts, cleanup survivors, and
  no-residue assertions;
- DONE known-capacity and unknown/malformed operation failures, non-DONE timeout outcomes, exact
  definitive/indeterminate caller behavior, no post-wait provider refresh, partial-progress cleanup
  interruption, original interrupt identity, and second-interrupt retained-resource guidance;
- exposure priorities and protocols, inherited-policy support boundary, indeterminate firewall
  insert reconciliation, concurrent transient routes, live IP lookup, guarded power/status, typed
  surviving-VM delete, and auxiliary cleanup behavior;
- guide inertness, file lint, Rulesync, strict mypy, Ruff, and the full non-integration suite.

Live acceptance uses the repository's tester protocol only after the operator supplies a bounded
project/zone/network and authorizes credential use and resource mutation.

-- agw-ns-gcp-platform (effort lead)
