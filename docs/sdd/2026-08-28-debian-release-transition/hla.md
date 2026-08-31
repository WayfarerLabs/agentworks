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

The transition has five cooperating parts:

1. `agentworks.debian` defines one ordered release-profile registry, derives current from its final
   profile, classifies relative support, parses `/etc/os-release`, and attaches each direct upgrade
   policy to its target profile.
2. Vm-platform contract version 3 requires `ProvisionRequest` to carry the concrete core-selected
   release. Platform-local maps turn it into the provider image selector, and platforms verify the
   live guest during their rollback-capable create window. Core independently probes the returned
   transport and owns the persisted observation. Old contract implementations fail conformance;
   missing release mappings fail clearly before backend mutation.
3. `vms.debian_release` and `vms.debian_release_observed_at` retain the last verified observation.
   Legacy rows begin unknown and are populated by a live probe.
4. A release value resolver chooses platform and APT values from explicit maps. The selected VM
   release, not current product policy, controls reinitialization of an existing guest.
5. A new VM manager workflow executes one durable `current-1` to `current` state machine. An
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

Vm-platform contract version 3 adds required `debian_release: DebianRelease` to `ProvisionRequest`.
`create_vm` always supplies the final registry profile's release; no caller parameter reaches that
assignment. `ProvisionResult` carries transport and backend identity, not a platform-authored
release proof. Bumping the kind descriptor from contract version 2 to 3 makes every built-in and
plugin implementation migrate as one hard cutover; exact registration conformance rejects a
version-2 implementation and names the incompatible platform/plugin.

No platform defines or imports a platform-local current release. Core passes one concrete release to
the pending create node and later uses that same value in `ProvisionRequest`. Before the command's
secret-resolution phase, the pending node calls the platform's pure, offline
`validate_create_release(release)` hook. The default is a no-op for code-owned catalogs. A platform
with an operator-owned catalog overrides it so a missing entry fails before authenticated `runup`,
while its config remains loadable for operations on existing VMs. The platform repeats the release
lookup as its first create action, so every mapping is enforced before backend mutation.

A shared resolver shapes two missing-key failures:

- a missing code-owned selector raises a typed state error naming `vm-platform/<name>`, the
  requested codename, and the need to update Agentworks or the plugin; and
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
sandbox for in-process plugin code. The ordering never persists requested intent or a platform claim
as observed state, and it never deletes the only row capable of targeting a backend that escaped
platform rollback.

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

One forward migration adds nullable columns to `vms`:

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

The database records the operator action: upgrade start, external checkpoint reference,
repair-required outcome, and completion. It does not emit release-discovery or target-observation
events because the VM row and its observation timestamp already own those facts. It also does not
copy every remote package action into a second mutable state machine. While the guest is reachable,
the durable guest journal is the authority for an in-progress upgrade. The database release is the
last verified observation, and the remaining events are the action audit trail.

This avoids a split-brain status where a local process dies after apt advances but before a database
write. A future invocation gives an incomplete journal precedence, probes the guest, then records
the result it can prove. Retained old target profiles keep their journal reader and direct policy
for forward recovery; that recovery entry point cannot start another transition or form a multi-hop
path.

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
agw vm upgrade NAME [--checkpoint REF]
```

`NAME` is the required operand and `--checkpoint` is an optional modifier in argv because an
interactive caller can supply it at the recovery prompt. The value is a non-blank, bounded,
single-line operator reference, not a secret. It can be a provider snapshot ID, backup ID, local
export path, or another identifier for the actual recovery artifact. A ticket number is insufficient
unless the referenced ticket itself identifies that artifact. The command has no `--target`,
`--release`, `--force`, or `--yes` option. The target is always the core current release.

The command has three interactive authorization points. The first attests that the named external
checkpoint exists and that console or rescue access was tested. Two mutation decisions follow: the
preliminary decision authorizes bringing the source release current within its existing suite, and
the final decision follows a reopened operation boundary plus a complete recomputation of the target
plan and authorizes switching suites. It shows removals, disabled repositories, backup paths,
checkpoint limitations, platform recovery guidance, material plan drift, and expected downtime. Once
an irreversible action exists, rerunning the command may inspect or resume it without asking the
operator to recreate completed work.

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
- release-specific blockers from the selected adjacent policy;
- active, broken, or unverifiable Agentworks sessions; and
- the simulated minimal/full upgrade package removals.

It also refuses when apt or dpkg's native locks show another package-manager owner. Before package
actions it records the current enabled/disabled state of `apt-daily.timer` and
`apt-daily-upgrade.timer`, then stops and inhibits them. A source-safe abort and a verified healthy
target completion restore that exact prior state. Once source-switch intent is durable, a mixed or
unhealthy package state keeps the timers inhibited and records that fact until forward repair proves
a healthy target or the operator restores the external checkpoint. The private Agentworks journal
lock prevents a second Agentworks runner; native locks and the timer lifecycle cover external
owners. Restoration first stops every known timer, reconfigures every recorded enablement state,
then restores activation and verifies the complete set. Failure before that proof records a durable
repair-required VM event rather than existing only as terminal output.

Deterministic unsafe conditions fail. Modified conffiles and packages whose release notes require
application-specific intervention fail with exact manual guidance; the first implementation does not
invent a general prompt-answer engine. Package removals are shown for operator review rather than
silently accepted. No concurrent Agentworks session may be live or unverifiable.

The preliminary preflight is not the suite-switch authorization. After backup, external checkpoint
attestation, and the first mutation confirmation, the manager brings the source release fully
current. Because that update and checkpoint activity can change dependencies or machine state, it
closes and reopens the ordinary VM operation boundary, then recomputes the complete preflight:
sessions, package health, holds, conffiles, blockers, sources, space, and simulated removals. It
shows the final plan, highlights any material difference from the preliminary plan, and requires the
second mutation confirmation before changing sources.

The target-release simulation is isolated from guest-owned APT policy. It uses generated canonical
target sources, scratch package indexes, scratch dpkg status and extended-state copies, no
preferences, and a primary scratch `APT_CONFIG` that redirects the main configuration to `/dev/null`
and configuration parts to an empty scratch directory before APT traverses either. Guest APT hooks
or other fragments therefore cannot execute during this read-only plan.

### Backup and recovery gate

Before source mutation the workflow completes two local artifacts:

1. the existing `vm backup`, which protects Agentworks metadata and workspace files; and
2. a compact release-upgrade bundle containing `/etc`, `/var/lib/dpkg`,
   `/var/lib/apt/extended_states`, package selections, package/source inventories, and the upgrade
   plan.

Both are written under the configured host backup root with owner-only permissions and a manifest
that identifies the VM, observed source release, requested target release, timestamp, and external
checkpoint reference. The bundle uses a secure root-owned disk-backed staging directory such as
`/var/tmp` for transfer and is removed after a verified local copy.

The manager prints platform-specific instructions for creating a bootable external checkpoint and
obtaining console or rescue access. It requires `REF` and explicit confirmation that the operator
created that artifact. The corresponding event preserves the reference for later diagnostics.

Agentworks does not call provider snapshot APIs. This keeps snapshot ownership, retention cost,
restore semantics, and cleanup in the operator's existing infrastructure process. The UI never calls
either local artifact a VM snapshot or claims an automatic rollback.

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
`full-upgrade-complete` and package-manager quiescence are reverified under the same lock. Reconnect
is strict: the current warning-and-continue reconnect helper is not used.

The post-reboot order is:

1. wait for provider power state and guest boot;
2. try canonical Tailscale SSH;
3. when that fails, try the platform's existing native transport and explicitly rejoin Tailscale
   using the pre-resolved key;
4. if no route works, preserve the remote progress as last known and record a local
   `repair-required` event with the external checkpoint reference plus provider console
   instructions;
5. observe and persist the target release as soon as `/etc/os-release` proves it;
6. verify dpkg/apt convergence and target source hygiene, the running target guest kernel or WSL2
   provider kernel, systemd, sshd, Tailscale, and Agentworks identities; source hygiene considers
   only enabled `.list` and deb822 stanzas, requires coverage of every policy target suite, and
   rejects enabled foreign suites or third-party URIs;
7. run Phase B with the target release and restore only selected mapped APT sources; and
8. record complete, or target observed with repair required when later verification/reinit fails.

Proxmox has no post-create native transport. Its failure path therefore stops after the canonical
reconnect attempt and directs the operator to the recorded Proxmox backup/console path. That is a
truthful platform limitation, not an excuse to proceed without verification.

The system never attempts an automatic downgrade. A partially upgraded package system is resumed
forward according to Debian's instructions or recovered externally by the operator.

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

`vm list` gains release and relative support columns after its names-only short circuit.
`vm describe` shows the last verified release, observation timestamp, derived `current`, `previous`,
or `legacy` status, and relevant upgrade events. JSON v1 gains additive nullable `debian_release`
and `debian_release_observed_at` fields; the command reference defines recognized-codename and null
semantics. Support position remains derived rather than persisted.

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
| Before first mutation confirmation | no remote mutation; DB observation unchanged                   | correct preflight and rerun                 |
| Backup failure                     | partial local artifact is not accepted                         | fix space/transport and rerun               |
| Source update failure              | last-completed/active actions and apt log                      | repair source release, then rerun           |
| Final plan differs                 | recomputed plan; suites still on source release                | review and confirm or stop                  |
| Source switch or apt failure       | progress, active attempt, sources backup, log, checkpoint ref  | repair forward or restore externally        |
| Local CLI/SSH interruption         | systemd unit plus progress/attempt state                       | rerun `vm upgrade` to inspect/resume        |
| Reboot never returns               | last known progress plus checkpoint and repair-required events | use platform console/checkpoint             |
| Target observed, health fails      | DB says target; repair-required event                          | repair target release and rerun             |
| Phase B warning/failure            | DB says target; init status partial/failed                     | fix mapped source or initializer and reinit |
| Manual adjacent upgrade found      | DB/live disagreement, no active journal                        | use `vm upgrade` adoption path              |

No failure path rewrites the database to the source release after the target has been observed. No
path calls the existing data-only VM backup a boot restore point.

## Deliberate boundaries and complexity guard

- No new public OS/image selection model.
- No release field in VM templates or core site declarations.
- No arbitrary distribution-upgrade graph or multi-hop engine. One ordered profile tuple, its final
  current entry, and the target profile's upgrade-from-previous policy drive one state machine.
- No provider snapshot capability, cleanup command, or rollback abstraction in this effort.
- No second database table mirroring remote stage state.
- No automatic repair flags for arbitrary apt dependency failures.
- No automatic re-enable of unmanaged third-party repositories.
- No whole-VM restore or transparent clone/migrate command.
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
- vm-platform contract version 2 implementations fail exact registration conformance after the
  version 3 cutover;
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
- external checkpoint attestation, both local backup gates, and the first mutation confirmation
  before source mutation;
- strict reconnect, native-route rejoin, Proxmox no-route failure, Trixie observation timing, and
  repair-required outcomes; and
- disk-backed large staging with a small `/tmp` tmpfs;
- DSA and `~/.pam_environment` absence, `/etc/sysctl.d` ownership, and `.list` plus `.sources`
  classification; and
- Bookworm interface inventory, Trixie-rule prediction refusal before reboot, and stable-name
  post-reboot verification.

The contract-version merge unit updates `cli/agentworks/capabilities/README.md`,
`cli/agentworks/capabilities/vm_platform/README.md`, and `cli/agentworks/plugins/README.md`. The
plugin example advertises version 3 and teaches the required release request, release-keyed lookup,
platform-local live verification, returned transport, core-owned matching observation, and failure
contract. Review checks all three documents without tests that assert on authored prose. The kind's
published topic prose and the `VMPlatform.create`, request, and result docstrings change in that
same merge unit so generated capability teaching does not retain the version 2 contract.

### Live certification

For every exposed architecture on Lima, WSL2, AWS, Azure, GCP, and Proxmox, certification proves
artifact lookup, boot, release observation, and delete. Each platform then runs one complete Phase
B, shell, workspace, backup, reboot/reconnect, and real Bookworm-to-Trixie upgrade with an
operator-established recovery checkpoint, post-reboot interface verification, and cleanup. Shared
APT-map behavior and constrained-`/tmp` large transfer run once on representative Trixie guests plus
the cross-platform contract suite, rather than being repeated where no provider code participates.

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
teaching, and upgrade/recovery runbook in the same implementation series. The contract authoring
surfaces already changed with version 3 and do not have a second cutover lifecycle.
