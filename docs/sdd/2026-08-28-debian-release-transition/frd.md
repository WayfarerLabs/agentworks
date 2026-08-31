# FRD: Debian Release Transition

- Status: Draft
- Date: 2026-08-28
- Scope: reusable Debian release lifecycle, Trixie creation, and the first adjacent VM upgrade

## Rulings this SDD rests on

- **Debian remains the one supported guest operating system.** Agentworks does not add a general
  operating-system selector, a custom image field, or a best-effort bring-your-own distribution
  mode. This preserves the consistency decision in ADR 0002 while superseding its fixed Bookworm
  release.
- **Agentworks has exactly one current Debian release.** New VMs always target that core-selected
  release. This effort advances it from Bookworm to Trixie. A future release promotion is expected
  to reuse the same registry, create contract, CLI, support policy, and adjacent-upgrade workflow,
  but it still requires a release-specific profile, mappings, upgrade policy, and certification.
  Evidence from that release may require separately scoped internal workflow changes.
- **The supported upgrade edge is release-relative.** Agentworks supports `current-1` to `current`
  through `agw vm upgrade NAME`. It never supports a direct or chained `current-2` upgrade.
- **Old VMs remain operable.** A recognized `current-2` or older VM remains available on a
  best-effort basis, with a warning when an operation accesses it. Release age alone does not brick
  the VM or block an otherwise viable lifecycle operation.
- **The guest release is durable state.** Agentworks records the last release it verified on each
  VM. It does not hide that cross-platform fact in platform metadata or infer it forever from the
  current product default.
- **Release-varying values are mappings.** Platform image selectors, third-party APT stanzas, and
  other release-specific facts are keyed by Debian release. A literal branch or value that merely
  happens to work for one release is not the model.
- **Core selects; platforms translate.** The common VM create request carries the concrete current
  Debian release. A platform maps that value to its own artifact and never infers a platform-local
  meaning of "current". A platform that cannot create the requested release fails clearly rather
  than choosing another release.
- **Platform configuration may supply a platform artifact, but not a core release choice.** This is
  necessary for Proxmox, where the operator owns the template catalog. The core still selects
  Trixie; the Proxmox site maps that release to a template VMID.
- **Agentworks owns the upgrade recovery checkpoint.** The vm-platform contract creates, lists,
  restores, and deletes named checkpoints. Core permits one managed checkpoint per VM, creates it
  automatically before an upgrade mutates packages, and retains it until the operator explicitly
  restores or deletes it. Provider-native artifacts remain implementation details, not a new
  declarable resource kind.

## Why now

Debian 13 Trixie became stable on 2025-08-09. Agentworks still creates Bookworm VMs and hardcodes
Bookworm in five platform implementations and several shipped APT sources. Continuing that posture
creates new legacy machines while delaying the migration work that already exists. Treating this as
a one-off string replacement would repeat the same design work for the next stable Debian release.

The change is broader than replacing image strings. Existing rows do not record their guest release,
Proxmox templates were never release-validated, the initializer cannot select a release-specific APT
value, and the current VM backup is not a bootable rollback point. Trixie also makes `/tmp` a
memory-backed filesystem by default, while current VM backup and workspace-copy paths stage
unbounded archives there.

## Requirements

### R1: One ordered Debian release model

Agentworks has one typed, ordered Debian release-profile registry whose initial values are
`bookworm` and `trixie`. The final profile is current by definition, so there is no separate current
pointer that can disagree with the order. Trixie becomes the final profile in this effort. The
immediate predecessor is `current-1`; every earlier recognized release is `current-2` or older.
These positions are derived from the one registry, never persisted as VM state.

Promoting a future stable Debian release implements and certifies its release-specific mappings and
adjacent upgrade policy, then appends one profile to the registry. Every non-first profile owns the
reviewed policy for upgrading from the profile immediately before it; non-adjacent policy edges are
not representable. The append makes the new profile current without changing a separate setting. The
stable product contract is that promotion does not add a second current setting, an
operator-selectable target, a codename-specific database migration, a new CLI command, or an
arbitrary upgrade graph; the existing concrete-release create field is reused. This is not a promise
that release-specific workflow internals remain unchanged. The concrete release is internal product
policy, not an operator-selectable VM field.

The following surfaces remain absent:

- no `vm create --release`, `--os`, or `--image` option;
- no VM-template release field;
- no core or platform setting that asks which Debian release to create;
- no arbitrary AMI, Azure image, GCP image, Lima image, or WSL rootfs override; and
- no support claim for Ubuntu, Fedora, or another distribution.

### R2: Current-release creation on every supported platform

Every successful `vm create` provisions the core's current release, which is Trixie after this
cutover. The VM manager writes that concrete release into the common provision request. Each
platform consumes the request value and selects its own artifact from a release-keyed mapping. Core
never sends `current-1` for creation, even when an operator-owned catalog retains that entry for
compatibility with existing VMs:

| Platform | Trixie source                                                        |
| -------- | -------------------------------------------------------------------- |
| Lima     | Debian cloud `debian-13-generic-{amd64,arm64}.qcow2`                 |
| WSL2     | Docker Official Image tag `debian:trixie`                            |
| AWS EC2  | Debian public SSM release 13 parameter for the selected architecture |
| Azure VM | `Debian:debian-13:13-gen2:latest`                                    |
| GCP GCE  | `debian-cloud` families `debian-13` and `debian-13-arm64`            |
| Proxmox  | the site's `trixie` template VMID                                    |

A platform has no independent default or `current` constant. If its code-owned mapping does not
contain the requested release, creation fails before backend mutation with an error that names the
platform, release, and need to update Agentworks. If an operator-owned map, such as a Proxmox site's
template map, lacks the key, the error instead names the site setting that must be supplied. Neither
case falls back to another release.

The required request field expands the internal vm-platform capability contract. That complete
contract remains version 1, and the descriptor plus all six bundled implementations mutate
atomically. Exact contract-version conformance catches an inconsistent bundled implementation as a
curation error; there is no external compatibility cutover or adapter. A conforming platform must
verify `/etc/os-release` during its rollback-capable create window so it can clean up a mismatch.
Core then independently probes the returned transport and persists only its own observation. A
mismatch, missing release, non-Debian guest, unavailable official image, or missing Proxmox Trixie
template fails creation before Agentworks reports success. New creation never falls back to Bookworm
or an operator-supplied generic image.

An operator-owned artifact requires an additional fail-closed boundary. Once the guest is live
enough to inspect, the platform verifies its release before running Agentworks bootstrap. Proxmox
does this through the QEMU guest agent after the clone starts. Core performs the ordinary final live
probe after the platform returns. The first check prevents Debian-specific mutation of a wrong
template; the core check attests the final handoff. Neither check has an operator bypass.

The manager preserves the distinct code-owned and operator-owned missing-map errors, including their
remediation hints, across its provisioning wrapper. A compliant platform rolls back a live release
mismatch before it raises. Core does not trust a platform's success claim: after persisting the
returned backend identifiers, it independently verifies the returned transport. A failed core probe
retains one failed, uninitialized VM row so `vm delete` can still address the backend; it does not
report success or discard the only cleanup handle.

The provisioning section identifies the selected Debian release in its opening creation line,
announces core's independent release confirmation, and emits an explicit completion line only after
the provisioning phase has completed.

The feature does not claim a platform until its real create, initialize, reboot, reconnect, backup,
and delete path has passed the Trixie certification matrix for every architecture Agentworks exposes
on that platform. The implementation updates the capability-model README, vm-platform README, and
system-plugin README so maintainers can predict this request, mapping, failure, verification, and
contract-version behavior.

### R3: Persist the last verified guest release

Each VM row stores the last Debian codename Agentworks verified from the live guest, plus when it
was observed. This is a cross-platform lifecycle fact and is not stored in opaque platform metadata,
desired instance spec, or a declaration file.

New VMs record the requested current release only after create-time verification. Existing rows
migrate to `unknown`; the database migration does not assume they are Bookworm. A release-sensitive
live operation probes `/etc/os-release`, records a recognized result, and refuses on a non-Debian or
unrecognized release. This is particularly important for Proxmox and for VMs an operator may already
have upgraded by hand.

`vm list`, `vm describe`, and JSON v1 expose the recognized Debian codename or `null`. `null` means
no live observation has established the fact. It is not rendered as Bookworm and is not treated as
drift. The compact list renders only the codename; `vm describe` also renders the derived relative
support tier. Adding a future recognized release does not require a database schema migration. Where
a live probe disagrees with the last verified row, the release-sensitive operation stops and reports
both values before changing the guest or database. `vm upgrade` owns the one reconciliation path: it
can validate and adopt an already-current guest after explicit operator confirmation rather than
strand a VM that was upgraded outside Agentworks. Trixie is the first current target.

### R4: Every release-specific value is selected from a release map

Platform image records are maps from the registered Debian release value to the full platform
selector, including associated facts such as Azure's minimum OS disk size. Architecture remains a
subordinate axis where required. Platform code does not scatter codename conditionals.

An `apt-source` supports exactly one of these forms:

- `source`, a scalar stanza that is genuinely release-independent; or
- `sources`, a release-keyed map of stanzas.

The two forms are mutually exclusive. A selected release-specific source that lacks a value for the
guest's release fails before any key, source file, or package mutation. Shipped source entries use a
map wherever the vendor's supported value depends on release. The mapped value is vendor policy, not
mechanical codename substitution: a Trixie mapping may legitimately contain a vendor suite named
`bookworm` when that is the vendor-supported repository.

The initializer receives the VM's verified release and resolves mappings before configuring APT.
Operator manifests may keep a scalar for a release-neutral repository, but a scalar containing a
supported Debian codename is rejected with guidance to use `sources`.

The same rule applies to future release-sensitive constants. A reviewer must be able to search for
every registered codename in active implementation code and account for each occurrence as either a
release model definition, a mapping key/value, a transition-specific fixture, or documentation.
Provider selector contract tests account for numeric release values without treating unrelated
version-like literals as findings.

### R5: A safe, resumable `vm upgrade` workflow

`agw vm upgrade NAME` upgrades one verified `current-1` VM to `current`. It has no target-release
option and is not an alias or flag on `vm reinit`. The first active transition policy is
Bookworm-to-Trixie. The workflow, journal, and CLI use the selected adjacent source and target
rather than hardcoding that pair as their architecture.

On entry, the command checks for an incomplete Agentworks upgrade journal before deriving a new
transition from the current registry. If exactly one exists, it resumes or diagnoses that journal's
recorded adjacent pair even when a later release promotion has made its source `current-2`. This is
recovery of work Agentworks already started, not permission to begin a new legacy upgrade. Multiple
incomplete journals fail with repair guidance, and no new journal starts beside an incomplete one.

Only a VM with no incomplete journal enters new-upgrade eligibility. An already-current VM reports
that no upgrade is needed. A `current-2` or older VM never starts a direct or automatic multi-hop
upgrade; the command refuses with the observed/current releases and the fresh-VM/data-copy path. A
new adjacent transition also refuses before mutation when the target profile has no reviewed
upgrade-from-previous policy.

Before changing Debian sources, the command must:

1. activate and verify the named VM and resolve every credential needed for reconnect or Tailscale
   rejoin;
2. observe `current-1` and reconcile that observation with the database;
3. announce the session check before expensive package planning, then fail fast when any Agentworks
   session is active, broken, or unverifiable; the error names each blocked session and gives the
   exact VM-filtered stop and verification commands, without stopping sessions automatically;
4. verify package database health, package holds, architecture, kernel metapackage on platforms
   where the guest owns its kernel, APT pins, third-party and backports sources, modified package
   conffiles, minimum `/boot` space, and sufficient aggregate space on each distinct filesystem
   backing `/`, `/var`, the package cache, and `/boot`;
5. enforce the source release's OpenSSH minimum needed for a reconnectable remote upgrade and run
   the pair-specific blocker checks captured from the target release's Debian release notes;
6. show the preliminary packages apt plans to remove and every source Agentworks will disable or
   replace;
7. check the checkpoint database slot and provider inventory without mutation, refusing an unusable
   or disagreeing retained artifact before creating backup files;
8. complete the ordinary Agentworks metadata/workspace backup and a local Debian recovery bundle
   containing `/etc`, dpkg and apt state, package selections, and the pre-upgrade source files; and
9. create and verify one Agentworks-managed, offline VM checkpoint through the bound platform.

If the VM has named consoles, the fresh-upgrade preflight warns that the required reboot ends their
live tmux state and any console-only shell processes. The persisted console definitions and session
membership survive and can rebuild on the next attach; the absence of a console-stop command does
not allow the upgrade to proceed with live sessions.

The command emits an initial inspection line immediately and announces each remote preflight group.
Every durable source-update, source-switch, minimal-upgrade, and full-upgrade action has tracked
progress and an explicit completion, failure, or interruption result. Reconnect, release
attestation, network verification, target health, release-aware initialization, and timer
restoration are also announced, so a long remote operation never begins as an unexplained silent
wait.

The Agentworks backups remain data-recovery artifacts, not bootable VM checkpoints. The managed
checkpoint is a separate platform-owned artifact whose lifecycle Agentworks controls through the
vm-platform contract. Core generates its name; the operator neither supplies a reference nor attests
to work performed outside Agentworks. A fresh upgrade refuses an existing checkpoint rather than
replacing it or assuming it belongs to the transition. A resumed upgrade requires the recorded
checkpoint and the platform's live inventory to agree.

The first mutation confirmation authorizes bringing the source release current at its existing
suite, not switching Debian suites. After that update, the command reopens the operation boundary
and recomputes every package, source, conffile, blocker, and space fact. It shows the final plan and
requires a second mutation confirmation before switching suites; any material drift from the
preliminary plan is explicit. For the first transition it then follows Debian's supported direct
Bookworm-to-Trixie procedure:

- bring Bookworm fully current before changing suites;
- disable non-Debian APT sources and preserve their original files;
- write canonical codename-pinned Trixie Debian sources;
- run the scripted `apt-get` equivalent of the documented minimal upgrade and full upgrade;
- ensure the Trixie kernel is installed on guest-kernel platforms; WSL2 instead verifies the
  Microsoft-managed provider kernel because a WSL distribution does not own a Debian kernel;
- predict interface names with the installed Trixie udev rules and block reboot when an unsafe
  rename still needs operator pinning; WSL2 records the provider-managed names instead;
- reboot;
- reconnect strictly, using an existing platform-native route and explicit Tailscale rejoin where
  available when the canonical path does not return;
- verify `/etc/os-release`, dpkg/apt health, the running kernel, systemd, sshd, Tailscale, and the
  Agentworks users and paths; and
- rerun Phase B initialization with Trixie-selected Agentworks APT sources.

APT work runs from a durable, root-owned remote state directory rather than `/tmp`. Before each
mutation it records the attempt and active action; after success it advances a separate
last-completed action. Its progress, outcome, script, and output survive an SSH interruption and
reboot. Re-running `vm upgrade` inspects an interrupted active action rather than guessing that it
succeeded. Every completion, failure, retry, and repeated reboot dispatch compares the attempt
identity it observed with the still-active identity under the journal lock, so a stale coordinator
cannot write over a newer attempt. The root, pair directories, lock, plan, and state reject
symlinks, wrong ownership, and non-private modes. It never starts a second package manager, and it
owns a bounded inhibit/restore lifecycle for Debian's automatic APT timers while also respecting
apt/dpkg's native locks.

Agentworks does not automatically restore a checkpoint or attempt a package-level distribution
downgrade. Before the guest proves Trixie, the database retains its last verified release. Once a
reconnecting probe proves Trixie, the row records Trixie even if a later initializer or health check
needs attention, so the database does not call a Trixie guest Bookworm. The command and VM events
distinguish `complete` from `Trixie observed, repair required`. The completed upgrade names its
retained checkpoint, warns that provider storage charges may continue, and gives the exact deletion
command. The checkpoint remains until the operator explicitly deletes it.

### R6: Support is relative to the current release

The support contract has no Agentworks calendar cutoff:

- `current` receives full support and is the only release used for new creation;
- `current-1` receives supported ordinary operations and the supported adjacent `vm upgrade`; and
- `current-2` or older receives best-effort ordinary operations with a warning, but no supported
  `vm upgrade`.

When a release profile is appended, existing VMs change tier automatically from their observed
release and position in the ordered registry. No migration rewrites their release and no database
field stores the tier. Debian lifecycle dates may inform diagnostics and release planning, but they
do not create a second Agentworks support clock.

The shared VM operation boundary warns once per command when it accesses a recognized `current-2` or
older VM. Release age alone does not refuse start, stop, inspect, shell, exec, backup, delete,
reinit, or another ordinary operation. Normal concrete safety and capability checks still apply, and
a release-specific mutation may fail clearly when a retained mapping or upstream facility is no
longer usable. This is best effort, not a compatibility guarantee.

`vm upgrade` is the deliberate exception. With no incomplete journal, it accepts only `current-1`
and refuses `current-2` or older without attempting a chained upgrade. An existing incomplete
journal remains recoverable under its original adjacent policy after a later profile append. Its
recovery guidance points to forward repair or external restore; a legacy VM with no journal gets the
new-current-VM and data-copy guidance.

### R7: Trixie operational changes are part of certification

No size-unbounded archive or transfer is staged in guest `/tmp`. VM backup and workspace copy move
large staging to a secure disk-backed location. Small scripts, sockets, and bounded status files may
remain in `/tmp` when their lifetime and size make tmpfs appropriate.

Certification also covers the Trixie changes relevant to Agentworks: DSA key rejection, no reliance
on `~/.pam_environment`, persistent sysctl settings under `/etc/sysctl.d`, possible network
interface renames, the new APT source format, and reconnection after sshd and kernel changes. Each
item has an assigned proof: source audits and SSH fixtures for DSA and PAM environment use, path
assertions for sysctl drop-ins, live `udevadm` interface-name inventory on Bookworm followed by
prediction after Trixie rules are installed but before reboot and a post-reboot comparison, and
parser/upgrade tests covering both `.list` and deb822 `.sources` files.

### R8: The operator can understand and recover the transition

The permanent CLI reference, upgrade guide, platform guides, apt-resource teaching, sample output,
and machine-output contract explain:

- Trixie-only creation and the absence of an OS selector;
- the recorded release and what `unknown` means;
- the current, previous, and legacy release-relative support tiers;
- preflight blockers and the managed checkpoint lifecycle;
- the exact `vm upgrade` stages and resumability behavior;
- how to recover or continue from every durable failure stage;
- which third-party sources remain disabled and how to review them; and
- how the fresh-VM/workspace-copy fallback differs from an in-place upgrade.

The permanent capability-model README explains that a consuming domain may pass a required,
domain-owned value through an operation request. The vm-platform README specifies the concrete
Debian release field, platform-local mapping, internal version-1 contract, missing-release error,
live verification, and tests required of every bundled platform. The system-plugin README moves its
example and create/checkpoint-contract teaching to version 1 in the same merge unit. These updates
ship with the contract implementation, not ahead of behavior.

ADR 0002 is superseded by a new ADR that retains Debian as the one guest OS while moving the fixed
release through a controlled, recorded product lifecycle.

### R9: Managed VM checkpoints provide the recovery boundary

Vm-platform contract version 1 includes four required operations over a small checkpoint descriptor:
create a core-named checkpoint, list the VM's Agentworks-managed checkpoints, restore one, and
delete one. The descriptor and all six bundled implementations move together. There is no optional
capability flag, default method that reports unsupported, external compatibility promise, or version
bridge, because `vm upgrade` depends on this recovery boundary on every supported platform.

Core persists checkpoint ownership separately from VM platform metadata in a new forward database
migration. The already-shipped migration that added Debian observations remains byte-for-byte
unchanged. A checkpoint row records the VM, core-generated name, provider identifier when known,
lifecycle state and operation identity, desired-state fingerprint, optional adjacent upgrade pair,
immutable observed Debian release at capture, and creation time. The operator-facing purpose is
derived from whether the adjacent pair is present rather than stored as a duplicate fact. A
standalone create copies the recognized persisted VM observation under the exclusive guard; if none
exists, it fails with guidance to start and observe or reinitialize the VM, stop it, and retry.
Upgrade instead uses its fresh pre-stop observation. The schema and manager enforce at most one
managed checkpoint per VM. The provider API remains plural so Agentworks can detect duplicates,
missing artifacts, and interrupted creation rather than trusting only local state. Checkpoints are
durable VM-owned operational state, not manifests, registry resources, template state, or
desired/applied instance records.

Checkpoint creation is offline. The VM must be stopped before the platform captures it, and the
platform must verify that the resulting artifact is complete, owned by Agentworks, and bound to the
exact VM incarnation before core marks it ready. Core passes the persisted create operation identity
and whether this is a resumed creating row, so a provider does not duplicate an ambiguous prior
request. Restore receives a persisted restore-attempt identity: retrying the same interrupted
attempt converges, while a later explicit operator restore has a new identity and reapplies the
checkpoint. Delete is replay-safe for the recorded descriptor. Provider implementations preserve the
logical VM and its existing platform metadata even when restoration requires replacing a boot disk
or reimporting a WSL distribution. Before claiming a fresh create row, core checks the live managed
provider inventory and refuses an unrecorded artifact instead of overwriting or adopting it.

The operator-facing surface follows the existing flat second-object convention:

```console
agw vm create-checkpoint NAME
agw vm list-checkpoints [--vm NAMES] [--names-only] [--output human|json]
agw vm restore-checkpoint NAME [-y|--yes]
agw vm delete-checkpoint NAME [-y|--yes] [--force]
```

Operators do not choose checkpoint names or select among checkpoints while the one-slot rule is in
force. Create never replaces. Restore does not delete. Normal deletion releases an occupied slot
only after proving provider cleanup; explicit forced deletion can instead disown the local record
under the exceptional rules below. List and describe distinguish provider lifecycle state from
current restore eligibility. A completed provider artifact remains `ready`, while the derived
restore value is `available`, `declarations-changed`, `resume-required`, or `unavailable`.
`available` requires both a matching desired-state fingerprint and a proved live provider
descriptor; an unavailable provider boundary or disagreeing inventory cannot be presented as
restorable. The compact `vm list` gains no checkpoint column.

Ordinary checkpoint deletion reconciles interrupted creation and proves provider absence before it
releases the row. When provider access or cleanup cannot recover, explicit `--force` first attempts
that same normal path and then, only on failure, asks the operator to disown Agentworks' record. The
warning identifies the recorded provider artifact when available and explains that late, incomplete,
emergency, or additional provider artifacts may remain and continue billing. Forced disowning
atomically releases the ownership row and records a distinct audit event. Forced VM deletion uses
the same fallback only after its checkpoint cleanup attempt fails; ordinary VM deletion remains
blocked. This escape does not make provider cleanup successful and does not bypass restore
fingerprint checks.

Restoration is explicit and destructive, never an automatic upgrade rollback. Before restore, all
Agentworks sessions must be stopped and the VM must be stopped. The platform preserves a recoverable
intermediate while a destructive boot-disk swap or WSL unregister/import is incomplete. After
restore, core starts the VM only long enough to re-establish access and independently attest
`/etc/os-release`, records that observed release even when it moves backward, marks initialization
as needing reconciliation, and returns the VM to stopped state. Desired Agentworks declarations are
not rolled back. The result directs the operator to `vm reinit` before relying on guest convergence.

Before restore begins, core must prove that the current VM-owned effective desired declarations have
the same canonical fingerprint recorded at checkpoint creation. A changed VM, workspace, agent,
session, console, membership, grant, desired overlay, inherited template, transitive non-secret
resource declaration, non-secret vm-site declaration, or authorized-key identity blocks restore with
guidance to create a new checkpoint or restore the matching Agentworks database/config backup.
Release-sensitive dependency values are resolved for the checkpoint's immutable capture release, not
the VM's current observation, because a successful upgrade or restore may legitimately make the two
differ. Secret-reference identities participate, but resolved secret values do not. Runtime status,
timestamps, events, observations, and applied-state facts are excluded. There is no force bypass.
Checkpoint lifecycle and upgrade take exclusive ownership of a narrow per-VM shared/exclusive guard
across provider work and restore attestation. VM delete also takes exclusive ownership because it
composes checkpoint deletion. Agentworks entry points that could concurrently activate or mutate the
VM take shared ownership, so ordinary compatible commands retain their existing concurrency.

`vm upgrade` removes `--checkpoint` and every manual artifact prompt. A fresh transition refuses an
unrelated existing checkpoint. It checks the database slot and live provider inventory before
creating either local backup, then automatically creates its transition-owned checkpoint after both
backups succeed and immediately before the first source-release package mutation. It temporarily
stops and restarts the VM around that offline capture. A same-pair checkpoint may be reused after a
pre-mutation cancellation or an explicit restore when the live guest is still the recorded source
and no remote journal conflicts; reuse output discloses its original creation time. A resume with a
journal requires the database, remote journal, and live provider descriptor to name the same
checkpoint. A completed upgrade retains the checkpoint for explicit operator recovery, names it,
warns about continuing provider storage charges, and prints its delete command; it does not silently
trade recovery safety for a free slot.

## Acceptance

- A create caller cannot name an OS or release. The VM manager passes the core current release in
  every provision request, and every successful create on each certified platform returns a live
  guest whose `VERSION_CODENAME` matches that request (`trixie` at this cutover).
- The vm-platform descriptor and all six bundled implementations declare internal contract version
  1. An inconsistent bundled version or incomplete implementation fails registration. A conforming
     platform with no requested release mapping fails before backend mutation with a clear
     Agentworks-update or site-configuration error and never falls back.
- The create exception boundary preserves both missing-map error kinds and their remediation hints.
  A platform-observed release mismatch rolls back inside its create window. After any platform
  returns, core independently probes the returned transport; a failed core probe never reports
  success and retains a failed row with the backend identifiers needed for deletion.
- A migrated legacy row stays `null` until a live probe establishes its release, including a Proxmox
  row created from an operator template.
- Reinitializing a recognized VM selects values for its observed release regardless of support tier.
  A legacy warning precedes best-effort work, and a missing mapping fails before APT mutation.
- The shipped HashiCorp, ngrok, and tofuutils sources use reviewed release maps; release-neutral
  sources remain scalar.
- `vm upgrade` refuses every unsafe preflight listed in R5 and prints an actionable correction.
- With no incomplete journal, `vm upgrade` accepts only the edge from the penultimate registry
  profile to the final profile. Appending a certified profile changes that eligible pair without a
  CLI or schema change, and no current-2 direct or chained upgrade can start.
- An incomplete journal is inspected before new-upgrade eligibility. It remains resumable under its
  recorded direct policy after a later profile append, and it blocks creation of a second journal.
- Upgrade checkpoint viability is checked read-only before local backup artifacts are created. No
  irreversible upgrade stage begins without completed local backups and a verified managed
  checkpoint whose database row and live platform descriptor agree.
- The final post-Bookworm-update plan is recomputed and confirmed before suite switching; a changed
  removal, source, conffile, blocker, or space fact cannot inherit the preliminary confirmation.
- Target-release package simulation uses only the generated target sources and scratch package
  state, not guest APT configuration fragments, hooks, preferences, or source files. Space floors
  and estimated growth are aggregated conservatively for every distinct filesystem.
- External apt/dpkg ownership blocks the operation. Automatic APT timers return to their exact prior
  state after a Bookworm-safe abort or verified healthy Trixie completion; they remain inhibited on
  a mixed or unhealthy package state until forward repair or external restore. A failed source-safe
  timer restoration leaves all known timers stopped during reconfiguration and records a durable
  repair-required VM event.
- Killing the local CLI or dropping SSH during each package stage leaves one durable remote
  operation whose active attempt and last completed action a later invocation can resume or
  diagnose, including interruption inside an action rather than only between actions.
- One fixed remote lock guards every journal claim and write, including reboot intent and dispatch,
  and attempt-identity compare-and-set prevents stale invocations from completing, failing,
  retrying, or dispatching reboot again for a newer attempt. The fixed root and journal-owned files
  reject unsafe links, ownership, and modes.
- A successful upgrade reboots into Trixie, reconnects, records Trixie, reinitializes with Trixie
  mappings, and leaves every required Trixie suite covered with no enabled Bookworm or third-party
  source.
- A post-reboot health or reinit failure records the observed Trixie release and a repair-required
  outcome rather than reporting success or reverting the row to Bookworm.
- Migration 34 creates checkpoint persistence without modifying migration 33. An already-migrated
  version-33 database advances normally and retains its Debian observations.
- Every bundled vm-platform version 1 implementation provides create/list/restore/delete. Core
  checks fresh provider inventory before claiming a create row and detects missing, duplicate, or
  descriptor-disagreeing platform inventory, while each platform proves completion, ownership, and
  VM binding. The one-checkpoint product limit is enforced by schema and compare-and-set updates
  rather than documented only in prose. Operation identity and narrow shared/exclusive per-VM
  exclusion prevent stale or concurrent lifecycle work from crossing provider operations without
  serializing ordinary readers against each other.
- Restoring a checkpoint preserves the logical VM identity, re-attests and records the restored
  Debian release, marks initialization for reconciliation, leaves the checkpoint intact, and never
  rewrites desired declarations. It refuses before provider mutation if those declarations no longer
  match the checkpoint's canonical desired-state fingerprint. List and describe expose that mismatch
  as derived restore eligibility without relabeling the completed provider lifecycle state.
- Ordinary checkpoint and VM deletion remain blocked when provider cleanup cannot be proved.
  Explicit forced deletion first attempts normal reconciliation, then warns about provider residue
  and billing, records a distinct disowning event, and releases only Agentworks' ownership record.
- VM backup and workspace copy pass large-archive tests on a Trixie guest with `/tmp` mounted as a
  bounded tmpfs.
- The live integration matrix exercises create and delete everywhere, plus an actual Bookworm to
  Trixie upgrade on each platform where the recovery prerequisite can be established.
- The capability-model, vm-platform, and system-plugin READMEs describe the implemented internal
  version-1 release/checkpoint contract and its failure behavior in the same change as the bundled
  implementations.

## Constraints and non-goals

- Supporting arbitrary Linux distributions or arbitrary images is out of scope.
- Letting an operator choose Bookworm for a new VM is out of scope.
- Skipping Debian releases, downgrading, and direct or chained upgrade from `current-2` are out of
  scope. This effort implements and certifies only Bookworm-to-Trixie while giving the next adjacent
  release the same registry, mapping, request, and workflow shape. Synthetic successor fixtures
  prove that those seams are not pair-hardcoded; they do not claim compatibility with an
  unimplemented release or promise unchanged workflow internals. This is not an arbitrary release
  graph or unbounded generic upgrader.
- General VM cloning, arbitrary checkpoint retention policies, operator-chosen checkpoint names, and
  more than one managed checkpoint per VM are out of scope. Provider-native artifacts created
  outside Agentworks remain outside the managed inventory.
- Automatic restore is out of scope. The explicit `restore-checkpoint` command is the only
  Agentworks whole-VM restore path in this effort.
- `vm upgrade` is not an unattended fleet rollout facility. The first transition is one VM at a time
  and requires operator review of the apt plan and recovery posture.
- Agentworks does not rewrite or re-enable unmanaged third-party repositories after the upgrade.
- Historical locked SDDs and changelog entries that accurately describe Bookworm-era behavior stay
  historical.

## Settled decisions

- Core supports Debian only, derives current from the final profile in one ordered registry, and
  passes that concrete release to every platform create.
- Existing Bookworm rows are discovered live, never guessed in migration.
- The recorded value is the last verified observed release, not desired configuration.
- Version-specific values are release maps owned at the layer that understands their platform or
  vendor semantics.
- `vm upgrade` is the primary migration path; fresh VM plus data copy is the documented fallback.
- The first upgrade command creates one managed offline checkpoint through vm-platform contract
  version 1 and retains it for explicit restore or deletion.
- Support tiers derive from current, current-1, and current-2 position rather than dates.
- Platforms never infer current. An inconsistent bundled capability declaration fails conformance; a
  missing requested mapping fails clearly before backend mutation.

## Open questions

None. Tested point releases and provider selectors are implementation evidence, not product-policy
clocks or operator choices.
