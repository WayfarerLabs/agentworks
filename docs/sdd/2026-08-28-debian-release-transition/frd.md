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
platform consumes the request value and selects its own artifact from a release-keyed mapping:

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
platform, release, and need to update Agentworks or the providing plugin. If an operator-owned map,
such as a Proxmox site's template map, lacks the key, the error instead names the site setting that
must be supplied. Neither case falls back to another release.

The required request and result fields change the vm-platform capability contract. Implementations
written against the previous contract are rejected by exact contract-version conformance with a
clear incompatibility error. A contract-current platform must verify `/etc/os-release` during its
rollback-capable create window and return the observed release. A mismatch, missing release,
non-Debian guest, unavailable official image, or missing Proxmox Trixie template fails creation
before Agentworks reports success. New creation never falls back to Bookworm or an operator-supplied
generic image.

The manager preserves the distinct code-owned and operator-owned missing-map errors, including their
remediation hints, across its provisioning wrapper. A compliant platform rolls back a live release
mismatch before it raises. As a defensive contract check, if a nonconforming platform instead
returns a release that differs from the request, the manager persists its backend identifiers and
retains one failed, uninitialized VM row so `vm delete` can still address the backend; it does not
report success or discard the only cleanup handle.

The feature does not claim a platform until its real create, initialize, reboot, reconnect, backup,
and delete path has passed the Trixie certification matrix for every architecture Agentworks exposes
on that platform. The implementation updates the capability-model README, vm-platform README, and
plugin-author README so capability and platform authors can predict this request, mapping, failure,
verification, and contract-version behavior.

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
drift. Adding a future recognized release does not require a database schema migration. Where a live
probe disagrees with the last verified row, the release-sensitive operation stops and reports both
values before changing the guest or database. `vm upgrade` owns the one reconciliation path: it can
validate and adopt an already-current guest after explicit operator confirmation rather than strand
a VM that was upgraded outside Agentworks. Trixie is the first current target.

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
3. refuse active, broken, or unverifiable Agentworks sessions;
4. verify package database health, package holds, architecture, kernel metapackage on platforms
   where the guest owns its kernel, APT pins, third-party and backports sources, modified package
   conffiles, minimum `/boot` space, and sufficient package-cache/root-disk space;
5. enforce the source release's OpenSSH minimum needed for a reconnectable remote upgrade and run
   the pair-specific blocker checks captured from the target release's Debian release notes;
6. show the preliminary packages apt plans to remove and every source Agentworks will disable or
   replace;
7. complete the ordinary Agentworks metadata/workspace backup and a local Debian recovery bundle
   containing `/etc`, dpkg and apt state, package selections, and the pre-upgrade source files; and
8. require an operator-attested external recovery checkpoint reference that identifies the actual
   recovery artifact, after showing platform-specific checkpoint and console guidance.

The Agentworks backups are data-recovery artifacts, not a bootable VM checkpoint. The CLI says so
before confirmation and in its result. The external reference can name a provider snapshot, WSL
export, Proxmox backup, or equivalent artifact. Agentworks records the reference with the upgrade
event but does not claim it created, validated, owns, or can restore that external artifact.

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
succeeded. It never starts a second package manager, and it owns a bounded inhibit/restore lifecycle
for Debian's automatic APT timers while also respecting apt/dpkg's native locks.

Agentworks does not attempt an automatic distribution downgrade or provider rollback. Before the
guest proves Trixie, the database retains its last verified release. Once a reconnecting probe
proves Trixie, the row records Trixie even if a later initializer or health check needs attention,
so the database does not call a Trixie guest Bookworm. The command and VM events distinguish
`complete` from `Trixie observed, repair required`.

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
- preflight blockers and external checkpoint responsibility;
- the exact `vm upgrade` stages and resumability behavior;
- how to recover or continue from every durable failure stage;
- which third-party sources remain disabled and how to review them; and
- how the fresh-VM/workspace-copy fallback differs from an in-place upgrade.

The permanent capability-model README explains that a consuming domain may pass a required,
domain-owned value through an operation request. The vm-platform README specifies the concrete
Debian release field, platform-local mapping, exact contract-version cutover, missing-release error,
live verification, and tests required of every built-in or plugin platform. The plugin-author README
moves its example and create-contract teaching from version 2 to version 3 in the same merge unit.
These updates ship with the contract implementation, not ahead of behavior.

ADR 0002 is superseded by a new ADR that retains Debian as the one guest OS while moving the fixed
release through a controlled, recorded product lifecycle.

## Acceptance

- A create caller cannot name an OS or release. The VM manager passes the core current release in
  every provision request, and every successful create on each certified platform returns a live
  guest whose `VERSION_CODENAME` matches that request (`trixie` at this cutover).
- A previous-contract platform fails registration. A contract-current platform with no requested
  release mapping fails before backend mutation with a clear platform-update or site-configuration
  error and never falls back.
- The create exception boundary preserves both missing-map error kinds and their remediation hints.
  A platform-returned release mismatch never reports success or orphans a backend: compliant
  implementations roll back, while the manager safety net retains a failed row with the backend
  identifiers needed for deletion.
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
- No irreversible upgrade stage begins without completed local backups and an external recovery
  reference whose limitations are explicit.
- The final post-Bookworm-update plan is recomputed and confirmed before suite switching; a changed
  removal, source, conffile, blocker, or space fact cannot inherit the preliminary confirmation.
- External apt/dpkg ownership blocks the operation. Automatic APT timers return to their exact prior
  state after a Bookworm-safe abort or verified healthy Trixie completion; they remain inhibited on
  a mixed or unhealthy package state until forward repair or external restore.
- Killing the local CLI or dropping SSH during each package stage leaves one durable remote
  operation whose active attempt and last completed action a later invocation can resume or
  diagnose, including interruption inside an action rather than only between actions.
- One fixed remote lock guards every journal claim and write, including reboot intent and dispatch,
  so two invocations cannot own the same transition.
- A successful upgrade reboots into Trixie, reconnects, records Trixie, reinitializes with Trixie
  mappings, and leaves no enabled Bookworm Debian source.
- A post-reboot health or reinit failure records the observed Trixie release and a repair-required
  outcome rather than reporting success or reverting the row to Bookworm.
- VM backup and workspace copy pass large-archive tests on a Trixie guest with `/tmp` mounted as a
  bounded tmpfs.
- The live integration matrix exercises create and delete everywhere, plus an actual Bookworm to
  Trixie upgrade on each platform where the recovery prerequisite can be established.
- The capability-model, vm-platform, and plugin-author READMEs describe the implemented
  release-carrying contract and its failure behavior in the same change that bumps the capability
  contract.

## Constraints and non-goals

- Supporting arbitrary Linux distributions or arbitrary images is out of scope.
- Letting an operator choose Bookworm for a new VM is out of scope.
- Skipping Debian releases, downgrading, and direct or chained upgrade from `current-2` are out of
  scope. This effort implements and certifies only Bookworm-to-Trixie while giving the next adjacent
  release the same registry, mapping, request, and workflow shape. Synthetic successor fixtures
  prove that those seams are not pair-hardcoded; they do not claim compatibility with an
  unimplemented release or promise unchanged workflow internals. This is not an arbitrary release
  graph or unbounded generic upgrader.
- Automatic provider snapshot creation, snapshot deletion, or rollback is out of scope for this
  first transition. Agentworks does not own resources it cannot clean up consistently.
- A general VM restore/clone command and transparent whole-VM rebuild migration are out of scope.
  The fallback uses documented provider recovery or a new Trixie VM plus existing data-copy tools.
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
- The first upgrade command requires a separately created external recovery checkpoint and does not
  add a provider snapshot abstraction.
- Support tiers derive from current, current-1, and current-2 position rather than dates.
- Platforms never infer current. An old capability contract fails conformance; a missing requested
  mapping fails clearly before backend mutation.

## Open questions

None. Tested point releases and provider selectors are implementation evidence, not product-policy
clocks or operator choices.
