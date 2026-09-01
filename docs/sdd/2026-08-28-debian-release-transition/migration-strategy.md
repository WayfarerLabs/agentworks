# Migration Strategy: Debian Release Transition

- Status: Draft
- Date: 2026-08-28
- Applies to: database state, VM creation, Proxmox configuration, APT resources, and existing VMs

## 1. Transition shape

The release containing this work makes Trixie the sole release for new VM creation. It does not
rewrite an existing guest and does not infer that an old row is Bookworm. Existing VMs keep their
platform identity and ordinary lifecycle behavior.

The cutover has five independent parts:

1. migrate durable release observation fields;
2. retire the unshipped checkpoint schema through a later migration;
3. make every create request carry and verify Trixie;
4. convert release-specific platform and APT values to maps; and
5. give operators an explicit way to adopt a release changed outside Agentworks.

No deployment stage offers both Bookworm and Trixie creation. There is no public release selector
and no compatibility period in which platforms choose their own meaning of current.

## 2. Database migration

### Migration ladder

Migration 33 adds nullable `debian_release` and `debian_release_observed_at` columns. The pair-null
check prevents a release without observation provenance or a timestamp without a release. Existing
rows are left null.

Migration 34 created `vm_checkpoints` during development. It is immutable because development
installations, including the authenticated operator's installation, have already applied it.

Migration 35 retires that table safely:

- an absent table is already in the desired state;
- an empty table is dropped; and
- a nonempty table aborts migration without changing schema version or deleting rows.

The refusal lists affected VM names and directs the operator to use the previous build to delete the
managed provider artifacts before retrying. This is a one-time development migration, not a shipping
checkpoint lifecycle.

### Required operator preparation for schema version 34

Before installing the corrected build, an operator on version 34 should run the old build's
`vm list-checkpoints` and delete every managed checkpoint it reports. Provider billing or retained
recovery artifacts may otherwise outlive the database record.

If the corrected build encounters a remaining row, startup migration stops safely. The operator must
reinstall or invoke the exact prior build, remove the checkpoint through its managed delete command,
and then retry the corrected build. Direct SQL deletion is not recommended because it would disown
the provider artifact.

Fresh databases still apply the forward ladder through 33, 34, and 35. Their newly created
checkpoint table is empty and is removed in the same migration transaction sequence.

### Observation population

No migration backfills release data. Rows gain an observation only when:

- a new create is independently verified by core;
- an ordinary release-sensitive operation proves an unknown or matching live release; or
- the operator explicitly runs `vm confirm-release`.

The database stores the observed codename rather than current/previous/legacy. Those labels are
derived at read time from the running release registry.

An older Agentworks binary that rejects the new database version is not a rollback mechanism.
Operators restore a pre-migration database backup if they must revert the application. That does not
revert a guest distribution upgrade.

## 3. Creation cutover

Core chooses Trixie from the final ordered release profile and passes the concrete value in
`ProvisionRequest.debian_release`. All bundled version-1 vm-platform implementations change in the
same release.

Each code-owned platform map must have a Trixie artifact for every architecture it exposes. Missing
code-owned mappings are implementation defects and fail without fallback. Proxmox is operator-owned,
so a create preflight requires `template_vmids.trixie` before secret resolution or backend access.

Platforms validate the image inside their rollback window where possible. Core always performs the
final `/etc/os-release` probe through the returned native transport. A mismatch records neither the
requested release nor a successful provisioning result. Backend identity remains addressable when
core discovers a mismatch after platform success.

There is no existing-VM rewrite. A Bookworm VM continues pointing at the same backend object and can
be started, stopped, accessed, backed up, reinitialized where valid, and deleted.

## 4. Proxmox configuration

The old scalar:

```yaml
template_vmid: 9000
```

is read only as a legacy Bookworm mapping. Current creation requires:

```yaml
template_vmids:
  trixie: 9013
```

Operators may retain a Bookworm entry for local clarity or old-tool compatibility, but it does not
enable Bookworm creation in the new build:

```yaml
template_vmids:
  bookworm: 9012
  trixie: 9013
```

Before upgrade, the operator builds a Debian 13 template using the supported setup procedure and
adds `template_vmids.trixie` to every Proxmox vm-site that should create VMs. Sites without that key
remain loadable for best-effort operations on existing VMs; only create fails early.

Core's final live attestation prevents an incorrectly labeled template from being accepted. The
configuration has no bypass for that check.

## 5. APT resource transition

Release-neutral sources keep scalar `source`. A source whose correctness changes by Debian release
moves to an explicit map:

```yaml
sources:
  bookworm: deb [signed-by=...] https://vendor.example/debian bookworm main
  trixie: deb [signed-by=...] https://vendor.example/debian trixie main
```

The migration is declarative, not a database rewrite. Built-in resources ship with reviewed map
values. Operator-authored resources using a codename-bearing scalar fail validation with guidance to
provide `sources`.

Before installing this release, operators should add entries for every recognized VM release on
which they expect the resource to converge. A missing selected entry fails before key or source
writes. Vendor suite names are copied from vendor policy and are not generated by substituting the
host codename.

## 6. Existing VM support

After the cutover:

- Trixie is current and used for creation;
- Bookworm is previous and remains ordinarily operable; and
- a recognized release older than Bookworm would be legacy, warn on access, and continue best
  effort.

Appending a future Forky profile changes Trixie to previous and Bookworm to legacy. It does not
rewrite rows or require another support-state migration.

Agentworks supports the operator workflow from previous to current. It does not support chained
current-2 upgrades. For a legacy guest, create a current VM and copy the needed data.

## 7. Operator-led distribution upgrade

Agentworks does not execute package or source changes. The safe migration sequence is:

1. update Agentworks to a build that recognizes the target release;
2. stop Agentworks sessions and workloads that must not be interrupted;
3. create and verify a provider-native backup or recovery artifact;
4. ensure an out-of-band recovery path appropriate to the platform;
5. follow Debian's release-specific upgrade notes completely;
6. verify the guest is healthy and reachable;
7. run `agw vm confirm-release NAME` and consent to the recorded change; and
8. run `agw vm reinit NAME` to converge release-aware Agentworks state.

Step 7 atomically records the observed target and sets initialization pending. Step 8 is separate.
If it fails, the database still reports the truthful live release and pending initialization; repair
the cause and rerun `vm reinit`.

An operator-led provider restore uses the same adoption sequence. After the provider restores a
recognized older guest, run `vm confirm-release` to record the backward observation and then
`vm reinit`. Agentworks does not restore its live related objects from a provider disk image, so the
operator must reconcile application data and declarations separately.

## 8. Trixie staging migration

Size-unbounded backup and workspace-copy archives move from guest `/tmp` to private paths under
`/var/tmp`. This is an implementation cutover with no persistent data migration. Temporary files are
removed after success or failure.

Operators should ensure `/var/tmp` has enough disk space for the largest expected archive. Existing
small bounded uses of `/tmp` remain subject to ordinary temporary-file controls.

## 9. Doctor transition

Remove every Debian release, live observation, upgrade residue, and per-VM connectivity addition
made to doctor by the superseded design. No compatibility shim remains.

Doctor must retain its previous local runtime behavior: it does not activate VMs or wait for an
unavailable WSL distribution. Operators request live evidence through the named VM commands.

## 10. Delivery and rollback

The implementation lands as one atomic product cutover:

1. release registry and observation schema;
2. version-1 platform request and all provider maps;
3. release-aware APT resources;
4. `vm confirm-release` and recorded projections;
5. migration 35 checkpoint-schema retirement;
6. doctor and checkpoint/upgrade surface removal; and
7. permanent documentation and generated collateral.

Before marking the PR ready, unit, static, generated, capability-conformance, and documentation
gates run on the exact head. Live certification covers Trixie create and ordinary lifecycle on
available platforms. It does not certify an Agentworks upgrade command that no longer exists.

Application rollback restores an application/database backup from before the schema transition and
the matching binary. Guest rollback remains a provider-native operator operation. After any guest
restore, the operator explicitly confirms the observed release and reinitializes.

## 11. Removal proof

Repository checks must prove that no shipping surface retains:

- `vm upgrade`;
- checkpoint CLI commands or completion entries;
- checkpoint database models, repositories, or capability methods;
- provider checkpoint implementations or snapshot permissions added only for them;
- the remote upgrade journal or preflight engine;
- experimental upgrade-enable configuration; or
- live Debian doctor probes.

Historical migration 34 and completed SDD plan checkboxes remain as immutable evidence of what was
developed and then superseded. Migration 35 and the SDD ruling explain why those names still appear
in history without defining a shipping product surface.
