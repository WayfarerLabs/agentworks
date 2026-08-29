# HLA: Debian Release Transition

- Status: Draft
- Date: 2026-08-28
- Requirements: `frd.md`

## Architectural summary

The implementation adds one small core release model and threads it through the existing VM
lifecycle. Core owns which Debian release Agentworks creates. Platforms own how that release maps to
an image. The database owns the last release observed on a live VM. The initializer owns resolution
of release-aware package sources. None of those layers gains an operator release selector.

The transition has five cooperating parts:

1. `agentworks.debian` defines the closed release vocabulary, creation release, support policy,
   `/etc/os-release` parser, and adjacent upgrade policy.
2. `ProvisionRequest` carries the internally selected release. Platform-local maps turn it into the
   provider image selector, and create returns the release verified during the rollback-capable
   bootstrap window.
3. `vms.debian_release` and `vms.debian_release_observed_at` retain the last verified observation.
   Legacy rows begin unknown and are populated by a live probe.
4. A release value resolver chooses platform and APT values from explicit maps. The selected VM
   release, not current product policy, controls reinitialization of an existing guest.
5. A new VM manager workflow executes one durable Bookworm-to-Trixie state machine. It uses ordinary
   VM orchestration before and after the reboot, but package work and progress live under
   `/var/lib/agentworks` so an SSH or local CLI interruption cannot erase the operation.

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


CURRENT_DEBIAN_RELEASE = DebianRelease.TRIXIE

DEBIAN_RELEASES = {
    DebianRelease.BOOKWORM: DebianReleaseProfile(
        version_id="12",
        regular_support_ends=date(2026, 7, 11),
        lts_ends=date(2028, 6, 30),
    ),
    DebianRelease.TRIXIE: DebianReleaseProfile(
        version_id="13",
        regular_support_ends=date(2028, 8, 9),
        lts_ends=date(2030, 6, 30),
    ),
}
```

The implementation release adds the exact six-month Bookworm compatibility date to the Bookworm
profile and release documentation. A separate `UPGRADE_POLICIES[(BOOKWORM, TRIXIE)]` record owns
transition-specific checks, source suites, minimum OpenSSH version, and documentation links. This is
not a generic rule that assumes enum adjacency. Supporting the next Debian release requires adding
its profile and one reviewed adjacent transition policy.

The parser reads `ID`, `VERSION_ID`, and `VERSION_CODENAME` from `/etc/os-release` without sourcing
the file in a shell. A supported observation requires `ID=debian` and a codename/version pair that
matches one profile. Missing, contradictory, unknown, or non-Debian values produce a typed state
error containing the observed non-secret fields.

### Release value invariant

Every active implementation value whose correctness depends on Debian release has the shape
`Mapping[DebianRelease, T]`. A nested architecture map is valid when architecture is a second axis.
The mapping lives in the component that understands `T`:

- platform image selectors stay inside their platform modules;
- Debian archive suites and upgrade checks stay in `agentworks.debian`;
- third-party repository stanzas stay in their `apt-source` resources; and
- support dates stay in release profiles.

There is no global mega-catalog combining provider details. The common invariant and type are
shared; the domain knowledge remains local.

## Create pipeline

### Common request and result

`ProvisionRequest` gains a required `debian_release: DebianRelease`. `create_vm` always supplies
`CURRENT_DEBIAN_RELEASE`; no caller parameter reaches that assignment. `ProvisionResult` gains
`debian_release: DebianRelease`, meaning the live guest release the platform verified before its
create rollback window closed.

Each platform calls one shared release probe over the create-time transport it already controls. The
probe runs after the guest boots and before the platform returns success. A platform-specific
bootstrap that cannot execute the probe is not Trixie-certified. A mismatch raises while the
platform can still clean up its backend resource.

The database row begins with a null observed release. After `platform.create` returns a matching
Trixie result, the manager writes the release and observation timestamp before it begins Phase B.
This ordering gives Phase B an authoritative release for APT resolution and never writes an
unverified intended value as observed state.

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
image selectors because no shipped create path consumes them; Bookworm detection, APT values, and
the transition policy remain independently. Fixtures use prebuilt Bookworm VMs rather than a hidden
create selector.

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
requests Bookworm. Through 2028-06-30, legacy `template_vmid` is accepted only as the historic
Bookworm slot so an unchanged site remains loadable for existing VM operations. It never fills the
Trixie slot. Keeping the adapter through upgrade support means an unchanged site can still load for
backup, recovery, delete, and upgrade after ordinary Bookworm convergence ends.

The Proxmox setup script takes an explicit supported release and emits the corresponding official
cloud image URL and template name. Its documentation teaches a Trixie template by default.

## Persistence and observed state

### Schema

One forward migration adds nullable columns to `vms`:

```sql
debian_release TEXT,
debian_release_observed_at TEXT,
CHECK ((debian_release IS NULL) = (debian_release_observed_at IS NULL)),
CHECK (debian_release IS NULL OR debian_release IN ('bookworm', 'trixie'))
```

The pair is both null or both non-null. Repository/converter code exposes the release as
`DebianRelease | None`; arbitrary strings do not escape the database boundary. The migration also
updates the safer-migrations exact schema inventory and its migration fixtures.

No data backfill runs. In particular, the migration does not assume that all existing VMs are
Bookworm because Proxmox accepted operator templates and operators can modify guests outside
Agentworks.

### Discovery and reconciliation

Release-sensitive operations use a shared `verified_vm_release` service:

1. probe the live guest;
2. if the row is unknown, persist the supported observation and return it;
3. if row and guest agree, refresh `observed_at` and return it; and
4. if they disagree, stop before guest mutation and report both.

The one intentional reconciliation owner is `vm upgrade`. If the row says Bookworm but a healthy
guest already says Trixie and there is no active Agentworks upgrade journal, it treats this as an
operator-performed upgrade. It presents an adoption plan, validates the Trixie guest, records the
proved Trixie observation after confirmation, and then runs release-aware Phase B. A later Phase B
failure therefore cannot leave the row calling a Trixie guest Bookworm. Other lifecycle commands
point to this path instead of silently adopting a changed base system.

Release-insensitive recovery operations such as list, start, stop, shell, backup, and delete remain
usable with an unknown row. `vm list` never performs a live probe because it backs completion and
must remain fast and side-effect-free.

### Operation support policy

Observation answers what release the guest runs. A separate shared operation-policy gate answers
whether a requested mutation is supported on that release today. Every release-sensitive lifecycle
operation passes its operation kind, observed release, and current date through that gate before
guest mutation.

The Bookworm full-compatibility date stays unset and date-dependent enforcement stays dormant until
the final Trixie cutover commit supplies the exact release-derived date. After that date, the gate
permits inspection, recovery access, backup, delete, and `vm upgrade`, while refusing ordinary
Bookworm convergence. After 2028-06-30 it also refuses automated upgrade in current releases and
points to the recorded last supporting release. Tests inject dates immediately before, on, and after
both boundaries rather than depending on the wall clock.

### Events rather than a second progress truth

The database records the operator action: upgrade start, external checkpoint reference,
repair-required outcome, and completion. It does not emit release-discovery or Trixie-observation
events because the VM row and its observation timestamp already own those facts. It also does not
copy every remote package action into a second mutable state machine. While the guest is reachable,
the durable guest journal is the authority for an in-progress upgrade. The database release is the
last verified observation, and the remaining events are the action audit trail.

This avoids a split-brain status where a local process dies after apt advances but before a database
write. A future invocation probes the guest and journal, then records the result it can prove.

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
token equal to a supported Debian codename. This makes the mapping rule enforceable without
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
using the VM's verified release. Generated samples, schema/explain output, graph publication, and
plugin parity tests expose both valid forms.

### Upgrade source handling

The distribution upgrade does not blindly rewrite every `bookworm` string. It classifies active APT
sources into:

- canonical Debian and Debian security sources;
- selected Agentworks-managed third-party source files; and
- unmanaged or unselected third-party source files.

Before switching suites, it copies the full source estate into the local recovery bundle and remote
journal directory. It disables every non-Debian source for the distribution package stages. It then
writes one canonical deb822 `debian.sources` file pinned to the Trixie suites from the transition
policy and removes enabled Bookworm Debian entries.

After Trixie is observed, Phase B recreates selected Agentworks-managed sources from their Trixie
mapping. Unmanaged sources stay disabled with their original filename and content preserved and
listed in the final result. Agentworks never guesses whether an unmanaged repository supports
Trixie.

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
`--release`, `--force`, or `--yes` option in this first transition.

The command makes two interactive decisions. The preliminary decision follows backup/checkpoint and
authorizes bringing Bookworm current. The final decision follows a reopened operation boundary and a
complete recomputation of the Trixie plan, and authorizes switching suites. It shows removals,
disabled repositories, backup paths, checkpoint limitations, platform recovery guidance, material
plan drift, and expected downtime. Once an irreversible action exists, rerunning the command may
inspect or resume it without asking the operator to recreate completed work.

The manager owns validation and typed errors. The Typer command only maps argv to the service call.
The workflow opens the ordinary VM operation boundary to resolve the site credentials, canonical
transport, operator SSH identity, and VM template Tailscale key before mutation. This follows the
rekey boundary because reconnect may need to rejoin Tailscale; `reinit` does not resolve that
secret.

### Preflight

The read-only preflight records a plan containing:

- database and live release;
- architecture and current kernel;
- dpkg audit result and held packages;
- Bookworm point state and OpenSSH package version;
- installed kernel metapackage;
- separate `/boot` size/free space when present;
- free space for `/`, `/var`, and the apt archive estimate;
- APT preferences, backports, proposed-updates, mixed suites, and third-party sources;
- installed non-Debian and obsolete packages;
- package-owned conffiles whose current hash differs from dpkg's recorded hash;
- release-specific blockers from the Bookworm-to-Trixie policy;
- active, broken, or unverifiable Agentworks sessions; and
- the simulated minimal/full upgrade package removals.

It also refuses when apt or dpkg's native locks show another package-manager owner. Before package
actions it records the current enabled/disabled state of `apt-daily.timer` and
`apt-daily-upgrade.timer`, then stops and inhibits them. A Bookworm-safe abort and a verified
healthy Trixie completion restore that exact prior state. Once source-switch intent is durable, a
mixed or unhealthy package state keeps the timers inhibited and records that fact until forward
repair proves healthy Trixie or the operator restores the external checkpoint. The private
Agentworks journal lock prevents a second Agentworks runner; native locks and the timer lifecycle
cover external owners.

Deterministic unsafe conditions fail. Modified conffiles and packages whose release notes require
application-specific intervention fail with exact manual guidance; the first implementation does not
invent a general prompt-answer engine. Package removals are shown for operator review rather than
silently accepted. No concurrent Agentworks session may be live or unverifiable.

The preliminary preflight is not the suite-switch authorization. After backup, external checkpoint,
and the first confirmation, the manager brings Bookworm fully current. Because that update and
checkpoint activity can change dependencies or machine state, it closes and reopens the ordinary VM
operation boundary, then recomputes the complete preflight: sessions, package health, holds,
conffiles, blockers, sources, space, and simulated removals. It shows the final plan, highlights any
material difference from the preliminary plan, and requires the second confirmation before changing
sources.

### Backup and recovery gate

Before source mutation the workflow completes two local artifacts:

1. the existing `vm backup`, which protects Agentworks metadata and workspace files; and
2. a compact release-upgrade bundle containing `/etc`, `/var/lib/dpkg`,
   `/var/lib/apt/extended_states`, package selections, package/source inventories, and the upgrade
   plan.

Both are written under the configured host backup root with owner-only permissions and a manifest
that identifies the VM, observed Bookworm release, timestamp, and external checkpoint reference. The
bundle uses a secure root-owned disk-backed staging directory such as `/var/tmp` for transfer and is
removed after a verified local copy.

The manager prints platform-specific instructions for creating a bootable external checkpoint and
obtaining console or rescue access. It requires `REF` and explicit confirmation that the operator
created that artifact. The corresponding event preserves the reference for later diagnostics.

Agentworks does not call provider snapshot APIs. This keeps snapshot ownership, retention cost,
restore semantics, and cleanup in the operator's existing infrastructure process. The UI never calls
either local artifact a VM snapshot or claims an automatic rollback.

### Durable remote state machine

The upgrade owns a fixed root directory:

```text
/var/lib/agentworks/debian-upgrades/bookworm-to-trixie/
  lock
  plan.json
  state.json
  sources-before/
  upgrade.sh
  upgrade.log
```

Root owns the directory at mode 0700. `state.json` separates monotonic progress from the current
attempt and its outcome. Its conceptual shape is:

```json
{
  "version": 1,
  "attempt_id": "...",
  "last_completed": "prepared",
  "active_action": "bookworm-update",
  "active_started_at": "...",
  "boot_id_before": null,
  "outcome": "running",
  "failure": null
}
```

`last_completed` has six ordered values: `prepared`, `bookworm-current`, `sources-switched`,
`minimal-upgrade-complete`, `full-upgrade-complete`, and `reboot-complete`. `active_action` names
the action attempting the next transition. `outcome` and `failure` describe the attempt without
erasing progress. Trixie observation stays in the VM row, Phase B stays in existing init state, and
overall completion/repair stays in VM events.

Before every mutation, the actor takes the one fixed non-blocking journal `flock`, reconciles the
current state, and atomically writes a new attempt identity, active action, start time, and, for
reboot, the current boot ID. The package service holds that same lock for its remote action; the
manager takes it around any journal claim/write and reboot dispatch. After an actor proves the
action's postcondition, it atomically advances `last_completed` and clears the active fields before
releasing the lock. An interruption inside an action therefore leaves intent visible. A retry takes
the same lock and checks the systemd unit, native apt/dpkg locks, logs, and that action's
postcondition before it either advances, safely reruns, or requires manual repair. It never equates
a missing success write with failure or success, and no second coordinator exists beside the lock
and journal.

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

Reboot is a separate action after the package unit reports success. With Trixie's installed udev
rules now authoritative, the manager runs interface-name prediction and blocks reboot with pinning
guidance if connectivity would be unsafe. It then takes the journal lock, records reboot intent and
the pre-reboot boot ID, dispatches reboot inside that critical section, closes the pre-upgrade
activation span, and opens a fresh post-reboot orchestration span. A changed boot ID proves reboot
completion; an unchanged boot ID permits safely dispatching reboot again only after
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
5. observe and persist Trixie as soon as `/etc/os-release` proves it;
6. verify dpkg/apt, the running Trixie kernel, systemd, sshd, Tailscale, and Agentworks identities;
7. run Phase B with the Trixie release and restore only selected mapped APT sources; and
8. record complete, or Trixie observed with repair required when later verification/reinit fails.

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

## CLI, diagnostics, and machine output

`vm list` gains a release column after its names-only short circuit. `vm describe` shows the last
verified release, observation timestamp, support status, and relevant upgrade events. JSON v1 gains
additive nullable `debian_release` and `debian_release_observed_at` fields; the command reference
defines their closed values and null semantics.

The completion spec maps `vm.upgrade`'s `name` operand to VM names. `vm upgrade` itself has no JSON
mode in this first interactive release. Durable logs and recovery artifacts are file outputs, while
human progress uses the ordinary output facade.

Doctor reports:

- unknown release on legacy rows;
- live/recorded disagreement when it can probe safely;
- Bookworm inside or beyond the compatibility window once the cutover policy is active;
- an incomplete remote upgrade journal;
- disabled unmanaged APT sources after upgrade; and
- Trixie `/tmp` staging hazards if an old implementation left the paths in use.

## Failure and integrity behavior

| Failure point                  | Durable truth                                                  | Next action                                 |
| ------------------------------ | -------------------------------------------------------------- | ------------------------------------------- |
| Before first confirmation      | no remote mutation; DB observation unchanged                   | correct preflight and rerun                 |
| Backup failure                 | partial local artifact is not accepted                         | fix space/transport and rerun               |
| Bookworm update failure        | last-completed/active actions and apt log                      | repair Bookworm, then rerun                 |
| Final plan differs             | recomputed plan; suites still Bookworm                         | review and confirm or stop                  |
| Source switch or apt failure   | progress, active attempt, sources backup, log, checkpoint ref  | repair forward or restore externally        |
| Local CLI/SSH interruption     | systemd unit plus progress/attempt state                       | rerun `vm upgrade` to inspect/resume        |
| Reboot never returns           | last known progress plus checkpoint and repair-required events | use platform console/checkpoint             |
| Trixie observed, health fails  | DB says Trixie; repair-required event                          | repair Trixie and rerun                     |
| Phase B warning/failure        | DB says Trixie; init status partial/failed                     | fix mapped source or initializer and reinit |
| Manual Trixie upgrade detected | DB/live disagreement, no active journal                        | use `vm upgrade` adoption path              |

No failure path rewrites the database back to Bookworm after Trixie has been observed. No path calls
the existing data-only VM backup a boot restore point.

## Deliberate boundaries and complexity guard

- No new public OS/image selection model.
- No release field in VM templates or core site declarations.
- No generic distribution-upgrade engine. One explicit adjacent policy drives one state machine.
- No provider snapshot capability, cleanup command, or rollback abstraction in this effort.
- No second database table mirroring remote stage state.
- No automatic repair flags for arbitrary apt dependency failures.
- No automatic re-enable of unmanaged third-party repositories.
- No whole-VM restore or transparent clone/migrate command.
- No network-interface redesign unless a real Trixie certification failure proves it necessary.

These boundaries preserve the useful generality: a closed release type, local mapping ownership,
observed state, and an explicit adjacent transition. They avoid paying for arbitrary operating
systems, arbitrary release graphs, or incompatible provider recovery semantics.

## Validation strategy

### Unit and contract tests

- release parser accepts matching Debian codename/version pairs and rejects contradictions;
- database migration, exact schema inventory, converter, insert/update, backup serialization, and
  null legacy behavior, including rejection of both mismatched release/timestamp null combinations;
- operation-policy behavior immediately before, on, and after both Bookworm deadlines;
- no operator-facing schema or CLI accepts a release/image selection;
- every platform mapping contains Trixie values for each exposed architecture and uses the request
  release;
- every create path verifies and returns the live release before success;
- Proxmox legacy scalar maps only to Bookworm and cannot satisfy Trixie creation;
- APT scalar/map exclusivity, codename scalar rejection, selected-map resolution, missing-map
  failure-before-mutation, generated samples, and plugin parity;
- list/describe human output and additive JSON v1 values/nulls;
- preflight blocker semantics and apt plan projection without prose-wording assertions;
- full plan recomputation after Bookworm update and renewed confirmation on every material change;
- native apt/dpkg ownership refusal plus automatic timer inhibit, safe restoration, and retained
  inhibition on mixed/unhealthy states;
- atomic progress/attempt transitions, interruption inside every action, the same lock around every
  journal write/reboot dispatch, and no duplicate apt work;
- external checkpoint and both local backup gates before source mutation;
- strict reconnect, native-route rejoin, Proxmox no-route failure, Trixie observation timing, and
  repair-required outcomes; and
- disk-backed large staging with a small `/tmp` tmpfs;
- DSA and `~/.pam_environment` absence, `/etc/sysctl.d` ownership, and `.list` plus `.sources`
  classification; and
- Bookworm interface inventory, Trixie-rule prediction refusal before reboot, and stable-name
  post-reboot verification.

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

Closeout searches active code and current documentation for unaccounted Bookworm pins, scalar
release-sensitive values, claims that VM backup is bootable, unbounded guest `/tmp` staging, and an
operator OS selector. It updates the new superseding ADR, platform guides, resource guide, CLI
reference, generated schema/sample collateral, support dates, and upgrade/recovery runbook in the
same implementation series.
