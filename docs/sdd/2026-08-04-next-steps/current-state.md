# Current State

- Snapshot date: 2026-09-14, post-agent-artifacts-merge (PR #794, `908258a9`), which followed wave 4
  (PR #761, `7c744828`) (update at wave boundaries)
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

## Harness integration surface (wave 4 and agent artifacts, both merged)

Wave 4 merged 2026-09-12 as `7c744828`. Its agent-artifacts successor merged 2026-09-14 as
`908258a9`. Neither SDD is locked: both retain open live-acceptance items. Everything below was
re-derived from `main` after the successor landed, because several wave-4-era facts recorded here
were changed by it.

- **The capability spans four facets.** `required_operations` is
  `frozenset({"start", "vm_init", "user_init", "workspace_init"})`
  (`cli/agentworks/capabilities/harness_integration/kinds.py:121`), and `contract_version` is **6**
  (`:118`): wave 4 took it from 3 to 4, and #794 bumped it twice more. The base declares the setup
  methods at `capabilities/harness_integration/base.py:307`, `:315`, and `:323`, with `check_setup`
  at `:331` and `start` at `:447`.
- **The base still refuses an unimplemented facet, but no first-party integration relies on that.**
  Those three base methods raise `StateError` naming the integration and facet, which is wave 4's
  reversal of the earlier no-op-defaults rule. Since #794, all four shipped integrations override
  all three: claude, codex, grok, and shell each implement `vm_init`, and each one `defer`s its VM
  artifacts to a later facet rather than placing files at the VM. So the refusal now guards future
  and third-party integrations rather than describing current behavior.
- **`config_for` carries its facet argument** at `capabilities/base.py:340` and
  `capabilities/harness_integration/base.py:246`. The default keeps single-config capabilities
  unchanged.
- **The harness leak into core is closed, which was wave 4's stated acceptance test.**
  `claude_marketplaces` and `claude_plugins` no longer sit on the agent template or admin config,
  and the `install_claude_plugins` VM-init step is gone. They survive only as `LEGACY_CLAUDE_FIELDS`
  (`cli/agentworks/legacy_claude.py:22`) with migration-facing error handling
  (`cli/agentworks/instance_overlay_codec.py:83`, `cli/agentworks/schema/errors.py:579`), so an
  operator's old declaration is diagnosed rather than silently ignored.
- **Artifacts shipped in the successor.** `cli/agentworks/artifacts/` carries declarations, capture,
  codec, routing, publication, session handling, inspection, and applied state. `artifact-bundle` is
  a registered resource kind (`artifacts/kinds.py:69`) declaring four types: hint, rule, skill, and
  agent personas (`artifacts/model.py:29-32`). Hints and rules may be inline text or a source
  reference (`artifacts/declarations.py:54-58`); skills and agent personas require a source
  (`:77-88`) and have no inline form. `agw artifact show` explains declarations and recorded
  delivery without applying anything. Claude, Codex, and grok have native artifact modules; shell's
  delivery lives at `artifacts/native/shell.py` and does not claim to load files into model context.
- **Applied state gained two keys without touching the table.** `AppliedStateKey` carries
  `HARNESS_NATIVE_SETUP` (`cli/agentworks/db/instance_state.py:42`) and `ARTIFACT_INPUTS` (`:43`),
  with `ARTIFACT_INPUTS` valid for vm, workspace, agent, **and** session owners (`:63-76`).
- **Captured artifact content is persisted in that typed payload, bytes included.**
  `artifacts/codec.py` encodes `content.text` and base64-encoded member `data` into the
  `VersionedPayload` that `artifacts/state.py` writes under `ARTIFACT_INPUTS`. Capture limits bound
  what can be persisted (`codec.py:29-30`, `package_sources.CaptureLimits`). This is worth flagging
  rather than burying: `cli/agentworks/db/README.md` describes instance-state payloads as
  "deliberately compact" and tells a new consumer not to add a generic blob API, so bounded artifact
  bodies in the payload sit in tension with that contract. The saga lead raised content-in-payload
  as a finding during wave 4, read the successor's design as having moved bytes out, and was wrong
  about the implementation. Whether the bound makes this acceptable is an open operator question,
  not a settled one.
- **Feature capability kinds remain future work**, with their pipeline position between core and
  integrations preserved as guidance only.

## Session runtime (observability groundwork)

- **Session and run identity now exist** (migration 38, delivered by PR #794 under the
  scope-participation contract's early-landing allowance). `SessionRow` carries `session_uuid`
  (`NOT NULL UNIQUE`, so never-reused is a schema constraint rather than a convention) and a
  nullable `run_id` (`cli/agentworks/db/models.py:178-180`). Existing sessions received a durable
  UUID at migration; none received an invented run, and a legacy incarnation takes its first
  `run_id` at its next managed launch. The operator-facing name stays the reusable human key, so a
  delete-and-recreate under one name can no longer splice histories. This was previously recorded
  here as the single sharpest schema gap for the observability effort; it is closed.
- `boot_id` remains what it was: `SessionRow` carries `pid`, `boot_id`, and
  `tmux_server_start_ticks` together as the tmux server's process fingerprint, used for teardown
  identity. That is process identity, not workload identity, and the new fields are what carry
  workload identity.
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

- **Windows is now covered by CI; macOS still is not.** PR #747 took the suite from 558 failures to
  zero on a Windows host (carrying two product fixes with it, a samples guard and a confirm-helper
  stdout fix), and PR #760 added the job: `test-windows` runs on `windows-latest`
  (`.github/workflows/ci.yml:87`). Both merged 2026-09-06, so the Windows half of this gap is closed
  structurally rather than by one-off effort. The macOS half is unchanged and the reasoning below
  still applies to it. The Windows-only `vm create` break that PR #677 fixed is why it mattered: no
  gate could have caught it, and it reached a published release. The exposure is structural rather
  than incidental: any platform-conditional path is unverified until an operator hits it, and the
  mechanism there (`subprocess.run(..., text=True)` wrapping stdin in a `TextIOWrapper` that
  rewrites LF to `os.linesep`) was invisible on Linux by construction. Recorded as a known gap, not
  a scheduled item.

- **Green CI does not cover the harness-integration subsystem's native paths.** The artifact and
  harness fixtures skip when the native CLI is absent, and CI installs neither `codex` nor `claude`,
  so the tests that exercise real native behavior do not run on the runner. At this snapshot the
  Linux job reports 9,478 passed and 55 skipped, and the Windows job runs a marker-selected subset
  (197 passed, 15 skipped). The efforts disclosed this rather than claiming the coverage, and
  retained a prepared patch to install the CLIs in CI. It is the same shape as the Linux-only gap
  above that let a Windows `vm create` break reach a published release: a class of regressions "all
  checks pass" does not speak to. This is a statement about automated regression coverage, not about
  whether the subsystem works, since live integration testing does exercise these paths against real
  VMs and native CLIs. The gap is that a future change can break them without CI noticing. How many
  of the 55 skips are native-CLI skips has not been re-measured at this snapshot; an earlier
  measurement during wave 4 is not carried forward here because its figures came from two different
  runs. Open.
