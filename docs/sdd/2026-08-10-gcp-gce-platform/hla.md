# GCP GCE VM platform: high-level architecture

## Placement

The plugin lives at `cli/agentworks/plugins/gcp/` and follows the AWS package shape:

- `__init__.py`: one `Plugin(name="gcp")` descriptor contributing `GCEPlatform`;
- `config.py`: auth union, site model, machine-type catalog and selection;
- `auth.py`: ambient/service-account credential construction and secret-free error mapping;
- `network.py`: Compute API error mapping, external access, firewall, rollback, and cleanup helpers;
- `platform.py`: contract-v2 VM lifecycle and `TopicProse`.

The installed plugin index imports `gcp` beside `aws` and `azure`. Shared bootstrap generation,
ephemeral stdin join, SSH-prefix detection, capability registration, and manager lifecycle stay in
core.

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

The reviewed latest stable floors are `google-cloud-compute>=1.50.0` and `google-auth>=2.56.0`. The
implementation imports both, so both are direct dependencies and enter the lock file.

Ambient construction uses Application Default Credentials with the cloud-platform scope.
Service-account construction parses `ctx.secret(auth.secret)` as JSON and calls
`Credentials.from_service_account_info`. Parsing and credential validation happen inside a narrow
builder that does not log, cache, or chain the raw value or parser exception. It returns only the
derived credential, cached once per platform instance. One derived Compute client is cached per
concrete client kind; requests carry the site's project explicitly. The raw JSON string and parsed
mapping are not retained by the platform.

No fallback crosses modes. Client construction and runup failures name the site, mode, secret name
when applicable, and remediation, but never the value.

The ordinary secret-resolution boundary rejects carriage returns and line feeds, so a multi-line key
file cannot be exported verbatim. Operator docs require validating and compacting it into the
default env-var value in one step:

```bash
export AW_SECRET_GCP_SERVICE_ACCOUNT_KEY="$(jq -c . /path/to/service-account-key.json)"
```

The explicit arm then receives that complete JSON value through the ordinary secret-source chain;
the path and document never enter the site manifest.

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

1. Select the smallest machine type satisfying `ProvisionRequest.cpus` and `memory_gib`; resolve the
   live machine type and verify its CPU, memory, and architecture match the catalog declaration;
   resolve `projects/debian-cloud/global/images/family/debian-12` or
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
5. Insert the instance with one lifetime ephemeral external IPv4 access config, the target tag, an
   explicit `IPV4_ONLY` interface, metadata SSH key, project-key blocking, OS Login disabled for
   this instance, and the key-free startup script. The boot disk is `auto_delete=True`;
   `service_accounts` is explicitly empty.
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
  deleted.
- `start` / `stop`: live status guards avoid invalid-state provider calls and make already-running
  or already-stopped success; real transitions use bounded operation waits.
- `delete`: close the provisioning allow, request instance deletion, verify absence, then delete the
  deny. Already-gone is success. A surviving/indeterminate instance raises typed and keeps the deny;
  auxiliary rule residue after proven absence warns precisely.
- `display_backend_name`: `<instance>@<zone>`.
- `platform_metadata`: instance name, project, zone, network tag, allow rule, deny rule, and access
  config name. It excludes IP addresses and credentials.
- native transport: live external IP from the lifetime access config; `transient_route` owns only
  the per-operation scoped inbound allow.

The external IP is never cached because GCE may replace an ephemeral address across stop/start.

## Rollback and errors

Firewall creation and instance insertion are one guarded mutation region. A firewall insert timeout
is reconciled by exact-name `get`: a rule is deleted only when its complete network, direction,
target, priority, source, and allow/deny shape matches the request this operation owned. A missing
rule is absent; a mismatched rule is retained and reported as a collision. Ordinary exceptions first
close the scoped allow, then request bounded instance deletion even when allow close fails. The deny
is deleted only after `instances.get` proves absence; deletion failure or indeterminate timeout
retains it around the possible survivor. Cleanup raises a typed `GCEError` preserving secret-free
diagnostics and exact retained identities. A first `KeyboardInterrupt` runs the same rollback and
re-raises the original object. A second interrupt stops waiting, reports project/zone/instance and
both firewall rules, retains the deny when needed, and preserves the first interrupt.

Google API authentication/permission, not-found, quota, collision, operation, readiness, and cleanup
failures map to existing Agentworks error categories where one fits. Provider exceptions are
sanitized before chaining; errors that may retain request or credential objects are not attached as
cause/context. No provider error text is allowed to reflect service-account JSON or the Tailscale
key.

## Discovery and documentation

Registration drives capability rows, config schema, `describe-kind`, guide topics, and resource
samples. Permanent edits cover the installed-plugin list, VM platform list, command reference,
resources guide, vm-platform author contract, capability durable-surface enumeration, and GCP
operator setup/recovery. Completion code remains unchanged because platform and plugin names are
discovered from registries; tests pin both names on completion-adjacent surfaces.

## Verification structure

Offline fakes retain the final typed Compute request and record operations. Tests cover:

- plugin disabled/enabled registration and contract-v2 conformance;
- exact schema, omission-only outer-auth default, in-arm secret null/default, `SecretRef`,
  schema/sample/guide projection;
- ambient and service-account construction, no fallback, caching, malformed JSON, and secret-free
  full exception graphs;
- runup default-network/subnet behavior and pre-mutation failures;
- network-policy enforcement order plus priority-zero allow/deny conflicts; exact retained-identity
  derivation/collision behavior; live machine CPU/memory/architecture verification; `debian-cloud`
  image-family and zonal disk request selection; run-once reboot behavior; encoded startup-script
  size boundary; boot-disk auto-delete and empty guest service-account fields;
- provider-retained sentinel absence plus exactly one fixed-stdin join;
- create success, every partial rollback point, first/second interrupts, cleanup survivors, and
  no-residue assertions;
- exposure priorities and protocols, inherited-policy support boundary, indeterminate firewall
  insert reconciliation, concurrent transient routes, live IP lookup, guarded power/status, typed
  surviving-VM delete, and auxiliary cleanup behavior;
- guide inertness, file lint, Rulesync, strict mypy, Ruff, and the full non-integration suite.

Live acceptance uses the repository's tester protocol only after the operator supplies a bounded
project/zone/network and authorizes credential use and resource mutation.

-- agw-ns-gcp-platform (effort lead)
