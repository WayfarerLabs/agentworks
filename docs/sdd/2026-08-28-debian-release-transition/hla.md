# HLA: Debian Release Transition

- Status: Draft
- Date: 2026-08-28
- Requirements: `frd.md`

## Architectural summary

The implementation adds one small ordered core release model and threads it through the existing VM
lifecycle. Core owns the one current Debian release and passes its concrete value to VM creation.
Platforms own how that requested release maps to an image; they never infer current themselves. The
database owns the last release observed on a live VM. The initializer owns resolution of
release-aware package sources. None of those layers gains an operator release selector.

The transition has six cooperating parts:

1. `agentworks.debian` defines one ordered release-profile registry, derives current from its final
   profile, classifies relative support, parses `/etc/os-release`, and attaches each direct upgrade
   policy to its target profile.
2. Internal vm-platform contract version 1 retains the concrete core-selected release in
   `ProvisionRequest` and includes required create/list/restore/delete checkpoint operations.
   Platform-local maps turn releases into provider image selectors, while provider-native snapshots,
   boot-disk copies, and exports remain behind the common checkpoint descriptor. The descriptor and
   all six bundled implementations mutate atomically; missing release mappings fail clearly before
   backend mutation.
3. `vms.debian_release` and `vms.debian_release_observed_at` retain the last verified observation.
   Legacy rows begin unknown and are populated by a live probe.
4. A release value resolver chooses platform and APT values from explicit maps. The selected VM
   release, not current product policy, controls reinitialization of an existing guest.
5. A forward checkpoint table records Agentworks' one managed recovery artifact per VM separately
   from provider metadata. Core-generated names, provider inventory, and lifecycle states make
   interrupted create, restore, and delete diagnosable and replay-safe.
6. A new VM manager workflow executes one durable `current-1` to `current` state machine. An
   incomplete journal is resumed before a new transition is derived, so a later release promotion
   cannot strand work already in progress. The first policy and certification instance is
   Bookworm-to-Trixie. It uses ordinary VM orchestration before and after the reboot, but package
   work and progress live under `/var/lib/agentworks` so an SSH or local CLI interruption cannot
   erase the operation.

`vm reinit` remains Phase B convergence. It becomes release-aware, but it does not own distribution
upgrade logic. `vm upgrade` composes release discovery, preflight, backup, the Debian upgrade,
reboot/reconnect, and then Phase B.

## Core release model

### Values and policy

The core module exposes concepts equivalent to:

```python
class DebianRelease(StrEnum):
    BOOKWORM = "bookworm"
    TRIXIE = "trixie"


DEBIAN_RELEASES = (
    DebianReleaseProfile(
        release=DebianRelease.BOOKWORM,
        version_id="12",
    ),
    DebianReleaseProfile(
        release=DebianRelease.TRIXIE,
        version_id="13",
        upgrade_from_previous=BookwormToTrixiePolicy(...),
    ),
)

CURRENT_DEBIAN_RELEASE = DEBIAN_RELEASES[-1].release
```

Registration validates that release and codename/version pairs are unique, the first profile has no
upgrade policy, and each later profile owns exactly one policy for upgrading from the profile
immediately before it. The final profile is current by construction. The support classifier derives
`current`, `previous`, or `legacy` from positions in this one tuple. Those are runtime
classifications, not persisted values or calendar windows.

The target profile's `upgrade_from_previous` policy owns transition-specific checks, source suites,
minimum OpenSSH version, and documentation links. There is no pair-keyed upgrade graph. With no
incomplete journal, `vm upgrade` can select only the final profile's policy and the source profile
immediately before it. Supporting the next Debian release requires implementing and certifying its
local mappings and transition policy, then atomically appending one profile whose policy upgrades
from the old final profile. The database representation, concrete create request, CLI command,
relative support classifier, and adjacent state-machine framework are designed for reuse. Release
notes and certification evidence may still require separately scoped changes inside that framework.

Before that atomic append, a candidate codename is not in the active registry and therefore cannot
be classified or persisted by the shipping build. If a live guest reports that candidate early, the
ordinary unknown-release diagnostic refuses release-sensitive mutation and tells the operator to use
an Agentworks build that supports it. Transition-engine tests use an explicit candidate profile
fixture; they do not create a second active registry or an ahead-of-current tier.

The parser reads `ID`, `VERSION_ID`, and `VERSION_CODENAME` from `/etc/os-release` without sourcing
the file in a shell. A recognized observation requires `ID=debian` and a codename/version pair that
matches one profile. Missing, contradictory, unknown, or non-Debian values produce a typed state
error containing the observed non-secret fields.

### Release value invariant

Every active implementation value whose correctness depends on Debian release has the shape
`Mapping[DebianRelease, T]`. A nested architecture map is valid when architecture is a second axis.
The mapping lives in the component that understands `T`:

- platform image selectors stay inside their platform modules;
- Debian archive suites and upgrade checks stay in `agentworks.debian`;
- third-party repository stanzas stay in their `apt-source` resources; and
- transition-specific exceptions stay in the target profile's upgrade-from-previous policy.

There is no global mega-catalog combining provider details. The common invariant and type are
shared; the domain knowledge remains local.

## Create pipeline

### Common request, result, and core attestation

Vm-platform contract version 1 requires `debian_release: DebianRelease` in `ProvisionRequest`.
`create_vm` always supplies the final registry profile's release; no caller parameter reaches that
assignment. `ProvisionResult` carries transport and backend identity, not a platform-authored
release proof. Vm-platform is an internal API, so the descriptor and all six bundled implementations
mutate together while retaining version 1. Exact registration conformance catches an inconsistent
bundled declaration or incomplete implementation as a curation error. There is no external
compatibility cutover or adapter that can drop either the release or checkpoint requirements.

No platform defines or imports a platform-local current release. Core passes one concrete release to
the pending create node and later uses that same value in `ProvisionRequest`. Before the command's
secret-resolution phase, the pending node calls the platform's pure, offline
`validate_create_release(release)` hook. The default is a no-op for code-owned catalogs. A platform
with an operator-owned catalog overrides it so a missing entry fails before authenticated `runup`,
while its config remains loadable for operations on existing VMs. The platform repeats the release
lookup as its first create action, so every mapping is enforced before backend mutation.

A shared resolver shapes two missing-key failures:

- a missing code-owned selector raises a typed state error naming `vm-platform/<name>`, the
  requested codename, and the need to update Agentworks; and
- a missing operator-owned selector raises a typed configuration error naming the vm-site and exact
  release-keyed field, such as `template_vmids.trixie`.

There is no fallback key, default selector, or reinterpretation of another release's artifact. An
operator-owned miss propagates from pending-create preflight before secret resolution or a
provisional database row. A code-owned miss propagates from `create`; the manager preserves its
type, message, and remediation hint after unwinding the provisional row. Other platform failures
retain the existing `ProvisioningError` wrapper and durable log reference.

Each platform calls one shared release probe with `request.debian_release` over the create-time
transport it already controls. The probe runs after the guest boots and before the platform returns
success. It raises on mismatch, keeping that failure inside the platform's rollback window. A
platform-specific bootstrap that cannot execute the probe is not current-release certified. This
platform-side check improves rollback behavior but is not core's source of truth.

Proxmox has an earlier check because its template catalog is operator-owned. After clone and start
make the QEMU guest agent available, but before the Agentworks bootstrap script runs, Proxmox reads
`/etc/os-release` through one-shot guest-agent exec and compares it with `request.debian_release`. A
missing, unreadable, non-Debian, or mismatched observation rolls back the clone. Proxmox does not
repeat the check after bootstrap. The early check protects the mutation boundary; core's final check
protects the success boundary. There is no `danger_skip_debian_check` or equivalent escape hatch.

The database row begins with a null observed release. After `platform.create` returns, the manager
first persists the result's platform metadata so the backend remains addressable. It then
independently calls the shared release probe over `result.native_transport` with the requested
release pinned before dispatch. Mutating the request cannot change what current means, and core does
not accept a platform-authored release claim in the result. A mismatch, unreadable transport,
malformed observation, or interrupt retains the row in failed, uninitialized state with its backend
identifiers and `vm delete` recovery. Only core's matching live observation is written with the
observation timestamp before Phase A begins. This is runtime conformance enforcement, not a security
sandbox for in-process platform code. The ordering never persists requested intent or a platform
claim as observed state, and it never deletes the only row capable of targeting a backend that
escaped platform rollback.

### Platform-owned selector maps

The initial mappings are:

| Platform | Mapping value                                                                |
| -------- | ---------------------------------------------------------------------------- |
| Lima     | architecture to official Debian cloud qcow2 URL                              |
| WSL2     | authoritative OCI codename tag; URLs, labels, and cache names derive from it |
| AWS EC2  | reviewed SSM release path segment plus architecture                          |
| Azure VM | publisher, offer, SKU, version, architecture support, and OS disk floor      |
| GCP GCE  | project plus architecture-specific image family                              |
| Proxmox  | operator template VMID                                                       |

The current Trixie values are the R2 table in the FRD. The final cutover deletes Bookworm platform
image selectors because no shipped create path consumes them; Bookworm detection, operational APT
values, and the Trixie profile's upgrade policy remain. A later release promotion adds its selector
to each platform before the certified profile is appended. A platform missed by that promotion fails
with the standard out-of-date error instead of silently creating its own idea of current. Fixtures
use prebuilt Bookworm VMs rather than a hidden create selector.

Proxmox is the one necessary configuration exception because Agentworks does not control a provider
image catalog. Its site model moves from:

```yaml
template_vmid: 9000
```

to:

```yaml
template_vmids:
  trixie: 9001
```

Core still chooses the key. A site cannot use this map to make a Bookworm VM because create never
requests Bookworm. Legacy `template_vmid` is accepted only as the historic Bookworm slot so an
unchanged site remains loadable for existing VM operations. It never fills the Trixie slot. The
adapter is governed by ordinary configuration compatibility rather than a Debian support date; its
presence cannot make a non-current release creatable.

The Proxmox setup script builds core's current release and exposes no release argument. It emits the
corresponding official cloud image URL and template name.

## Persistence and observed state

### Schema

Migration 33 adds nullable columns to `vms`:

```sql
debian_release TEXT,
debian_release_observed_at TEXT,
CHECK ((debian_release IS NULL) = (debian_release_observed_at IS NULL))
```

The pair is both null or both non-null. The schema deliberately does not enumerate codenames, so a
future release profile does not require a database-shape migration. Repository/converter code
validates stored text against the running build's registry and exposes `DebianRelease | None`;
arbitrary strings do not escape the database boundary. A newer recognized value seen by an older
binary produces a typed unsupported-state error rather than being guessed or ignored. The migration
also updates the safer-migrations exact schema inventory and its migration fixtures.

No data backfill runs. In particular, the migration does not assume that all existing VMs are
Bookworm because Proxmox accepted operator templates and operators can modify guests outside
Agentworks.

Migration 33 is immutable once applied. A separate forward migration 34 adds checkpoint state:

```sql
CREATE TABLE vm_checkpoints (
    vm_name             TEXT PRIMARY KEY REFERENCES vms(name) ON DELETE RESTRICT,
    name                TEXT NOT NULL UNIQUE,
    provider_identifier TEXT,
    operation_id        TEXT UNIQUE,
    desired_state_fingerprint TEXT NOT NULL
                        CHECK (length(desired_state_fingerprint) = 64),
    state               TEXT NOT NULL
                        CHECK (state IN ('creating', 'ready', 'restoring', 'deleting')),
    capture_release     TEXT NOT NULL,
    source_release      TEXT,
    target_release      TEXT,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    CHECK (provider_identifier IS NOT NULL OR state = 'creating'),
    CHECK ((state = 'ready') = (operation_id IS NULL)),
    CHECK ((source_release IS NULL) = (target_release IS NULL)),
    CHECK (source_release IS NULL OR capture_release = source_release)
);
```

The VM primary key makes the current one-checkpoint product rule structural. The provider identifier
is null only while an interrupted create has not yet been reconciled. `operation_id` is non-null
only while one invocation owns an active lifecycle transition; a retry claims a fresh identifier,
and every completion compares it so a stale invocation cannot complete newer work. The desired-state
fingerprint is a SHA-256 digest of the canonical effective desired declaration projection at capture
time, evaluated against the checkpoint's immutable `capture_release`. Standalone creation cannot
live-probe a stopped guest, so it requires and copies the VM's recognized persisted observation
under the exclusive guard; absence fails with guidance to start and observe or reinitialize the VM,
stop it, and retry. Upgrade uses its fresh pre-stop observation. `source_release` and
`target_release` remain the optional transition pair; their presence derives the operator-facing
`debian-upgrade` purpose, while their absence derives `operator`, so purpose is not stored as a
duplicate fact. The schema requires an upgrade checkpoint's capture release to equal its source
release. Releases remain text validated by the running registry rather than an SQL enumeration. The
migration updates the exact schema sentinels and proves both a fresh migration ladder and a
version-33 database advancing to version 34. It never modifies migration 33 or probes a provider.

`vm delete` owns checkpoint cleanup before backend VM deletion. Ordinary failure keeps both the
checkpoint and VM rows addressable; the restrictive foreign key is a final orphan guard, not the
primary lifecycle mechanism. An explicit forced delete first attempts the same cleanup, then may
compare-and-delete the lifecycle row only after warning that provider residue and billing can remain
and atomically recording a `checkpoint_abandoned` event. This is disowning, not provider cleanup.
Database backup projection includes the checkpoint row so the local recovery record and VM state
cannot silently diverge.

### Discovery and reconciliation

Release-sensitive operations use a shared `verified_vm_release` service:

1. probe the live guest;
2. if the row is unknown, persist the recognized observation and return it;
3. if row and guest agree, refresh `observed_at` and return it; and
4. if they disagree, stop before guest mutation and report both.

The one intentional reconciliation owner is `vm upgrade`. If the row says `current-1` but a healthy
guest already says `current` and there is no active Agentworks upgrade journal, it treats this as an
operator-performed adjacent upgrade. It presents an adoption plan, validates the current guest,
records the proved observation after confirmation, and then runs release-aware Phase B. A later
Phase B failure therefore cannot leave the row naming the previous release. Other lifecycle commands
point to this path instead of silently adopting a changed base system. Bookworm-to-Trixie is the
first concrete adoption pair.

Release-insensitive recovery operations such as list, start, stop, shell, backup, and delete remain
usable with an unknown row. `vm list` never performs a live probe because it backs completion and
must remain fast and side-effect-free.

### Operation support policy

Observation answers what release the guest runs. A shared classifier compares the observed release
with positions in `DEBIAN_RELEASES`:

| Position | Ordinary operations | `vm upgrade` |
| -------- | ------------------- | ------------ |
| current  | supported           | no-op        |
| previous | supported           | to current   |
| legacy   | best effort, warn   | refuse       |

The shared VM operation boundary emits one warning per command before accessing a known legacy VM.
It does not refuse an ordinary operation solely because of that classification. Existing concrete
checks still apply, including platform reachability, package safety, and availability of any
release-specific mapping the operation actually needs. `vm list` remains probe-free and renders the
stored classification instead of emitting one warning per row.

With no incomplete journal, `vm upgrade` separately enforces that the observed source is the
penultimate profile and uses only the final profile's upgrade-from-previous policy. A legacy source
fails without mutation and points to the fresh-VM/data-copy path. An incomplete journal follows the
recovery rule below instead of new-upgrade eligibility. No date argument, compatibility deadline, or
persisted support tier participates in either decision. Upstream lifecycle dates may be displayed as
release facts but never override this product contract.

### Events rather than a second progress truth

The checkpoint table owns current recovery-artifact identity and lifecycle. VM events record the
operator action: checkpoint creation/restoration/deletion, upgrade start, repair-required outcome,
and completion. They do not emit release-discovery or target-observation events because the VM row
and its observation timestamp already own those facts. It also does not copy every remote package
action into a second mutable state machine. While the guest is reachable, the durable guest journal
is the authority for an in-progress upgrade. The database release is the last verified observation,
and the remaining events are the action audit trail.

This avoids a split-brain status where a local process dies after apt advances but before a database
write. A future invocation gives an incomplete journal precedence, probes the guest, then records
the result it can prove. Retained old target profiles keep their journal reader and direct policy
for forward recovery; that recovery entry point cannot start another transition or form a multi-hop
path.

## Managed checkpoint contract

### Platform API and ownership

Internal contract version 1 includes one frozen value and four mandatory operations:

```python
@dataclass(frozen=True, slots=True)
class CheckpointDescriptor:
    name: str
    identifier: str


def create_checkpoint(vm, name, ctx, *, operation_id: str, resume: bool) -> CheckpointDescriptor: ...
def list_checkpoints(vm, ctx) -> tuple[CheckpointDescriptor, ...]: ...
def restore_checkpoint(vm, checkpoint, ctx, *, operation_id: str) -> None: ...
def delete_checkpoint(vm, checkpoint, ctx) -> None: ...
```

Core generates a globally collision-resistant, provider-safe name with an `agw-` prefix. The
provider identifier may equal that name or may be an assigned snapshot/resource ID. Both strings are
opaque outside the owning platform. The list operation returns only Agentworks-managed checkpoints
bound to the exact VM incarnation; it does not adopt or expose unrelated operator snapshots. It
includes an incomplete owned artifact left after provider mutation so core can resume or delete it
without releasing the lifecycle row. A listed descriptor proves identity and ownership, not
readiness. `create_checkpoint` receives the persisted creating-row operation identity and a `resume`
bit. It drives or resumes provider completion and proves completion, source disk identity, ownership
tags, and VM-incarnation binding before returning success. A provider whose create API has an
ambiguous response must not submit another request on resume until it can reconcile the original;
failure to do so remains fenced for repair. Core compares the provider inventory with the database
row and refuses absence, duplicates, or name/identifier disagreement. Restore receives the durable
restore-attempt identity: the same identity replays one interrupted attempt, while a later explicit
restore receives a new identity and must reapply the checkpoint even when the current boot disk came
from an earlier restore. Delete is replay-safe by descriptor, accepts an owned incomplete artifact,
and proves its absence.

The API is plural because providers can contain duplicate or interrupted artifacts even though the
product permits one. Checkpoints are not capabilities separate from vm-platform, optional methods,
resource graph nodes, manifests, or fields inside `vms.platform_metadata`. The descriptor and every
bundled platform retain version 1 and mutate atomically. Exact contract conformance catches an
incomplete or inconsistently declared bundled implementation before it is seated.

### Consistency, state transitions, and command exclusion

Creating and restoring require all Agentworks sessions to be stopped and the VM to be stopped.
Standalone `create-checkpoint` and `restore-checkpoint` fail with ordinary stop guidance when the VM
is running; they do not silently change operator power intent. Standalone creation copies a
recognized persisted release observation under the exclusive guard and fails with actionable
observe-and-stop guidance when it is absent. Upgrade is the one composition that uses its fresh
pre-stop release observation, temporarily stops the already-active VM, captures its checkpoint, and
restarts it before continuing. Platforms verify completion, ownership, and source binding before
returning create success. Before a fresh row is inserted, core lists the live managed inventory and
refuses an artifact with no database record rather than adopting or overwriting it. Stopped is the
restore entry and exit invariant; a platform may temporarily start or reboot the VM when its
provider requires that choreography, but it must stop and prove the final state before returning. If
checkpoint creation or restore attestation has already failed, a secondary restart or stop failure
is warned without replacing that primary failure; a cleanup failure remains the command failure when
no earlier failure exists.

The database row is inserted in `creating` with an operation identifier before provider mutation.
Success stores the returned identifier, clears the operation identifier, and advances to `ready`. An
interruption leaves enough identity for the same command to list and reconcile the generated
provider artifact. Deleting an interrupted create directly removes one exact owned artifact,
including a provider-reported terminal failure, and releases the original creating row only after
provider inventory proves absence. Before provider deletion, core atomically records the exact
descriptor and moves the row to `deleting`, so a crash after provider deletion resumes cleanup
without replaying creation. When inventory is empty before that claim, deletion first resumes the
same create attempt because an ambiguous provider response may have escaped observation; only then
does it delete the reconciled artifact. Unrelated inventory leaves the row fenced for repair rather
than guessing which artifact is safe to mutate. Restore claims `ready -> restoring` with a fresh
operation identifier and passes it to the platform; a `restoring` retry reuses the persisted
identity until post-restore core attestation returns the row to `ready`. Delete follows the same
claim rule and removes the row only after provider absence is proved. Every terminal update compares
both lifecycle state and operation identifier.

A persisted `restoring` row is the one exception to requiring the original provider identity to be
observable at resume entry. A destructive platform can be interrupted between renaming or replacing
the old resource and installing the recovered resource. Core permits `unknown` status only for that
already-claimed restore and delegates reconciliation with the same operation identity. A reported
running state still fails with stop guidance, and the platform must return and prove the logical VM
stopped before core begins restored-guest attestation.

`delete-checkpoint --force` never skips that reconciliation. It attempts the ordinary provider
cleanup first and reaches the fallback only after a typed Agentworks boundary or provider failure.
The fallback shows the known provider identifier when one exists, warns that late, incomplete,
emergency, or duplicate artifacts may remain and continue billing, confirms separately unless
`--yes` was supplied, removes the row by state-and-operation compare-and-delete, and records a
distinct abandonment event in the same database transaction. Forced VM deletion uses the same
fallback only after checkpoint cleanup fails. Unexpected programming errors propagate while
Agentworks retains checkpoint ownership; each bundled platform normalizes expected transport,
permission, and provider API failures into typed Agentworks errors at its boundary. Without force,
both delete commands preserve the rows and return repair/retry guidance.

A narrow, private, per-VM shared/exclusive operation guard closes the race that database transitions
alone cannot: another process must not start, delete, reinitialize, access, or resume a session on
the VM while an offline checkpoint operation is in progress. The guard uses a private SQLite lock
file under the Agentworks state root, named from a safe hash of the VM identity. Ordinary
incompatible operations hold a read transaction after reading SQLite schema metadata to acquire the
shared lock; checkpoint create/restore/delete and the complete upgrade hold an exclusive transaction
for the entire provider operation and post-restore attestation. The separate lock database uses
rollback journal mode so concurrent readers coexist and an exclusive owner excludes both readers and
writers. Acquisition uses a short bounded timeout and fails with the VM name, requested operation,
and retry guidance instead of waiting behind an interactive shell.

The guard is process-crash-releasing and reentrant within one command context. An exclusive owner
may enter nested shared lifecycle helpers, while shared-to-exclusive promotion is rejected as a
caller error. VM delete takes exclusive ownership at its command root because it composes checkpoint
deletion; its nested cleanup and ordinary lifecycle helpers are reentrant. VM
start/stop/reinit/rekey, the ordinary VM activation boundary, and session create/resume take shared
ownership before relevant provider or guest work. Workspace, agent, session, console, grant, and
desired-overlay commands that change the guarded VM's desired subtree take shared ownership before
their first database mutation and retain it through guest work. Read-only list, describe, and doctor
paths do not. A failed session check releases the exclusive guard, so the operator can run the
instructed stop command before retrying the upgrade.

This is checkpoint-integrity exclusion, not the broader cross-resource locking framework already
identified as follow-up work. Provider operations also use their backend's native task/resource
locks, and `vm upgrade` retains its guest journal lock for package work. Direct out-of-band
provider, SSH, or console mutation cannot be fenced. General ordered locking across VM, workspace,
agent, session, and console resources remains separate work.

### Built-in mappings

All built-ins preserve the same logical VM and existing `platform_metadata`:

| Platform | Managed checkpoint and restore                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Lima     | The live instance's driver selects the primitive. QEMU uses `limactl snapshot create/list/apply/delete`. VZ uses a stopped, protected `limactl clone`; restore clones a temporary instance, retains the original pre-first-restore instance under a protected emergency name, and swaps the recovered instance back to the original name. The checkpoint clone records the stable restore operation identity for replay. Delete removes every related recovery instance. VZ refuses `additionalDisks`. |
| WSL2     | A stopped-distro export under the Agentworks WSL storage root. Restore refuses a partial export before unregistering anything, strictly proves distro inventory across the destructive boundary, first exports the current distro as an emergency intermediate, unregisters/imports under the same name and install path, and keeps both artifacts if recovery cannot be proved.                                                                                                                       |
| AWS EC2  | A tagged root-volume EBS snapshot. Restore enters stopped, announces and temporarily starts the instance, submits the replacement task with a stable checkpoint-derived client token, survives its provider reboot, then stops and proves the same instance stopped. Stop cleanup never masks the primary restore error; replay discovers the task rather than submitting another.                                                                                                                     |
| Azure VM | A tagged managed-OS-disk snapshot. Snapshot, disk, update, and tag requests use typed Azure SDK models. Restore creates a replacement managed disk, swaps it onto the same VM, and retains the displaced disk until the swap and core attestation are recoverable.                                                                                                                                                                                                                                     |
| GCP GCE  | A labeled boot-disk snapshot. Restore creates a replacement zonal disk, swaps it onto the same stopped instance, and retains the displaced disk until recovery is proved.                                                                                                                                                                                                                                                                                                                              |
| Proxmox  | A generated QEMU snapshot name with `vmstate=0`, a 3600-second checkpoint task timeout, exact inventory checks, rollback, and deletion on the same VMID/node. Unsupported storage fails clearly.                                                                                                                                                                                                                                                                                                       |

Provider-specific intermediate resources use deterministic names/tags tied to the checkpoint and are
retained as one pre-first-restore emergency artifact until checkpoint deletion. A later restore
reuses that already-proved recovery artifact rather than replacing it with the current guest state;
deleting the managed checkpoint removes both. This preserves recovery across core's later
attestation without adding a fifth platform callback or another lifecycle state machine. New cloud
permissions and the Proxmox snapshot privilege are documented in each platform's setup and
least-privilege guidance. Snapshot storage cost remains visible operator-owned infrastructure cost
even though Agentworks owns the artifact lifecycle.

### Restore reconciliation

`restore-checkpoint` is destructive and requires the ordinary confirmation unless `--yes` is
present. It never runs automatically after upgrade failure and never deletes the checkpoint. Before
claiming `restoring`, core recomputes the desired-state fingerprint and refuses when the VM-owned
effective desired declarations changed after capture. The error explains that whole-VM restore
cannot safely combine an older guest with newer declarations and directs the operator to create a
fresh checkpoint or restore the matching Agentworks database backup. There is no force bypass.

List and describe project restore eligibility separately from provider lifecycle state. A completed
provider artifact stays `ready`; its derived restore value is `available` only when live inventory
proves the recorded descriptor and the current fingerprint matches, `declarations-changed` when the
fingerprint does not, `resume-required` while one restore attempt is durable, and `unavailable` when
provider proof cannot be obtained or for other lifecycle states. Describe degrades a failed proof to
that unavailable status plus a diagnostic; ordinary listing refuses the inconsistent inventory
rather than returning a misleading row. The projection is read-only and does not relabel persisted
state. Names-only listing short-circuits before provider or fingerprint work, preserving its fast
completion contract.

The canonical fingerprint includes the VM's stable desired specification and platform identity,
workspace declarations, agent declarations and grants, session declarations and membership, console
declarations and membership, and desired-overlay instance records owned by that subtree. It also
resolves and includes each referenced VM, admin, workspace, agent, and session template after
inheritance and stored-overlay application; the non-secret vm-site declaration; and the complete
authorized-key identity set from the primary and every configured extra public key. From the
effective templates and site it walks the transitive declared-resource closure and includes each
canonical non-secret effective spec, resolving release-sensitive values against the immutable
release captured in the checkpoint row. It does not use the VM's current release observation, which
can legitimately be the upgrade target before a backward restore. Secret references contribute kind
and name only; their resolved values and backend configuration do not participate. Missing or
invalid referenced declarations, unreadable public keys, or an unknown capture release make the
fingerprint unavailable and block restore. The projection sorts rows, keys, key fingerprints, and
resource identities before hashing.

It excludes timestamps, events, Debian observations, initialization/provision progress, process and
socket facts, current power/session status, applied-state records, and every resolved secret value.
Those are observations, workflow state, or sensitive operation inputs rather than desired
declarations. Adding, removing, or changing a child declaration, referenced effective template, or
relevant non-secret operator/site setting therefore makes the retained checkpoint ineligible for
managed restore instead of silently rolling the database backward or rebuilding a speculative
historical topology.

After the platform restore returns, core starts the VM, re-establishes a native or canonical route,
independently verifies Debian, and records the restored release and observation time even when this
is an intentional target-to-source move. It sets initialization status to pending, records the
restore event, stops the VM again, and directs the operator to run `vm reinit` before treating the
guest as reconciled. Desired VM, workspace, agent, session, and console declarations stay in the
database and are known to match the capture fingerprint; a provider checkpoint does not roll them
back.

If the platform restore or core attestation is interrupted, the row remains `restoring` and the same
command resumes or diagnoses provider state. A destructive platform keeps its pre-restore disk or
WSL export until the intended restored guest has been attested. Once core starts the guest for
attestation, it makes a best-effort stop on every success, failure, and interruption path; failure
to prove stopped leaves the row restoring with exact power-state guidance. The database never
guesses a release from the checkpoint row or rewrites the observation before the live probe
succeeds.

## Release-aware APT resources

### Resource shape

`AptSourceEntry` accepts either a scalar `source` or a release map `sources`, never both:

```yaml
spec:
  key_url: https://apt.releases.hashicorp.com/gpg
  key_path: /etc/apt/keyrings/hashicorp-archive-keyring.gpg
  key_dearmor: true
  sources:
    bookworm: "deb [arch={arch} signed-by=...] https://apt.releases.hashicorp.com bookworm main"
    trixie: "deb [arch={arch} signed-by=...] https://apt.releases.hashicorp.com trixie main"
  source_file: hashicorp.list
```

The model retains a scalar for repositories such as GitHub CLI, NodeSource, mise, and Google Cloud
CLI whose published suite is release-independent. A validation rule rejects a scalar that contains a
token equal to a registered Debian codename. This makes the mapping rule enforceable without
pretending that every vendor suite name must equal the host codename. A scalar is the declaring
operator or bundled plugin's assertion that the value is release-independent; the codename check
catches an obvious contradiction but does not claim to prove vendor compatibility for every URL,
numeric path, or suite name.

The shipped mappings are based on vendor support:

- HashiCorp maps Bookworm to `bookworm` and Trixie to `trixie`.
- tofuutils/tenv maps Bookworm to `bookworm` and Trixie to `trixie`.
- ngrok maps both host releases to the vendor-prescribed `bookworm` repository until ngrok publishes
  a different Trixie instruction. This is an explicit Trixie mapping, not an accidental old value.

Loading preserves the declared scalar or map. Resolution occurs immediately before source mutation
using the VM's verified release. Historical values stay useful for best-effort operations on legacy
VMs; a missing value still fails before mutation with a focused mapping error rather than a generic
age refusal. Generated samples, schema/explain output, graph publication, and plugin parity tests
expose both valid forms.

### Upgrade source handling

The distribution upgrade does not blindly rewrite every `bookworm` string. It classifies active APT
sources into:

- canonical Debian and Debian security sources;
- selected Agentworks-managed third-party source files; and
- unmanaged or unselected third-party source files.

Before switching suites, it copies the full source estate into the local recovery bundle and remote
journal directory. It disables every non-Debian source for the distribution package stages. It then
writes one canonical deb822 `debian.sources` file pinned to the target suites from the selected
transition policy and removes enabled source-release Debian entries.

After current is observed, Phase B recreates selected Agentworks-managed sources from their target
mapping. Unmanaged sources stay disabled with their original filename and content preserved and
listed in the final result. Agentworks never guesses whether an unmanaged repository supports the
target release. The first selected source and target are Bookworm and Trixie.

## `vm upgrade` manager workflow

### Command shape and boundary

The CLI shape is:

```console
agw vm upgrade NAME
```

`NAME` is the only operand. The command has no checkpoint reference, target, release, force, or yes
option. The target is always the core current release, and checkpoint creation is an owned workflow
stage rather than an operator attestation.

The command has two interactive authorization points. The preliminary decision authorizes bringing
the source release current within its existing suite after local backups and the managed checkpoint
are ready. The final decision follows a reopened operation boundary plus a complete recomputation of
the target plan and authorizes switching suites. It shows removals, disabled repositories, backup
paths, checkpoint identity, material plan drift, and expected downtime. Once an irreversible action
exists, rerunning the command inspects or resumes it without recreating completed work.

The manager owns validation and typed errors. The Typer command only maps argv to the service call.
The workflow opens the ordinary VM operation boundary to resolve the site credentials, canonical
transport, operator SSH identity, and VM template Tailscale key before mutation. This follows the
rekey boundary because reconnect may need to rejoin Tailscale; `reinit` does not resolve that
secret.

After activation provides access, the manager scans the fixed upgrade-state root before it observes
and classifies the guest for a new transition. Exactly one incomplete `{source}-to-{target}` journal
takes precedence over the current registry position. The manager validates that the directory names
an adjacent retained profile pair, loads the target profile's upgrade-from-previous policy, and
resumes, diagnoses, or repairs that operation. It does so even if a later profile append now makes
the recorded source legacy. Multiple incomplete journals fail with repair guidance, and an
incomplete journal prevents creation of another one.

Only when there is no incomplete journal does the manager observe and classify a new-upgrade
request. A current guest exits successfully with an already-current result. A legacy guest fails
without mutation and explains that current Agentworks supports only the adjacent previous-to-current
edge; it recommends a new current VM and data copy. A previous guest proceeds only through the final
profile's upgrade-from-previous policy. Bookworm-to-Trixie is the first selected pair.

### Preflight

Before constructing the package-planning snapshot, the manager announces and runs a separate
stopped-session check. It loads the VM's sessions, batch-checks any non-stopped rows, and fails
immediately when a session is running, broken, or unavailable. The typed VM state error lists each
blocked session in operator language and gives the exact `agw session stop --all --vm NAME` and
`agw session list --vm NAME` remediation. This gate remains read-only and never stops sessions on
the operator's behalf. Keeping it outside the package plan lets it fail before the slower release
re-probe and isolated APT simulation.

Named consoles are not a separate stopped-session check because Agentworks has no console-stop
lifecycle. For a fresh eligible upgrade, the manager instead warns once that reboot ends live tmux
state and console-only shell processes. Console definitions and session membership remain database
state and rebuild on the next attach.

The read-only preflight records a plan containing:

- database and live release;
- architecture and current kernel;
- dpkg audit result and held packages;
- source-release point state and OpenSSH package version;
- installed kernel metapackage on guest-kernel platforms, or the explicit WSL2 provider-kernel
  classification;
- `/boot` size/free space on its actual filesystem, whether separate or shared;
- free space and aggregate required space for each distinct filesystem backing `/`, `/var`, the apt
  archive, and `/boot`; the installed-growth estimate is charged conservatively to each distinct
  root or `/var` filesystem;
- APT preferences, backports, proposed-updates, mixed suites, and third-party sources;
- installed non-Debian and obsolete packages;
- package-owned conffiles whose current hash differs from dpkg's recorded hash;
- release-specific blockers from the selected adjacent policy; and
- the simulated minimal/full upgrade package removals.

The manager passes an announcement callback into the read-only probe and emits a line before each
remote probe group, including the isolated target-release APT simulation. Durable package actions
use output progress handles around journal-engine advancement, while reboot/reconnect and every
post-reboot verification stage emit explicit status lines. Presentation remains manager-owned; the
upgrade engine and probe return structured state and do not depend on Typer.

It also refuses when apt or dpkg's native locks show another package-manager owner. Before package
actions it records the current enabled/disabled state of `apt-daily.timer` and
`apt-daily-upgrade.timer`, then stops and inhibits them. A source-safe abort and a verified healthy
target completion restore that exact prior state. Once source-switch intent is durable, a mixed or
unhealthy package state keeps the timers inhibited and records that fact until forward repair proves
a healthy target or the operator restores the managed checkpoint. The private Agentworks journal
lock prevents a second Agentworks runner; native locks and the timer lifecycle cover external
owners. Restoration first stops every known timer, reconfigures every recorded enablement state,
then restores activation and verifies the complete set. Failure before that proof records a durable
repair-required VM event rather than existing only as terminal output.

Deterministic unsafe conditions fail. Modified conffiles and packages whose release notes require
application-specific intervention fail with exact manual guidance; the first implementation does not
invent a general prompt-answer engine. Package removals are shown for operator review rather than
silently accepted. No concurrent Agentworks session may be live or unverifiable.

The preliminary preflight is not the suite-switch authorization. After backup, managed checkpoint
creation, and the first mutation confirmation, the manager brings the source release fully current.
Because that update and checkpoint activity can change dependencies or machine state, it closes and
reopens the ordinary VM operation boundary, then recomputes the complete preflight: the separate
session gate, package health, holds, conffiles, blockers, sources, space, and simulated removals. It
shows the final plan, highlights any material difference from the preliminary plan, and requires the
second mutation confirmation before changing sources.

The target-release simulation is isolated from guest-owned APT policy. It uses generated canonical
target sources, scratch package indexes, scratch dpkg status and extended-state copies, no
preferences, and a primary scratch `APT_CONFIG` that redirects the main configuration to `/dev/null`
and configuration parts to an empty scratch directory before APT traverses either. Guest APT hooks
or other fragments therefore cannot execute during this read-only plan.

### Backup and recovery gate

Before creating either local artifact, the workflow performs a read-only checkpoint viability pass.
It validates the database slot, current desired-state fingerprint, transition pair, lifecycle state,
and live provider inventory. An empty healthy slot proceeds; a reusable same-pair row or resumable
create proceeds; an unrelated, stale, missing, duplicate, or unrecorded provider artifact fails
before backup storage and time are consumed.

Before source mutation the workflow then completes two local artifacts:

1. the existing `vm backup`, which protects Agentworks metadata and workspace files; and
2. a compact release-upgrade bundle containing `/etc`, `/var/lib/dpkg`,
   `/var/lib/apt/extended_states`, package selections, package/source inventories, and the upgrade
   plan.

Both are written under the configured host backup root with owner-only permissions. The recovery
bundle manifest identifies the VM, observed source release, requested target release, timestamp, and
preliminary plan. After checkpoint capture, the durable guest plan binds both local artifact paths
to the generated checkpoint name. The bundle uses a secure root-owned disk-backed staging directory
such as `/var/tmp` for transfer and is removed after a verified local copy.

After both local artifacts are complete, the manager claims a transition-pair checkpoint row,
temporarily stops the VM, creates and verifies the provider artifact, and restarts the VM. It
records the descriptor in the guest plan and the checkpoint event before the first package mutation.
An unrelated checkpoint already occupying the slot blocks a fresh transition with exact
restore/delete guidance. A matching same-pair checkpoint can be reused when no journal exists and a
live probe still proves the recorded source, covering cancellation before mutation and an explicit
restore. Reuse output includes the checkpoint's original creation time. If a journal exists, resume
requires database, journal, and provider inventory to agree exactly.

The UI distinguishes local data-recovery artifacts from the managed bootable checkpoint and never
claims an automatic rollback. Provider costs, unsupported snapshot storage, missing permissions, or
insufficient WSL export space fail before package mutation. Successful upgrade names the retained
checkpoint, warns that its provider storage can continue billing, and prints the exact
`agw vm delete-checkpoint NAME` cleanup command.

### Durable remote state machine

The upgrade owns a fixed root directory:

```text
/var/lib/agentworks/debian-upgrades/{source}-to-{target}/
  lock
  plan.json
  state.json
  sources-before/
  upgrade.sh
  upgrade.log
```

Root owns the fixed root and pair directory at mode 0700; the lock and JSON files use mode 0600.
Every read and write rejects symlinked roots, pair directories, locks, plans, or states, wrong
effective-user ownership, and non-private modes. The validated directory name is the only stored
source/target identity. `plan.json` holds the computed package, source, blocker, and space plan
without another pair field. `state.json` separates monotonic progress from the current attempt and
its outcome, again without source or target fields. Its conceptual shape is:

```json
{
  "version": 1,
  "attempt_id": "...",
  "last_completed": "prepared",
  "active_action": "source-update",
  "active_started_at": "...",
  "boot_id_before": null,
  "outcome": "running",
  "failure": null
}
```

`last_completed` has six ordered values: `prepared`, `source-current`, `sources-switched`,
`minimal-upgrade-complete`, `full-upgrade-complete`, and `reboot-complete`. `active_action` names
the action attempting the next transition. `outcome` and `failure` describe the attempt without
erasing progress. Target observation stays in the VM row, Phase B stays in existing init state, and
overall completion/repair stays in VM events.

Before every mutation, the actor takes the one fixed non-blocking journal `flock`, reconciles the
current state, and atomically writes a new attempt identity, active action, start time, and, for
reboot, the current boot ID. The package service holds that same lock for its remote action; the
manager takes it around any journal claim/write and reboot dispatch. After an actor proves the
action's postcondition, its completion or failure write compares the attempt identity it observed
with the identity still active under the lock. Retry and repeated reboot dispatch use the same
compare-and-set rule and issue a fresh attempt identity. A stale coordinator therefore cannot
complete, fail, retry, or dispatch reboot again for a newer attempt. Successful completion
atomically advances `last_completed` and clears the active fields before releasing the lock. An
interruption inside an action therefore leaves intent visible. A retry takes the same lock and
checks the systemd unit, native apt/dpkg locks, logs, and that action's postcondition before it
either advances, safely reruns, or requires manual repair. It never equates a missing success write
with failure or success; the package script's lock still prevents duplicate package execution while
invocation-level writes are fenced by attempt identity.

The script verifies native package-manager ownership and owns the recorded inhibit/restore lifecycle
for automatic APT timers. Package work runs in a root systemd service whose script, output, exit
data, and failure detail live in `state.json` and `upgrade.log`. There is no second `result.json`
reader or state authority. The service is detached from SSH but does not include the reboot. A local
interrupt or sshd restart leaves it running. A later CLI invocation shows the last completed and
active actions and either continues after proof or reports the exact manual repair Debian requires.
It does not automatically apply Debian's force-loop or immediate-configuration recovery switches.

The script uses `apt-get`, not `apt`, with a noninteractive environment only after preflight proved
there are no modified package conffiles requiring a decision. It runs the Debian-documented minimal
upgrade before full upgrade and writes ordinary apt/dpkg logs as well as the Agentworks transcript.

### Reboot, reconnect, and completion

Reboot is a separate action after the package unit reports success. With the target release's
installed udev rules now authoritative, the manager runs interface-name prediction and blocks reboot
with pinning guidance if connectivity would be unsafe. It then takes the journal lock, records
reboot intent and the pre-reboot boot ID, dispatches reboot inside that critical section, closes the
pre-upgrade activation span, and opens a fresh post-reboot orchestration span. A changed boot ID
proves reboot completion; an unchanged boot ID permits safely dispatching reboot again only after
`full-upgrade-complete` and clear package-manager locks are reverified under the same lock.
Reconnect is strict: the current warning-and-continue reconnect helper is not used.

The post-reboot order is:

1. wait for provider power state and guest boot;
2. try canonical Tailscale SSH;
3. when that fails, try the platform's existing native transport and explicitly rejoin Tailscale
   using the pre-resolved key;
4. if no route works, preserve the remote progress as last known and record a local
   `repair-required` event with the managed checkpoint descriptor plus provider console
   instructions;
5. observe and persist the target release as soon as `/etc/os-release` proves it;
6. verify dpkg/apt convergence and target source hygiene, the running target guest kernel or WSL2
   provider kernel, systemd, sshd, Tailscale, and Agentworks identities; source hygiene considers
   only enabled `.list` and deb822 stanzas, requires coverage of every policy target suite, and
   rejects enabled foreign suites or third-party URIs;
7. run Phase B with the target release and restore only selected mapped APT sources; and
8. record complete, or target observed with repair required when later verification/reinit fails.

Proxmox has no post-create native transport. Its failure path therefore stops after the canonical
reconnect attempt and directs the operator to the managed Proxmox checkpoint and web console. That
is a truthful platform limitation, not an excuse to proceed without verification.

The system never attempts an automatic downgrade or automatic checkpoint restore. A partially
upgraded package system is resumed forward according to Debian's instructions or restored only by
the operator's explicit `vm restore-checkpoint` command.

## Operational Trixie corrections

Large temporary data moves to disk-backed staging independent of the upgrade command:

- VM backup archives use a secure root-owned directory under `/var/tmp` or the persistent Agentworks
  state directory.
- Workspace copy archives use a secure disk-backed directory on the destination VM.
- Upgrade scripts, progress, and logs use `/var/lib/agentworks`.

Small bounded bootstrap scripts and ephemeral sockets may continue using `/tmp`. The implementation
inventory records why each remaining `/tmp` path is bounded or moves it.

The Trixie certification fixture mounts `/tmp` as a tmpfs with a deliberately small limit. Backup
and workspace-copy tests transfer data larger than that limit and prove the operations use the
disk-backed path.

Agentworks already uses `/etc/sysctl.d`, so no sysctl redesign is needed. Certification does search
for reliance on `~/.pam_environment` and DSA keys, and contract tests prove the generated SSH and
environment paths do not use them. A path assertion keeps sysctl writes under `/etc/sysctl.d`.
Source classification tests cover both legacy `.list` and deb822 `.sources` files. The preliminary
inventory records current Bookworm interface names but does not treat Bookworm's udev rules as a
prediction of Trixie behavior. After `full-upgrade-complete` and before reboot, certification runs
`udevadm test-builtin net_setup_link` against the installed Trixie rules for each interface. An
unsafe predicted rename blocks reboot with Debian's pinning guidance; a permitted reboot compares
the predicted and actual post-reboot names and connectivity.

WSL2 is the deliberate kernel and interface exception. A WSL distribution is a container inside
Microsoft's managed WSL2 VM and has no Debian `linux-image` metapackage of its own. Its preflight
records the running Microsoft kernel, native lock inspection falls back from `fuser` to `lslocks`,
and target health verifies the Microsoft kernel marker. The installed distribution's systemd owns
clean shutdown; after the pre-reboot activation span closes, the ordinary WSL activation gate starts
the distribution again and the changed boot ID proves restart. WSL networking is provider-managed,
so the workflow records the live interface names before shutdown and verifies those names after
restart instead of applying Debian udev naming rules to them.

## CLI, diagnostics, and machine output

`vm create` includes the selected codename in its opening provisioning line, announces the core
release-attestation step, and closes the provisioning section explicitly after Phase A succeeds.

`vm list` gains a release column after its names-only short circuit. It omits the relative support
column because that value is entirely derived from the release and would duplicate it in the compact
inventory view. `vm describe` shows the last verified release, observation timestamp, derived
`current`, `previous`, or `legacy` status, and relevant upgrade events. JSON v1 gains additive
nullable `debian_release` and `debian_release_observed_at` fields; the command reference defines
recognized-codename and null semantics. Support position remains derived rather than persisted.

`vm list-checkpoints` and the checkpoint section of `vm describe` expose provider lifecycle state
and derived restore eligibility as separate facts. Human and JSON output do not call a completed
artifact unusable merely because declarations later changed, and names-only output skips the live
inventory and fingerprint derivation.

The completion spec maps `vm.upgrade`'s `name` operand to VM names. `vm upgrade` itself has no JSON
mode in this first interactive release. Durable logs and recovery artifacts are file outputs, while
human progress uses the ordinary output facade.

Doctor reports:

- unknown release on legacy rows;
- live/recorded disagreement when it can probe safely;
- current-1 upgrade availability and a warning for current-2 or older VMs;
- an incomplete remote upgrade journal;
- disabled unmanaged APT sources after upgrade; and
- Trixie `/tmp` staging hazards if an old implementation left the paths in use.

The ordinary named-VM operation boundary emits the same legacy warning once before access while
allowing the operation to continue. It does not turn release age into a readiness failure.

## Failure and integrity behavior

| Failure point                      | Durable truth                                                  | Next action                                 |
| ---------------------------------- | -------------------------------------------------------------- | ------------------------------------------- |
| Before first mutation confirmation | DB observation unchanged; managed checkpoint may be ready      | rerun, or explicitly delete the checkpoint  |
| Backup failure                     | partial local artifact is not accepted                         | fix space/transport and rerun               |
| Source update failure              | last-completed/active actions and apt log                      | repair source release, then rerun           |
| Final plan differs                 | recomputed plan; suites still on source release                | review and confirm or stop                  |
| Source switch or apt failure       | progress, active attempt, sources backup, log, checkpoint      | repair forward or restore explicitly        |
| Local CLI/SSH interruption         | systemd unit plus progress/attempt state                       | rerun `vm upgrade` to inspect/resume        |
| Reboot never returns               | last known progress plus checkpoint and repair-required events | use platform console/checkpoint             |
| Target observed, health fails      | DB says target; repair-required event                          | repair target release and rerun             |
| Phase B warning/failure            | DB says target; init status partial/failed                     | fix mapped source or initializer and reinit |
| Manual adjacent upgrade found      | DB/live disagreement, no active journal                        | use `vm upgrade` adoption path              |

No automatic upgrade failure path rewrites the database to the source release after the target has
been observed. Explicit checkpoint restoration is the deliberate exception: it writes only the
release proved from the restored live guest. No path calls the existing data-only VM backup a boot
restore point.

## Deliberate boundaries and complexity guard

- No new public OS/image selection model.
- No release field in VM templates or core site declarations.
- No arbitrary distribution-upgrade graph or multi-hop engine. One ordered profile tuple, its final
  current entry, and the target profile's upgrade-from-previous policy drive one state machine.
- No checkpoint resource kind, arbitrary retention engine, user-authored name, public clone API, or
  automatic rollback. One VM-owned row and four vm-platform operations cover the required lifecycle.
- No database table mirroring remote package-stage state. The checkpoint table owns the recovery
  artifact lifecycle plus the single desired-state fingerprint required to reject unsafe restore.
- No automatic repair flags for arbitrary apt dependency failures.
- No automatic re-enable of unmanaged third-party repositories.
- No transparent clone/migrate command. Whole-VM restore is explicit and limited to the one managed
  checkpoint.
- No network-interface redesign unless a real Trixie certification failure proves it necessary.

These boundaries preserve the useful generality: an ordered release type, local mapping ownership,
an explicit core-to-platform create value, observed state, and an adjacent transition. They avoid
paying for arbitrary operating systems, arbitrary release graphs, or incompatible provider recovery
semantics.

## Validation strategy

### Unit and contract tests

- release parser accepts matching Debian codename/version pairs and rejects contradictions;
- database migration, exact schema inventory, converter, insert/update, backup serialization, and
  null legacy behavior, including pair-null enforcement without a codename-enumerating SQL check;
- relative support classification for current, previous, and legacy positions, including a synthetic
  profile append that changes positions without adding a codename-specific schema or
  public-interface field;
- no operator-facing schema or CLI accepts a release/image selection;
- the vm-platform descriptor and all six bundled implementations declare version 1, while an
  inconsistent version or incomplete implementation fails exact registration conformance;
- version-33 databases advance through a separate migration 34 without changing migration 33;
  checkpoint schema constraints, converters, backup projection, operation identity, desired-state
  fingerprinting, and compare-and-set state changes enforce one slot and replay-safe lifecycle
  transitions;
- every platform mapping contains Trixie values for each exposed architecture and uses the request
  release rather than a local default;
- a missing code-owned release mapping produces the platform-update error before backend mutation;
- pending-create preflight receives core's concrete release, while an empty or legacy Proxmox
  catalog remains loadable and a missing current mapping fails with the exact configuration key
  before boundary secret resolution, authenticated `runup`, provisional-row insertion, or backend
  access;
- Proxmox repeats the same mapping lookup inside `create`, and the manager preserves typed create
  failures, their remediation hints, and provisional-row unwind;
- every platform create path passes the request to the shared verifier before its rollback window
  closes, then core independently verifies the returned transport before success;
- a failed core verification leaves one failed, uninitialized row with the backend metadata needed
  for deletion and a typed delete/retry diagnostic;
- Proxmox legacy scalar maps only to Bookworm and cannot satisfy current-release creation;
- APT scalar/map exclusivity, codename scalar rejection, selected-map resolution, missing-map
  failure-before-mutation, generated samples, and plugin parity;
- list/describe human output and additive JSON v1 values/nulls;
- preflight blocker semantics and apt plan projection without prose-wording assertions;
- current/no-op, previous/upgrade, and legacy/refusal behavior without a multi-hop path;
- an incomplete direct journal outranks new eligibility after a later profile append, resumes only
  its recorded pair, and blocks a second journal;
- full plan recomputation after source-release update and renewed confirmation on every material
  change;
- APT simulation isolated from guest configuration/hooks plus enabled-stanza target-source coverage
  and distinct-filesystem space aggregation;
- native apt/dpkg ownership refusal plus automatic timer inhibit, safe restoration, and retained
  inhibition on mixed/unhealthy states, with all-timers-stopped reconfiguration and durable repair
  state when restoration fails;
- atomic progress/attempt transitions, interruption inside every action, the same lock around every
  journal write/reboot dispatch, attempt-identity fencing of stale coordinators, private non-symlink
  journal ownership, and no duplicate apt work;
- read-only checkpoint viability and provider inventory refusal before local backup creation, both
  local backup gates, provider-owned checkpoint creation/inventory agreement, and the first mutation
  confirmation before source mutation;
- every platform's offline create/list/restore/delete contract, including ownership/source binding,
  interrupted-operation replay, retained destructive-restore intermediates, and logical VM identity;
- Azure request-model serialization, WSL partial-export refusal before unregister, AWS temporary
  start/stop choreography with primary-error preservation, and Proxmox's one-hour checkpoint task
  timeout;
- checkpoint CLI cardinality, confirmations, VM filtering, names-only/JSON output, lifecycle versus
  restore-eligibility projection, automatic upgrade acquisition, resume matching, narrow
  shared/exclusive per-VM operation exclusion, reuse-time disclosure, explicit retention and billing
  cleanup guidance after success, and forced disowning only after ordinary provider cleanup fails;
- restore-time desired-state fingerprint refusal, release attestation, backward observation
  reconciliation, pending initialization that blocks convergence-dependent operations until reinit,
  retained checkpoint, and unchanged desired declarations;
- strict reconnect, native-route rejoin, Proxmox no-route failure, Trixie observation timing, and
  repair-required outcomes; and
- disk-backed large staging with a small `/tmp` tmpfs;
- DSA and `~/.pam_environment` absence, `/etc/sysctl.d` ownership, and `.list` plus `.sources`
  classification; and
- Bookworm interface inventory, Trixie-rule prediction refusal before reboot, and stable-name
  post-reboot verification.

The contract merge unit updates `cli/agentworks/capabilities/README.md`,
`cli/agentworks/capabilities/vm_platform/README.md`, and `cli/agentworks/plugins/README.md`. The
system-plugin example advertises internal vm-platform version 1 and teaches the required release
request, release-keyed lookup, platform-local live verification, returned transport, core-owned
matching observation, and managed checkpoint contract. Review checks all three documents without
tests that assert on authored prose. The kind's published topic prose and the `VMPlatform` operation
docstrings change in that same merge unit.

### Live certification

For every exposed architecture on Lima, WSL2, AWS, Azure, GCP, and Proxmox, certification proves
artifact lookup, boot, release observation, and delete. Each platform then runs one complete Phase
B, shell, workspace, backup, managed checkpoint create/list/restore/delete, reboot/reconnect, and
real Bookworm-to-Trixie upgrade, post-reboot interface verification, and cleanup. Shared APT-map
behavior and constrained-`/tmp` large transfer run once on representative Trixie guests plus the
cross-platform contract suite, rather than being repeated where no provider code participates.

Lima certification exercises the macOS default VZ path on the `msm4` environment, including retained
artifact inventory and complete cleanup. Focused tests retain the QEMU native-snapshot path and both
drivers' replay boundaries; a separate QEMU live VM is not required merely to repeat Lima's native
snapshot command surface.

Bookworm upgrade fixtures come from the last released Bookworm-creating Agentworks version or from
operator-approved prebuilt images. The run records the fixture's release, artifact provenance, and
cleanup. Current code receives no public or hidden Bookworm create selector for certification.

Expensive/destructive live runs follow the integration-testing environment's naming, budget,
inventory, recovery, and residue rules. A platform with no available official Trixie image or no
established recovery prerequisite remains explicitly uncertified rather than falling back.

### Documentation and closeout

Closeout searches active code and current documentation for unaccounted registered-release pins,
platform-local current defaults, scalar release-sensitive values, claims that VM backup is bootable,
unbounded guest `/tmp` staging, and an operator OS selector. It updates the new superseding ADR,
platform guides, resource guide, CLI reference, generated schema/sample collateral, relative support
teaching, and upgrade/recovery runbook in the same implementation series. The internal vm-platform
contract and every bundled implementation remain version 1 with one complete surface.
