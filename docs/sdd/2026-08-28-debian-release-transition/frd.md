# FRD: Debian Release Transition

- Status: Draft
- Date: 2026-08-28
- Scope: Trixie creation, Bookworm compatibility, and Bookworm-to-Trixie VM upgrades

## Rulings this SDD rests on

- **Debian remains the one supported guest operating system.** Agentworks does not add a general
  operating-system selector, a custom image field, or a best-effort bring-your-own distribution
  mode. This preserves the consistency decision in ADR 0002 while superseding its fixed Bookworm
  release.
- **New VMs move forward.** Once this effort ships, every new Agentworks VM targets Debian 13
  Trixie. Agentworks does not offer a Bookworm creation switch.
- **Existing Bookworm VMs get a transition, not an eviction.** Current VMs remain operable while
  operators migrate them. The supported in-place path is `agw vm upgrade NAME`.
- **The guest release is durable state.** Agentworks records the last release it verified on each
  VM. It does not hide that cross-platform fact in platform metadata or infer it forever from the
  current product default.
- **Release-varying values are mappings.** Platform image selectors, third-party APT stanzas, and
  other release-specific facts are keyed by Debian release. A literal branch or value that merely
  happens to work for one release is not the model.
- **Platform configuration may supply a platform artifact, but not a core release choice.** This is
  necessary for Proxmox, where the operator owns the template catalog. The core still selects
  Trixie; the Proxmox site maps that release to a template VMID.

## Why now

Debian 13 Trixie became stable on 2025-08-09. Bookworm's regular support ended on 2026-07-11 and its
LTS period ends on 2028-06-30. Agentworks still creates Bookworm VMs and hardcodes Bookworm in five
platform implementations and several shipped APT sources. Continuing that posture creates new legacy
machines while delaying the migration work that already exists.

The change is broader than replacing image strings. Existing rows do not record their guest release,
Proxmox templates were never release-validated, the initializer cannot select a release-specific APT
value, and the current VM backup is not a bootable rollback point. Trixie also makes `/tmp` a
memory-backed filesystem by default, while current VM backup and workspace-copy paths stage
unbounded archives there.

## Requirements

### R1: One closed Debian release model

Agentworks has a typed, closed Debian release vocabulary whose initial values are `bookworm` and
`trixie`. Trixie is the single current creation release. The value is internal product policy, not
an operator-selectable VM field.

The following surfaces remain absent:

- no `vm create --release`, `--os`, or `--image` option;
- no VM-template release field;
- no core or platform setting that asks which Debian release to create;
- no arbitrary AMI, Azure image, GCP image, Lima image, or WSL rootfs override; and
- no support claim for Ubuntu, Fedora, or another distribution.

### R2: Trixie-only creation on every supported platform

Every successful `vm create` provisions Trixie. The common provision request carries the core's
resolved release, and each platform selects its own artifact from a release-keyed mapping:

| Platform | Trixie source                                                        |
| -------- | -------------------------------------------------------------------- |
| Lima     | Debian cloud `debian-13-generic-{amd64,arm64}.qcow2`                 |
| WSL2     | Docker Official Image tag `debian:trixie`                            |
| AWS EC2  | Debian public SSM release 13 parameter for the selected architecture |
| Azure VM | `Debian:debian-13:13-gen2:latest`                                    |
| GCP GCE  | `debian-cloud` families `debian-13` and `debian-13-arm64`            |
| Proxmox  | the site's `trixie` template VMID                                    |

A platform must verify `/etc/os-release` during its rollback-capable create window and return the
observed release. A mismatch, missing release, non-Debian guest, unavailable official image, or
missing Proxmox Trixie template fails creation before Agentworks reports success. New creation never
falls back to Bookworm or an operator-supplied generic image.

The feature does not claim a platform until its real create, initialize, reboot, reconnect, backup,
and delete path has passed the Trixie certification matrix for every architecture Agentworks exposes
on that platform.

### R3: Persist the last verified guest release

Each VM row stores the last Debian codename Agentworks verified from the live guest, plus when it
was observed. This is a cross-platform lifecycle fact and is not stored in opaque platform metadata,
desired instance spec, or a declaration file.

New VMs record Trixie only after create-time verification. Existing rows migrate to `unknown`; the
database migration does not assume they are Bookworm. A release-sensitive live operation probes
`/etc/os-release`, records a supported result, and refuses on a non-Debian or unsupported release.
This is particularly important for Proxmox and for VMs an operator may already have upgraded by
hand.

`vm list`, `vm describe`, and JSON v1 expose `bookworm`, `trixie`, or `null`. `null` means no live
observation has established the fact. It is not rendered as Bookworm and is not treated as drift.
Where a live probe disagrees with the last verified row, the release-sensitive operation stops and
reports both values before changing the guest or database. `vm upgrade` owns the one reconciliation
path: it can validate and adopt an already-Trixie guest after explicit operator confirmation rather
than strand a VM that was upgraded outside Agentworks.

### R4: Every release-specific value is selected from a release map

Platform image records are maps from the closed Debian release value to the full platform selector,
including associated facts such as Azure's minimum OS disk size. Architecture remains a subordinate
axis where required. Platform code does not scatter codename conditionals.

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
`bookworm` and `trixie` in active implementation code and account for each occurrence as either a
release model definition, a mapping key/value, a parser fixture, or documentation. Provider selector
contract tests account for numeric release values without treating unrelated `12` and `13` literals
as findings.

### R5: A safe, resumable `vm upgrade` workflow

`agw vm upgrade NAME` upgrades one verified Bookworm VM to the current Trixie release. It has no
target-release option and is not an alias or flag on `vm reinit`.

Before changing Debian sources, the command must:

1. activate and verify the named VM and resolve every credential needed for reconnect or Tailscale
   rejoin;
2. observe Bookworm and reconcile that observation with the database;
3. refuse active, broken, or unverifiable Agentworks sessions;
4. verify package database health, package holds, architecture, kernel metapackage, APT pins,
   third-party and backports sources, modified package conffiles, minimum `/boot` space, and
   sufficient package-cache/root-disk space;
5. enforce the Bookworm OpenSSH minimum needed for a reconnectable remote upgrade and run the
   release-specific blocker checks captured from Debian's Trixie release notes;
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

The first confirmation authorizes bringing Bookworm current, not switching Debian suites. After that
update, the command reopens the operation boundary and recomputes every package, source, conffile,
blocker, and space fact. It shows the final plan and requires a second confirmation before switching
suites; any material drift from the preliminary plan is explicit. It then follows Debian's supported
direct Bookworm-to-Trixie procedure:

- bring Bookworm fully current before changing suites;
- disable non-Debian APT sources and preserve their original files;
- write canonical codename-pinned Trixie Debian sources;
- run the scripted `apt-get` equivalent of the documented minimal upgrade and full upgrade;
- ensure the Trixie kernel is installed;
- predict interface names with the installed Trixie udev rules and block reboot when an unsafe
  rename still needs operator pinning;
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

### R6: Bookworm transition support has an end

The release that first ships `vm upgrade` publishes an exact Bookworm compatibility end date six
months after that release, capped no later than 2028-06-30. Through that compatibility date,
existing Bookworm VMs retain the current lifecycle and Phase B initialization behavior, selected
from Bookworm mappings. No release in the transition creates a new Bookworm VM.

The supported Bookworm-to-Trixie upgrade path remains available through 2028-06-30. After the
six-month compatibility date, backup, inspect, shell/exec recovery, delete, and `vm upgrade` remain
the guaranteed paths; new Bookworm-targeted feature work and ordinary Bookworm reinitialization do
not. The CLI and docs warn when a Bookworm VM is past the compatibility date without making its data
inaccessible.

A shared operation-policy gate, separate from release observation, owns this cutoff for every
release-sensitive mutation. The exact compatibility date is dormant until the Trixie cutover release
sets it. Boundary-date tests prove behavior immediately before, on, and after both support
deadlines.

After 2028-06-30, Agentworks continues to identify and display Bookworm, but does not claim that a
newly released Agentworks version can safely automate its upgrade. It points operators to the last
supported Agentworks release and the data-recovery path.

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
- the Bookworm support dates;
- preflight blockers and external checkpoint responsibility;
- the exact `vm upgrade` stages and resumability behavior;
- how to recover or continue from every durable failure stage;
- which third-party sources remain disabled and how to review them; and
- how the fresh-VM/workspace-copy fallback differs from an in-place upgrade.

ADR 0002 is superseded by a new ADR that retains Debian as the one guest OS while moving the fixed
release through a controlled, recorded product lifecycle.

## Acceptance

- A create request cannot name an OS or release, and every successful create on each certified
  platform returns a live guest whose `VERSION_CODENAME` is `trixie`.
- No supported platform silently falls back to Bookworm when its Trixie artifact is missing.
- A migrated legacy row stays `null` until a live probe establishes its release, including a Proxmox
  row created from an operator template.
- Reinitializing a verified Bookworm VM during the compatibility window selects Bookworm values;
  reinitializing Trixie selects Trixie values. A missing mapping fails before APT mutation.
- The shipped HashiCorp, ngrok, and tofuutils sources use reviewed release maps; release-neutral
  sources remain scalar.
- `vm upgrade` refuses every unsafe preflight listed in R5 and prints an actionable correction.
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

## Constraints and non-goals

- Supporting arbitrary Linux distributions or arbitrary images is out of scope.
- Letting an operator choose Bookworm for a new VM is out of scope.
- Skipping Debian releases, downgrading Trixie to Bookworm, or upgrading beyond Trixie is out of
  scope. The architecture must make the next adjacent release a new mapping and transition policy,
  not an unbounded generic upgrader.
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

- Core supports Debian only and selects Trixie for new creation.
- Existing Bookworm rows are discovered live, never guessed in migration.
- The recorded value is the last verified observed release, not desired configuration.
- Version-specific values are release maps owned at the layer that understands their platform or
  vendor semantics.
- `vm upgrade` is the primary migration path; fresh VM plus data copy is the documented fallback.
- The first upgrade command requires a separately created external recovery checkpoint and does not
  add a provider snapshot abstraction.
- Bookworm full compatibility is time-bounded; the upgrade path lasts through Bookworm LTS.

## Open questions

None. Exact calendar dates and tested point releases are filled in by the implementation release
from the policies above, not reopened as product decisions.
