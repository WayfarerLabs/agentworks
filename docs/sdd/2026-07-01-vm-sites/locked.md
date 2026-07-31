# VM sites and platforms: lockfile

## 2026-07-18: partially superseded by the orchestration layer

The orchestration-layer effort (ADR 0019,
`docs/adrs/0019-orchestration-layer-command-plans-over-node-graphs.md`) retired the secret-handling
model this SDD's FRD, HLA, and plan narrate: capability construction no longer takes a resolver and
registers nothing at construct time; commands compose as plans over derived node graphs; the secret
union derives from the walked plan and resolves in one boundary pass per composition root; instances
receive values only through the context's scoped delivery (`ctx.secret(name)`). Read those artifacts
as history of the Phase 7 capability adoption, not as the current contract. The living contract is
`cli/agentworks/capabilities/README.md`; this SDD's own `capability-model.md` already redirects
there. Everything else in this SDD (sites, platforms, the schema, the CLI surface, the slug) remains
accurate.

## 2026-07-16

The vm-sites SDD is complete and locked as of this date. All seven plan phases are done (every
checkbox in `plan.md` is checked; none outstanding), the work shipped, and the permanent homes for
its load-bearing concepts exist at HEAD. Further changes go through a new SDD or a follow-up PR that
updates this lockfile with a dated entry.

The work landed primarily as **PR #169, `feat(vms)!: vm sites and platforms`** (merged 2026-07-13),
a breaking change: the `vms` table was rebuilt (schema v27), dropping the legacy per-platform
columns in favor of a `site` reference plus an opaque `platform_metadata` blob, and the legacy
`[azure]` / `[proxmox]` config sections became deprecated `vm-site` declarations. Phase 7
(capability-model adoption, added 2026-07-12) rode the same branch. The `!`-flagged breaks and their
remediations were recorded in the PR description; release-please derives the changelog from the `!`
commits.

### What shipped

- **Phase 1 (kinds, protocol, registry, dispatch)**: the `VMPlatform` capability, the read-only
  `vm-platform` capability kind, the declarable `vm-site` kind ("a configured place to create VMs"),
  and `VM_PLATFORM_REGISTRY`. All invocation goes through site resolution (`agentworks.vms.sites`).
- **Phase 2 (DB migration)**: the destructive `vms`-table rebuild to schema v27, with per-version
  checkpointing and a `foreign_key_check` so a multi-version jump is retry-safe. Legacy platform
  columns backfill into `platform_metadata`; the remote-Lima snippet prints during migration.
- **Phase 3 (manager rewiring)**: `create_vm` / `reinit` / `rekey` / `delete` and the gate-using
  paths dispatch through the bound platform; `delete_agent` / `delete_workspace` thread the caller's
  bound platform for ephemeral rollback.
- **Phase 4 (slug, prompts, SSH config, hostname, identity env)**: the one-time system-slug prompt
  at first `vm create` (blank answer is final), the R11 `{slug}-{name}` hostname, and the identity
  env plumbing.
- **Phase 5 (CLI surface and completions)**: `--site` on `vm create` (with the infer / prompt
  fallback), `vm shell --platform` (legacy `--provisioner` alias), removal of the `--vm-host` /
  `vm-host` group, and the doctor VM-sites group.
- **Phase 6 (tests, docs, release notes)**: no regressions; docs promoted (see Permanent homes);
  permanent artifacts carry no SDD-path references per the 2026-07-12 maintainer direction.
- **Phase 7 (capability-model adoption)**: the `capabilities/` subtree (the instance-scoped
  `Capability` base plus the construct / preflight / one-resolve-pass contract), the `Resolver`
  boundary (predict without prompting, resolve once at the preflight boundary, strict cached `get`),
  the `cls(site_name, platform_config, resolver)` constructor flip, per-platform `preflight`
  implementations capped at the structural ceiling (tools present, mappings predicted,
  unauthenticated reachability; credential probes and authenticated reads are the op's job), and the
  service-layer reorder (bind, preflight all participating resources, one union resolve pass, then
  ops), with prompt-once holding across a mixed-site batch.

### Permanent homes (the SDD-not-permanent promotions)

Per the SDD lifecycle, nothing under this directory should be load-bearing for day-to-day work. The
concepts the codebase relies on live here:

- **ADR 0016** (`docs/adrs/0016-yaml-resource-manifests.md`): the two-layer config/resource model,
  the capability-vs-declarable split, and the capability naming rule. It carries the implementation
  note that the vm-platform / vm-site pair sketched there has shipped.
- **`cli/agentworks/capabilities/README.md`**: the capability-model contract (construct / preflight
  / one-resolve-pass, the idempotency marker, the preflight structural ceiling), promoted from this
  SDD's `capability-model.md`.
- **`docs/guides/resources.md`**: the operator-facing story, including the "VM sites and platforms"
  section (manifest shape, reserved built-ins `lima-local` / `wsl2`, config secrets, migrate
  pointer).
- **`cli/README.md`**: the vm-site story, `--site` / slug / shell `--platform`, and both new kinds
  in the settings-vs-resources inventory.

### Not delivered (deliberately)

These were scoped out in the FRD; each has a clean seam so it needs no rework here:

- **The AWS platform.** This SDD prepares the interface; AWS arrives as follow-on work, likely a
  plugin platform. The capability-config secret machinery means AWS-credentials-by-secret needs no
  new design.
- **Plugin-registered VM platforms and the tiering move-out.** All four platforms stay in-tree at
  their current tier; `VM_PLATFORM_REGISTRY`, the `vm-platform` kind, and the bundled-site mechanism
  are the paved road the plugin SDD extends (`azure` / `proxmox` to plugins).
- **Schema-registration for capability config.** Uses the shipped invoked-validation API as-is; the
  declarative-schema upgrade is future work.
- **Hibernate** and **auto-suspend enforcement.** Hibernated-vs-stopped is expected to decompose
  onto observed state plus `operator_stopped` (no schema change anticipated); when and how to
  suspend an idle VM is a separate feature. The R7 gate already makes auto-resume correct.
- **VM adoption / import CLI** and a **slug rename command.** R5 makes adoption structurally
  possible; the slug is immutable-in-practice by R4/R5/R10/R11, so no rename command ships (document
  manual steps if ever needed).
- **Multiple full installs on one workstation** and **cross-install visibility**
  (`agw vm list --other-systems`). The DB-path separation that would let two installs coexist is a
  prerequisite for the slug to fully deliver there, and is not changed here.
- **Session lifecycle intent** and **continuous reconciliation.** The
  operator-intent-vs-observed-state pattern applies to sessions in a separate SDD; the gate fires
  only when an op needs the VM.

### Recorded deferrals and follow-ups

Carried from the plan's "carry into locked.md at merge" note:

- **Prompt-once is per-boundary, not per-command, at the consoles / restart entrypoints.** Those
  entrypoints prompt once per resolution boundary rather than once per command; the load-bearing
  invariant (no prompt fires before a participating resource's preflight passes) holds everywhere.
- **`agent create` folded its two prompt sessions later, not here.** As of this SDD, `agent create`
  still ran a bind-time site-secrets pass plus a `_collect_git_tokens` pass. Folding them was slated
  to ride the git-credentials capability adoption (#167), which has since landed (the capability
  subtree move and `refactor/capability-secret-contract`, PR #182); any remaining fold belongs to
  that track, not this SDD.
- **Tombstoning-time SDD-reference sweep.** Pre-existing `docs/sdd/` citations in permanent code
  from other efforts (noted in `hla.md`) are left as-is; they get swept when those SDDs tombstone,
  not here.

### Review history

Every phase went through agentworks-reviewer rounds (recorded inline in the plan's sequencing
notes), plus a final whole-branch round. Phase 7 ran three rounds and ended in an explicit approve
(two non-gating minors closed in the final commit). The idempotency marker stays test-enforced (a
semantic property; the behavioral guard suite is the enforcement).

The FRD, HLA, plan, capability-model note, and LLDs are accurate as-built as of this date. They are
now locked.

## Addendum: 2026-07-26 (PR #271)

`vm_active` is now a pure power-hold on every platform and no longer performs a reconnect wait, so
the HLA's WSL2 justification for `config` on the hold path (building the Tailscale transport for
that wait) no longer holds. `config` remains on `vm_active`'s signature as reserved operator
settings (the base-class contract), but no platform's hold uses it today. The Azure
`native_transport()` public-IP rationale for `config` is unchanged. See
`capabilities/vm_platform/wsl2.py::_keepalive`.

## Addendum: 2026-07-27 (issue #278, name-length caps)

Two name-length statements in the FRD are corrected as-built (the FRD is a point-in-time record and
is not edited in place; this addendum is authoritative):

- **Site names** no longer follow the VM-name rules and are NOT capped at 30. Site names hit no
  OS-level identifier limit: they are a registry key and display/config surface only, never derived
  into a hostname or SSH host alias (the FRD's "they appear in hostnames and SSH host aliases" was
  incorrect; VM names, not site names, feed `{slug}-{vm}` hostnames). They now use the freeform cap,
  `MAX_FREEFORM_NAME_LENGTH` (64).
- **The hostname bound** in the FRD's Tailscale section ("slug max 20 plus dash plus name max 30 is
  51 characters") is updated: the VM-name cap is now `MAX_VM_NAME_LENGTH` (38). The VM name feeds
  two composed sinks and the cap is the MIN over both: the `{slug}-{vm}` hostname / DNS label
  (`63 - 1 - 20 = 42`) and the `{slug}-{vm}-vnet` Azure virtual-network name
  (`64 - 20 - 1 - 5 = 38`). The vnet sink binds (its `-vnet` suffix costs 5 more characters than the
  bare hostname), so the cap is 38, NOT the 63-char hostname and NOT Azure's 64-char computer-name.
  See the resource-manifests lockfile's 2026-07-27 entry for the full per-kind cap rationale.

## Addendum: 2026-07-30 (issue #199, Azure service-principal credentials)

Two statements in this SDD's artifacts are superseded as-built; the artifacts are point-in-time
records and are not edited in place, so this addendum is authoritative.

- **"Azure, Lima, and WSL2 declare no config secrets today" (`hla.md`) is now only true of Lima and
  WSL2.** An `azure-vm` site may declare an optional `platform_config.service_principal` table whose
  `secret` field names the secret holding the client secret (default `azure-client-secret`), and
  `AzureVMPlatform.dependencies` emits the corresponding `ConfigReference` when it is present. A
  site that omits the table still declares nothing and authenticates ambiently, so the sentence
  holds for every pre-existing site. The HLA's follow-on, "AWS later rides the same rails for client
  secrets", is now literally true: the Azure block is the documented reference shape for it (see the
  "Credentials on a cloud platform" section of `cli/agentworks/capabilities/vm_platform/README.md`).
- **The `VMPlatform` signature block in `frd.md`, and the matching one in `hla.md`, no longer match
  the ABC.** Beyond the `ctx: RunContext` the orchestration layer added to the ops (already covered
  generically by the 2026-07-18 entry above), the three transport hooks now take it too:
  `native_transport(vm, ctx, *, config=None)`, `transient_route(vm, ctx)`, and
  `post_tailscale_ready(vm, ctx)`. Opening a route to a cloud VM is a backend call, and without a
  context a credentialed platform had no delivery surface on the one path that reaches it first: a
  cold `vm shell --platform` on a running VM, where the activation gate's happy path is a pure
  Tailscale reachability probe and the transport build is the command's first platform call.
  `agentworks.transports.native_transport` gained a matching keyword-only `ctx`, and
  `gated_vm_boundary` now yields `(vm_node, resolver, ops_ctx)` so gated commands have the same
  op-start context the no-gate ones already got. `vm_active` deliberately did NOT change: every hold
  that exists is local and makes no backend call.
- **`VMSiteNode.preflight` no longer predicts its declared secrets' resolvability.** The plan's
  "Proxmox's preflight is the base's token prediction only" and the FRD's readiness narration
  describe a placement that has now moved twice: out of the capability instance (the orchestration
  layer, already covered by the 2026-07-18 entry above) and now out of the holding node too, into
  the operation's preflight sweep. The site node keeps reference INTACTNESS (its declared names must
  reach real registry rows) and composes its platform's world checks; whether those secrets would
  RESOLVE belongs to the operation, and doctor, which invokes the node's preflight per row without a
  sweep, therefore reports a prompt-only site credential as a healthy site plus one Secrets-group
  row. See the 2026-07-31 entry in `docs/sdd/2026-07-16-orchestration-layer/locked.md` for the full
  note; that SDD owns the prediction placement.

The living contract remains `base.py` plus `cli/agentworks/capabilities/vm_platform/README.md`.
