# Migration Strategy: Debian Release Transition

- Status: Draft
- Date: 2026-08-28
- Governing artifacts: `frd.md` and `hla.md`

## 1. Transition shape

This is a forward product cutover with two different populations:

- **New VMs:** Trixie only, immediately when the implementation release ships.
- **Existing VMs:** last verified release is discovered live; Bookworm is `current-1` after the
  cutover and can move through `vm upgrade`.

There is no period in which the operator selects Bookworm or Trixie for creation. There is no
database rewrite pretending every old VM is Bookworm. There is no automatic whole-VM rollback.
Support derives only from position in one ordered profile registry whose final entry is current:
current is fully supported, current-1 is supported and upgradable, and current-2 or older is best
effort with warnings and no newly started upgrade. Debian lifecycle dates do not change those tiers.

## 2. Database transition

### Schema migration

The state migration adds nullable `debian_release` and `debian_release_observed_at` columns with the
pair-null constraint described in the HLA. It does not enumerate codenames in SQL, so a future
release promotion changes the code registry rather than the schema. It updates the safer-migrations
exact version-shape map and all migration fixtures in the same commit.

The migration does not inspect live guests, network, platform configuration, or historical events.
Every existing row receives `(NULL, NULL)`. A new database creates the columns through the ordinary
migration ladder.

### Population rules

- New create writes the release that core independently observes over the platform's returned
  transport before Phase A starts.
- A release-sensitive operation probes an unknown existing row and records a recognized observation.
- `vm list` and database open never probe the network.
- A recorded/live mismatch blocks ordinary release-sensitive mutation.
- `vm upgrade` is the explicit adoption path for a healthy guest manually upgraded from current-1 to
  current.

The observation timestamp makes the value's meaning explicit: it is the last verified live fact, not
a continuously synchronized claim.

### Older Agentworks binaries

The safer-migrations future-schema refusal remains the downgrade guard. An older binary whose schema
support ends before the new migration refuses the newer database rather than ignoring the columns.
An operator who must downgrade Agentworks restores the pre-migration database backup according to
the existing database restore runbook before running the older binary.

Restoring the database does not downgrade a VM that has already reached Trixie. After any database
restore, the operator must use a release that understands Trixie and re-observe affected guests
before a release-sensitive mutation.

## 3. Creation cutover

All platform selector maps, the vm-platform version 3 request contract, and create-time live
validation land before Trixie's certified profile is appended to the active registry. The commits
remain independently green by using explicit candidate-profile test fixtures until the final cutover
commit performs that one append. Before the append, a live Trixie guest is an unrecognized release
and release-sensitive mutation refuses with guidance to use a supporting Agentworks build; there is
no separate ahead-of-current tier.

The VM manager supplies the concrete current release in every `ProvisionRequest`; platforms do not
infer it. Exact capability conformance rejects an installed version 2 platform before use. A version
3 platform resolves the request through its own map before backend mutation. A missing code-owned
key reports that the platform or plugin is out of date; a missing operator-owned Proxmox key names
the exact vm-site setting. Neither failure chooses a different release.

The manager's provisioning wrapper preserves those two typed errors and their remediation hints. A
platform's live mismatch must raise while the platform can still roll back. After any platform
returns success, core first retains the platform metadata and then independently probes the returned
transport. A failed core probe leaves one failed, uninitialized row with delete/retry guidance. This
keeps the backend addressable for cleanup instead of unwinding its only database handle.

The release is not publishable until each platform's artifact lookup and create rollback behavior is
certified. At runtime, a missing image or architecture produces a typed platform or configuration
error. It never falls back to Bookworm. The cutover deletes Bookworm platform image selectors; real
upgrade certification uses prebuilt Bookworm fixtures rather than a hidden create route.

The contract change updates `cli/agentworks/capabilities/README.md`,
`cli/agentworks/capabilities/vm_platform/README.md`, and `cli/agentworks/plugins/README.md` in the
same implementation merge unit. The base guide explains the domain-owned request value and hard
contract-version cutover. The specific guide documents release-map lookup, missing-key errors,
platform rollback verification, core-owned final attestation, and the tests required of built-in and
plugin platforms. The plugin guide changes its example and create-contract teaching from version 2
to version 3. Topic prose and docstrings move with them.

Existing VMs retain the platform metadata recorded at their original creation. Image mappings are
used only to create a new backend VM; changing a map does not mutate or relabel an existing one.

## 4. Proxmox configuration migration

Proxmox must move the release-specific template input to a release map:

```yaml
# old
template_vmid: 9000

# new
template_vmids:
  trixie: 9001
```

The old scalar remains loadable as a Bookworm-only value under ordinary configuration compatibility.
It supports resolving the site for existing VM operations but does not satisfy a Trixie create. A
new create on that site fails with a focused message that names `template_vmids.trixie` and the
setup guide.

For a configured Trixie mapping, creation treats the VMID as an operator assertion until the cloned
guest proves it. Proxmox starts the clone, reads `/etc/os-release` through the QEMU guest agent, and
rolls the clone back before Agentworks bootstrap when the observation is missing, non-Debian, or not
Trixie. The ordinary post-bootstrap probe remains the final success attestation. There is no bypass
for either check.

The CLI does not automatically rewrite operator YAML. The guide instructs the operator to:

1. update `scripts/proxmox-setup.sh` from the implementation release;
2. create and verify a Trixie cloud-init template under a new VMID;
3. add that VMID under `template_vmids.trixie`;
4. keep the old Bookworm template until existing recovery/checkpoint needs end; and
5. remove `template_vmid` after every Agentworks installation that reads the config is upgraded.

The compatibility parser warns that the scalar is historical and cannot satisfy current creation.
There is no Debian-date removal promise and no alias that silently reinterprets the old VMID as
Trixie. Any future removal follows the repository's configuration-compatibility policy rather than
the guest release support classifier.

## 5. APT resource schema transition

The existing scalar `source` field remains valid for release-neutral repositories. The new `sources`
map is additive, mutually exclusive with `source`, and selected from the VM's verified release.

Shipped resources migrate in place:

- HashiCorp becomes a Bookworm/Trixie map.
- tofuutils/tenv becomes a Bookworm/Trixie map.
- ngrok becomes an explicit map whose currently reviewed values both use the vendor-supported
  `bookworm` suite.
- GitHub CLI, NodeSource, mise, and Google Cloud CLI remain scalar while vendor guidance remains
  release-neutral.

Operator manifests with a genuinely release-neutral scalar keep working. A scalar containing a
registered Debian codename becomes invalid and reports the equivalent `sources` shape. This is an
intentional validation cutover: silently treating a Bookworm stanza as Trixie-compatible is the bug
the new model prevents. The scalar remains an operator assertion of release independence, not a
claim that Agentworks can prove every vendor URL or suite is portable.

The permanent resource guide and generated sample are updated in the same commit. Historical SDDs
and changelog entries keep their old examples.

## 6. Existing VM support as current advances

After the Trixie cutover, an observed Bookworm VM is `current-1` and retains:

- start, stop, delete, shell, exec, backup, and inspection;
- workspace, agent, and session lifecycle behavior supported by the implementation release;
- Phase B `vm reinit` using Bookworm APT mappings; and
- `vm upgrade` to Trixie.

No Bookworm VM is recreated or upgraded automatically. Doctor and describe show its previous-release
status and recommend `vm upgrade`.

When a later promotion makes Bookworm `current-2`, ordinary commands still attempt best-effort
operation and emit one legacy warning before access. Release age alone does not block start, stop,
inspect, shell, exec, backup, delete, reinit, or another lifecycle action. Concrete platform,
package, and missing-map failures remain honest failures; best effort is not a compatibility claim.

The current Agentworks `vm upgrade` refuses to start a Bookworm upgrade once it is `current-2`. It
does not chain Bookworm-to-Trixie and Trixie-to-current. An incomplete Bookworm-to-Trixie journal
that Agentworks created earlier remains resumable or diagnosable through that one direct policy;
this recovery exception cannot create a second journal or begin another edge. Guidance for a legacy
VM with no journal points to a new current VM and data copy. The VM row remains readable and no
calendar event rewrites or invalidates it.

## 7. In-place upgrade transition

### Before irreversible work

After activation provides guest access, `vm upgrade` first scans for an incomplete Agentworks
upgrade journal. Exactly one journal resumes or diagnoses its validated adjacent pair before current
eligibility is considered; multiple journals fail with repair guidance. Only without a journal does
the command prove that the observed release is current-1 and select the final target profile's
upgrade-from-previous policy. It performs the HLA preflight, updates no Debian suites, and shows a
preliminary plan. It then creates the Agentworks backup and Debian recovery bundle, records the
operator's external checkpoint reference identifying the actual artifact, receives explicit
attestation that the artifact exists and console or rescue access was tested, and separately
receives confirmation to bring the source release current within its existing suite.

After that update it closes and reopens the VM operation boundary, reruns the complete preflight and
simulation, shows every material difference, and receives a second mutation confirmation before
switching sources. A preliminary mutation confirmation never authorizes a changed removal, source,
conffile, blocker, or space plan.

If the command exits before confirmation, no transition state needs cleanup. A failed or incomplete
local backup is not accepted as a gate.

### During package transition

The durable guest journal stores last-completed progress separately from the active attempt and its
outcome under `/var/lib/agentworks/debian-upgrades/{source}-to-{target}`. The validated directory is
the only stored source/target identity. `plan.json` keeps the computed upgrade plan, and
`state.json` keeps progress, without another pair field in either. Intent is durable before each
mutation. The original APT source files and final plan are preserved both remotely and in the local
recovery bundle. Re-running the command takes the same journal lock used by the package service,
then inspects the systemd unit, native package locks, active attempt, postcondition, and logs before
it performs any action or writes journal state. A source-safe abort or verified healthy target
completion restores the recorded automatic APT timer state. After source-switch intent, a mixed or
unhealthy state keeps the timers inhibited until forward repair or external restore.

There is no source-level rollback once target packages may have installed. Restoring source-release
files onto a partially upgraded package set is specifically forbidden. The supported choices are:

- repair and resume forward using the stage/log guidance; or
- restore the operator's external VM checkpoint.

### After reboot

The database updates to the target as soon as a live probe proves it, even if Tailscale repair,
health verification, or Phase B later fails. Existing init state and a `repair-required` event own
the remaining outcome; remote progress does not duplicate Trixie observation or Phase B completion.

Selected Agentworks APT sources are recreated from target mappings. Unmanaged sources remain
disabled and are listed for manual review. Old source backups and upgrade logs remain until the
operator completes the documented cleanup after validating the VM and external checkpoint policy.

## 8. Fresh-VM fallback

An operator who cannot satisfy in-place preflight, or who prefers a clean replacement, uses a new
Trixie VM and data copy:

1. run `agw vm backup OLD` and create the provider-native checkpoint for the old VM;
2. create `NEW`, which is necessarily Trixie;
3. recreate desired VM/workspace/agent/session declarations from the exported metadata and current
   Agentworks configuration;
4. use supported workspace copy or repository remotes to move workspace data;
5. validate credentials, sessions, and external integrations on `NEW`; and
6. retain then delete `OLD` under the operator's ordinary recovery policy.

This is documented as a reconstruction, not transparent VM restore. Current `workspace copy` creates
a new logical workspace, and current `vm backup` has no restore command. This SDD does not hide
those facts or add a second migration verb.

## 9. Trixie `/tmp` migration

Before Trixie creation is enabled, size-unbounded staging moves from guest `/tmp` to secure
disk-backed paths:

- VM backup workspace archive and path staging;
- workspace copy destination archive; and
- any upgrade package/state artifacts.

Existing small, bounded `/tmp` uses are inventoried and can remain. No configuration knob is added
to turn off Trixie's tmpfs default. Tests prove large data transfer with `/tmp` too small to contain
the payload.

## 10. Delivery and rollback order

The implementation lands in independently reviewable phases:

1. ordered core release type, relative support classifier, database observation, release-keyed Phase
   B values, output, and no-selector contract as one merge unit;
2. disk-backed staging corrections;
3. vm-platform version 3 request contract, platform image maps, Proxmox transition, capability
   READMEs, and Trixie create validation;
4. the adjacent durable `vm upgrade` workflow and permanent recovery teaching;
5. live platform certification, relative support teaching, superseding ADR, and release cutover.

Before phase 5, builds still create the prior final registry profile on the integration branch.
Phase 3's shared provider contract and all six implementations land through one green merge unit;
provider branches are stacked review inputs, not independently mergeable changes. The final cutover
atomically appends the certified Trixie profile and deletes Bookworm platform selectors only after
all gates pass. Current then derives from the new tail; no second setting changes.

Rolling back code before any VM upgrade uses the existing database-restore procedure. Rolling back
an already upgraded guest requires the operator's external checkpoint; changing the Agentworks
constant, database row, or APT sources is not a guest rollback.

## 11. Residual and removal policy

Release promotion changes support position, not data readability. When a release becomes
`current-2`, current documentation stops claiming supported convergence or upgrade for it and starts
teaching best-effort access plus fresh-VM/data-copy recovery. The implementation keeps:

- release recognition and display for persisted VMs;
- operational release mappings that still enable honest best-effort work;
- the legacy Proxmox parser while ordinary configuration compatibility requires it; and
- backup, access, delete, and reconstruction guidance.

Create-only selectors for a non-current release can be deleted because core never requests them. An
old target profile's adjacent policy is no longer eligible to start an upgrade once its source is
not current-1, but its policy and journal reader remain available to recover an incomplete journal
that predates promotion. Retaining that direct recovery data must not make a new or multi-hop path
callable. Removal of old recognition or operational data requires a separately authorized
compatibility decision, not a date embedded in this effort.
