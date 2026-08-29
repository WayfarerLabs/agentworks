# Migration Strategy: Debian Release Transition

- Status: Draft
- Date: 2026-08-28
- Governing artifacts: `frd.md` and `hla.md`

## 1. Transition shape

This is a forward product cutover with two different populations:

- **New VMs:** Trixie only, immediately when the implementation release ships.
- **Existing VMs:** last verified release is discovered live; Bookworm remains compatible for a
  bounded period and can move through `vm upgrade`.

There is no period in which the operator selects Bookworm or Trixie for creation. There is no
database rewrite pretending every old VM is Bookworm. There is no automatic whole-VM rollback.

The implementation release publishes the two calendar values that change future behavior:

- `BOOKWORM_FULL_COMPATIBILITY_ENDS`: six months after the release, no later than 2028-06-30; and
- `BOOKWORM_UPGRADE_SUPPORT_ENDS`: 2028-06-30.

They appear in the release profile, CLI diagnostics, release notes, and permanent upgrade guide.

## 2. Database transition

### Schema migration

The state migration adds nullable `debian_release` and `debian_release_observed_at` columns with the
closed constraint described in the HLA. It updates the safer-migrations exact version-shape map and
all migration fixtures in the same commit.

The migration does not inspect live guests, network, platform configuration, or historical events.
Every existing row receives `(NULL, NULL)`. A new database creates the columns through the ordinary
migration ladder.

### Population rules

- New create writes Trixie after the platform verifies the guest and before Phase B starts.
- A release-sensitive operation probes an unknown existing row and records a supported observation.
- `vm list` and database open never probe the network.
- A recorded/live mismatch blocks ordinary release-sensitive mutation.
- `vm upgrade` is the explicit adoption path for a healthy externally upgraded Trixie guest.

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

All platform selector maps and create-time live validation land before the core current-release
constant switches to Trixie. The commits remain independently green by using explicit test fixtures
until the final cutover commit wires the constant.

The release is not publishable until each platform's artifact lookup and create rollback behavior is
certified. At runtime, a missing image or architecture produces a typed platform readiness or create
error. It never falls back to Bookworm. The cutover deletes Bookworm platform image selectors; real
upgrade certification uses prebuilt Bookworm fixtures rather than a hidden create route.

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

Through 2028-06-30, the old scalar remains loadable as a Bookworm-only value. It supports resolving
the site for existing VM operations but does not satisfy a Trixie create. A new create on that site
fails with a focused message that names `template_vmids.trixie` and the setup guide.

The CLI does not automatically rewrite operator YAML. The guide instructs the operator to:

1. update `scripts/proxmox-setup.sh` from the implementation release;
2. create and verify a Trixie cloud-init template under a new VMID;
3. add that VMID under `template_vmids.trixie`;
4. keep the old Bookworm template until existing recovery/checkpoint needs end; and
5. remove `template_vmid` after every Agentworks installation that reads the config is upgraded.

The compatibility parser warns with the 2028-06-30 removal date. Keeping it through upgrade support
avoids a second config migration that could strand recovery commands on unchanged sites. There is no
alias that silently reinterprets the old VMID as Trixie.

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
supported Debian codename becomes invalid and reports the equivalent `sources` shape. This is an
intentional validation cutover: silently treating a Bookworm stanza as Trixie-compatible is the bug
the new model prevents. The scalar remains an operator assertion of release independence, not a
claim that Agentworks can prove every vendor URL or suite is portable.

The permanent resource guide and generated sample are updated in the same commit. Historical SDDs
and changelog entries keep their old examples.

## 6. Existing Bookworm VM behavior

During the full compatibility window, an observed Bookworm VM retains:

- start, stop, delete, shell, exec, backup, and inspection;
- workspace, agent, and session lifecycle behavior supported by the implementation release;
- Phase B `vm reinit` using Bookworm APT mappings; and
- `vm upgrade` to Trixie.

No Bookworm VM is recreated or upgraded automatically. Doctor and describe show the support date and
recommend `vm upgrade`.

After full compatibility ends, the guaranteed surface narrows to inspection, recovery access,
backup, delete, and `vm upgrade`. Commands that would converge new guest configuration refuse with a
support-window error rather than applying newly developed Trixie assumptions to Bookworm. The record
and existing data remain accessible.

The shared operation-policy gate owns that decision for every release-sensitive mutation. Its exact
compatibility date and enforcement remain dormant until the final Trixie cutover commit sets the
date, keeping earlier implementation units truthful.

`vm upgrade` remains supported through Bookworm LTS end. After that date, current releases identify
Bookworm and point to the last supporting Agentworks release; they do not claim a freshly tested
upgrade.

## 7. In-place upgrade transition

### Before irreversible work

`vm upgrade` performs the HLA preflight, updates no Debian suites, and shows a preliminary plan. It
then creates the Agentworks backup and Debian recovery bundle, records the operator's external
checkpoint reference identifying the actual artifact, and receives confirmation to bring Bookworm
current.

After that update it closes and reopens the VM operation boundary, reruns the complete preflight and
simulation, shows every material difference, and receives a second confirmation before switching
sources. A preliminary confirmation never authorizes a changed removal, source, conffile, blocker,
or space plan.

If the command exits before confirmation, no transition state needs cleanup. A failed or incomplete
local backup is not accepted as a gate.

### During package transition

The durable guest journal records last-completed progress separately from the active attempt and its
outcome under `/var/lib/agentworks`. Intent is durable before each mutation. The original APT source
files and final plan are preserved both remotely and in the local recovery bundle. Re-running the
command takes the same journal lock used by the package service, then inspects the systemd unit,
native package locks, active attempt, postcondition, and logs before it performs any action or
writes journal state. A Bookworm-safe abort or verified healthy Trixie completion restores the
recorded automatic APT timer state. After source-switch intent, a mixed or unhealthy state keeps the
timers inhibited until forward repair or external restore.

There is no source-level rollback once Trixie packages may have installed. Restoring Bookworm source
files onto a partially Trixie package set is specifically forbidden. The supported choices are:

- repair and resume forward using the stage/log guidance; or
- restore the operator's external VM checkpoint.

### After reboot

The database updates to Trixie as soon as a live probe proves Trixie, even if Tailscale repair,
health verification, or Phase B later fails. Existing init state and a `repair-required` event own
the remaining outcome; remote progress does not duplicate Trixie observation or Phase B completion.

Selected Agentworks APT sources are recreated from Trixie mappings. Unmanaged sources remain
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

1. core release type, database observation, release-keyed Phase B values, output, and no-selector
   contract as one merge unit;
2. disk-backed staging corrections;
3. platform image maps, Proxmox transition, and Trixie create validation;
4. the durable `vm upgrade` workflow and permanent recovery teaching;
5. live platform certification, exact support dates, superseding ADR, and release cutover.

Before phase 5, builds still create the prior current release on the integration branch. Phase 3's
shared provider contract and all six implementations land through one green merge unit; provider
branches are stacked review inputs, not independently mergeable changes. The final cutover switches
the one core constant and deletes Bookworm platform selectors only after all gates pass.

Rolling back code before any VM upgrade uses the existing database-restore procedure. Rolling back
an already upgraded guest requires the operator's external checkpoint; changing the Agentworks
constant, database row, or APT sources is not a guest rollback.

## 11. Residual and removal policy

At full Bookworm compatibility end, remove:

- Bookworm Phase B compatibility tests not required by the still-supported upgrade preflight; and
- current docs that teach ordinary Bookworm convergence.

Keep through 2028-06-30:

- Bookworm detection and display;
- the Bookworm release profile and source values required by the upgrade;
- the Bookworm-to-Trixie upgrade policy; and
- the legacy Proxmox `template_vmid` parser needed to keep old sites loadable for recovery; and
- backup/recovery/delete guidance.

The cutover commit creates a dated cleanup issue owned by the release maintainer, linked from the
superseding ADR and support-policy code. It tracks the six-month documentation/Phase B cleanup, the
2028 Proxmox adapter and upgrade-policy cleanup, and the last supporting Agentworks release pointer.

After upgrade support ends, Bookworm remains a recognized historical/observed value. Removing its
enum value or database constraint would make existing state unreadable and is not part of this
transition.
