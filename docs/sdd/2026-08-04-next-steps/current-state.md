# Current State

- Snapshot date: 2026-09-06, post-0.18.0 and post-wave-4-charter (update at wave boundaries)
- Baseline: released Agentworks 0.14.0 (2026-08-18, live on PyPI; see `phasing.md`'s release map for
  the cut's trail) plus post-release `main`. The release itself carries everything the previous
  baseline enumerated (the phase 1 TOML sunset, the 0.14 expired-compat removals, declarative-schema
  phase 2 through the descriptor, the guide through its grammar-native shape, wave 3 secret sources,
  the operational JSON output contract, the assistance flow with the README bootstrap) plus the CLI
  grammar rewrite and the resource-show child. The 0.14.0 field-evidence fixes (PRs #604 through
  #607) shipped as patch release 0.14.1 (2026-08-19, live on PyPI; the `Release-As` override
  reframed release-please's minor bump, PR #617)
- **0.15.0 released 2026-08-25** (PR #630 merged, tag `v0.15.0`), carrying the secret-preview
  contract rewrite, the doctor indeterminate split, and the 0.15 upgrade guide. Its changelog
  carries a hand-applied operator callout pointing at `docs/guides/upgrading-to-0.15.md`, which
  release-please destroys whenever it rewrites the release branch. **Correction, 2026-08-26:** an
  earlier version of this entry said the branch is rewritten only by a `feat`, `fix`, or breaking
  commit under `cli/`, and that `docs` merges leave the callout intact. That was wrong, and the saga
  lead retracted it publicly on PR #630. It rested on a false observation, that PR #647 did not
  render; its single `docs(cli)` commit renders in the published 0.15.0 Release body, and in fact
  renders twice, once via its merge commit and once via the commit itself. An entry appears only
  when it passes three independent filters, none dominant. **Window:** its committer date sorts
  after the previous release commit in the default branch's date-ordered history, which is what
  omitted twelve reachable commits from the 0.16.0 notes. **That omission is permanent and is not
  repairable by scheduling** (established 2026-08-26 when a lane reasoned the opposite): both inputs
  are already fixed, the boundary by the `v0.15.0` tag and the commits by their committer dates, so
  holding the cut does not recover them and 0.17.0 is strictly worse because its window opens at the
  `v0.16.0` tag. They are absent from every future generated surface. The hold on 0.16.0 rests on
  release completeness rather than on recovering them. **Component:** it touches `cli/`, which is
  why `fix(website)` never appears despite being a rendering type. **Type:** `feat`, `fix`, and
  `docs` render; `chore`, `test`, and `refactor` do not. So a `docs` merge under `cli/` does rewrite
  the branch. The durable fix is to carry operator-facing text in a commit footer, which is
  generated content, written as a single unbroken paragraph because the trailer parser stops at a
  continuation line beginning `word:` and fragments on blank lines (this truncated the 0.14.0 entry,
  issue #589). The published GitHub Release body is editable and is not regenerated once the release
  exists, so it is the fallback for correcting notes after a cut. **The cut proved one thing the
  four-merge study could not:** the hand-applied callout reached `cli/CHANGELOG.md` on `main` but
  **not the published GitHub Release body**, because release-please builds that body from its own
  generated notes rather than from the file. Protecting the file protects the wrong artifact if what
  matters is the page operators land on, which makes the commit-footer approach the fix for both
  surfaces rather than merely the more durable one. Two entries are duplicated in the 0.15.0 notes
  because we merge with merge commits whose GitHub-default body repeats the PR title while
  release-please parses merge bodies expecting a squash workflow; there is no config switch for this
  (the schema's only `merge` key concerns combining release PRs), so the remedy is either
  squash-merging, which would destroy the always-green phased commits inside one PR that this repo
  deliberately uses, or correcting the published Release body
- **Three releases shipped between 2026-08-28 and 2026-09-05**, and the 0.16.0 hold recorded in
  `phasing.md` is discharged. **0.16.0 (2026-08-28)** carried the instance-spec overlays and the
  typed instance state store, the per-session harness workload inputs that gated the cut (issue
  #674), and the Azure SDK nested-model migrations. **0.17.0 (2026-08-31)** carried runtime Git
  credential identities with their provider-owned structured `source` break, model-directed merge
  strategies, and the harness-integration contract-version-2 removal of `merge_config`. **0.18.0
  (2026-09-05)** carried the session and console lifecycle rework, standardized runnable status
  inspection, the Debian Trixie release transition, the AWS indeterminate-outcome reconciliation,
  and the SSH pty and stdin corrections that live Windows operation found. The three compatibility
  surfaces it shipped (the `session resume` forms, `console attach --recreate`, and
  `session list --no-status`, promised for removal in 0.19 by `docs/guides/upgrading-to-0.18.md`
  lines 29, 43, and 64) **were removed on 2026-09-06 by PR #752**, closing issue #720, and the open
  0.19.0 release PR #748 already carries the break. The promise is kept in the release that names it
- **The published 0.18.0 release body is known to be inaccurate** (issue #741): it advertises eleven
  managed-checkpoint and VZ-recovery entries for work the Debian effort withdrew on 2026-09-01, and
  six subjects render twice. A published body is never regenerated, so only a manual edit corrects
  it. This is the first case where the merge-commit duplication described above compounded with a
  mid-window design withdrawal

This document records where the system actually is, verified by code reconnaissance rather than
assumed from the perspectives. It is the ground truth the phasing rests on; when a wave lands,
update the affected section and the snapshot date, in place (git history is the append-only record).
The immutable origin snapshot is `starting-state.md`; the journey is the diff from there to here to
`target-state.md`.

## Declarative schema

Both phases are on `main`, and the `2026-07-31-declarative-schema` SDD is locked: phase 1 landed via
PR #316, phase 2 via PR #414 (2026-08-07). Every schema fact is authored once in a registration-time
Pydantic model: validation, reference extraction (`agentworks/schema/`, a total two-walker split
with shared iterative traversal in `agentworks/traversal.py`), JSON Schema emission with `x-agw-ref`
markers, live samples, and `describe-kind` all derive from the models. The error bridge is the
single framing choke point. `agw resource migrate` was deleted before release per the
remediation-posture ruling; the operator path is precise hard errors plus
`docs/guides/upgrading-to-0.14.md`. Settings that name resources (`defaults.site`,
`[secret_config].backends`) are shape-checked at load and resolved once at the composition boundary
as hard errors. The config deprecation channel is kept deliberately as the warn-window carrier.
`capabilities/facets.py` was removed pending its wave 4 consumer; the `config_for(facet)` contract
stays settled in the docs and this saga's contracts.

The vm-platform mode contract landed post-lock (PR #444, 2026-08-08, recorded on that SDD's
lockfile): azure and aws carry an `auth` union (`ambient` or their credential arm), lima carries
`placement` (`local` or `ssh`), each union defaulting to the mode omission historically selected,
with extraction reading declared defaults as if written so an omitted union produces the same graph
edges as the written spelling. Written old shapes hard-error with the exact rewrite; manifests that
never wrote the retired blocks cross without edits. The variant-modeling rule (one arm per
required-field shape; the discriminator tracks shape, not concept) lives permanently in
`cli/agentworks/capabilities/README.md`.

The variant rework landed the same day (PR #455, 2026-08-08): git-credential token acquisition is a
defaulted one-arm discriminated union (provider contract v2, `token: null` retired with the exact
rewrite), env entries are a selector-free structural union whose legacy null-companion spellings
canonicalize at one shared selector consumed by validation, extraction, and fill, github `repos` and
`owner` combine as a scope union, and install commands accept multiple test predicates with all-pass
semantics (zero declared tests always runs). The three-tier rule and its companion tests live
permanently in `cli/agentworks/capabilities/README.md`, backed by the structural-union and
scalar-shorthand machinery in `agentworks/schema/`.

## Guide and onboarding

The guide first slice is on `main` (onboarding phase 1, PR #428, 2026-08-08): the `agw guide`
command core with package-owned topic contributions, `concept-onboarding`, safe anchored projection
(`build_guide_view` materializes global inventories only for concept roots the traversal plan
permits; denied data is never constructed), verification surfaces with typed evidence, and the
`guide-contributions` always-on rule requiring topic updates to ride the changes that make them
true.

The operational JSON output contract landed via PR #462 (2026-08-10) after an operator scope
correction removed the doctor database-snapshot subsystem: all 16 covered commands emit one
deterministic JSON document from the same domain fact record the human renderer consumes (message
and hint carry identical text in both formats, so JSON inherits the human transcript's trust posture
— documented in `cli/command-reference.md`), errors ride the ordinary stderr route, and doctor is
non-migrating by authorized behavior: a scalar schema gate plus `Database(read_only=True)` behind a
12-line local context manager, failing closed on malformed schema state. Guide actions consume
doctor JSON directly. The assistance phase shipped with PR #480 (2026-08-13), including the
generated README bootstrap block, which pins version 0.14.0 or newer and resolves against the
released 0.14.0. Remaining onboarding phases (wave 2 adoption, closeout) proceed per that effort's
per-phase PR plan.

## Deprecation removal targets

Cleared by wave 1 (PR #406, 2026-08-05): every in-scope expired surface is removed, including the
session restart vocabulary, the legacy harness selectors, the older configuration aliases, the
legacy VM console module, and the dead Python surfaces. Wave 2 finished the job: the generic
capability discriminator compatibility is a hard error, the config deprecation channel currently
carries nothing and is kept deliberately as the warn-window carrier (operator ruling, 2026-08-07),
and the manifest surface has no warn-window channel (the standing consequence recorded in
`target-state.md`).

## Capability framework

- The switchboard is gone (wave 2, PR #414): one frozen, core-owned `CapabilityKindDescriptor` per
  kind in a single table is the only capability-kind enumeration, with the seven former
  hand-enumerated sites (adapter, graph kind set and readiness dispatch, registry loaders, bootstrap
  publication, snapshot/restore, decode sections) derived from it and a guard test asserting
  derivation. Registration-time conformance (contract, metadata, constructibility, operations,
  config-model contract, `contract_version`) replaced the type-and-cast seam, with atomic seating
  preserved.
- Each capability implementation registers exactly one config model; validation is one blob at a
  time against the tagged union assembled at the registration boundary, cached on its arms. The
  secret-backend constructed-singleton exception is removed (wave 3, PR #453, merged and locked
  2026-08-10). `_VMPlatformKind` moved into `capabilities/` with its siblings.
- Wave 3 shipped the two-level secret model: synthesized sources over backends, the resolution API,
  map-keyed hosting recorded in the descriptor's `mapping_host` field with schema emission consuming
  it, and the 0.14 hard break for direct backend references. The readiness-shape choice for the
  `secret-source` kind is settled and recorded in that SDD's lock.

## Harness integration surface (wave 4 groundwork)

- The capability exists at the session scope only. `HarnessIntegration`
  (`cli/agentworks/capabilities/harness_integration/base.py:202`) declares one abstract operation,
  `start` (`:308`), and the kind descriptor's `required_operations` is `frozenset({"start"})`
  (`cli/agentworks/capabilities/harness_integration/kinds.py:119`). There are no vm, user, or
  workspace methods.
- **The facet vocabulary is not missing; it is declared and unused.** `Capability.config_for`
  (`cli/agentworks/capabilities/base.py:339`) is a shipped classmethod whose docstring defines a
  facet as the level a capability is driven at, states that facets are not scopes, records that core
  owns the mapping, and says the signature takes no facet argument only because no capability offers
  more than one config yet. `cli/agentworks/capabilities/README.md` carries the same contract. The
  per-kind config contract lives on the descriptor (`kinds.py:127`,
  `cli/agentworks/capabilities/descriptor.py:165`); a capability declares its own model through
  `config_model` and offers it through `config_for`.
- Resume is not a second method. `HarnessLaunchIntent` (`base.py:71`) carries `CREATE`,
  `RESUME_ONLY`, `RESUME_OR_NEW`, and `FORCE_NEW` with a `starts_fresh` property, so one `start`
  serves both paths. The kind's `contract_version` is 3 (`kinds.py:117`), matching all four in-tree
  integrations.
- The harness leak into core is still present and is wave 4's acceptance test: `claude_marketplaces`
  and `claude_plugins` sit on the agent template (`cli/agentworks/agents/templates.py:42`) and on
  admin config (`cli/agentworks/vms/admin.py:142`), with an `install_claude_plugins` VM-init step
  (`cli/agentworks/vms/initializer/driver.py:741`, defined at `:764`). The Claude plugin's own
  module docstring (`cli/agentworks/plugins/claude/__init__.py:28`) already names them as core
  surfaces that should not be Claude-specific.
- The instance-state store is in place for applied state: `cli/agentworks/db/instance_state.py`
  exposes `replace_applied_slices` (`:529`), `clear_applied_slice` (`:575`), and
  `inspect_owner_state` (`:593`), with `AppliedStateKey` (`:37`) closed at two vm-only keys and
  `_APPLIED_KEYS_BY_KIND` (`:62`) already carrying `agent`, `workspace`, and `session` as kinds that
  own no keys yet. Wave 4 registers keys without touching the table, which is what the R2 store
  review promised.

## Session runtime (observability groundwork)

- Sessions have no run/incarnation identity. `sessions.name` is the sole key and is reusable after
  delete-and-recreate. `boot_id` is no longer only a reboot detector: `SessionRow` now carries
  `pid`, `boot_id`, and `tmux_server_start_ticks` together as the tmux server's process fingerprint,
  used for teardown identity by the session and console lifecycle work. That is process identity,
  not workload identity, and it does not close the gap below. Any transcript keyed by session name
  alone will splice unrelated histories. This is the single sharpest schema gap for the
  observability effort.
- There is no PTY observation, no input interception, no event or fanout infrastructure, and no
  supervisor or heartbeat. tmux owns the PTY (one tmux server per session on a private socket);
  Agentworks only ever pulls scrollback via `capture-pane`.
- The one existing push-style precedent is the Codex `notify` recorder
  (`plugins/codex/recorder.py`): the harness invokes an Agentworks-provisioned script with a
  structured JSON payload per turn, which today extracts a single thread id and discards the rest.
  This is the embryo of the "harness reports events" channel.
- The Claude integration only probes for its transcript file's existence to decide
  resume-versus-launch; nothing reads transcript content yet.

## Open SDD ledger (pre-saga efforts)

Cleared by wave 1 (PR #406): all five pre-saga SDDs are locked (`2026-08-03-harness-integration`,
`2026-08-04-session-resume`, `2026-03-29-proxmox-provider`, `2026-05-03-session-enhancements`, and
`2026-03-26-mise-integration` with its plan reconciled against evidence). The
`2026-07-29-harness-transcripts` draft is harvested into `inputs/harness-transcripts-harvest.md` and
its branch is deleted. Remaining unmerged drafts on remote branches, both out of saga scope:
`2026-07-29-herdr-integration` (spike-gated) and `2026-07-19-named-console-template-selector`
(ready, standalone).

## Environment notes

- Copilot's automated PR review is currently failing on monthly quota exhaustion (observed
  2026-08-05), so per the development process the fresh-eyes generic pass is substituted with a
  local reviewer until quota resets.

- **No CI runner covers Windows or macOS**; every gate runs on Linux. PR #747 is the first move
  against this: it takes the suite from 558 failures to zero on a Windows host and adds the
  `windows-latest` CI job, so the gap closes structurally rather than by one-off effort. It is no
  longer purely test-portability: two product changes rode along, a samples guard and a
  confirm-helper stdout fix. The Windows-only `vm create` break that PR #677 fixed is the case in
  point: no gate could have caught it, and it reached a published release. The exposure is
  structural rather than incidental: any platform-conditional path is unverified until an operator
  hits it, and the mechanism there (`subprocess.run(..., text=True)` wrapping stdin in a
  `TextIOWrapper` that rewrites LF to `os.linesep`) was invisible on Linux by construction. Recorded
  as a known gap, not a scheduled item.
