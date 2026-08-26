# Phasing

- Status: Active sequencing
- Last updated: 2026-08-13

This document records only ordering: the dependency structure that forces the sequence, the waves,
and the release mapping. What each wave builds is defined by `target-state.md`; where the system
stands is `current-state.md`; per-effort status is `child-sdds.md`.

## Dependency structure

The load-bearing edges, each traceable to a perspective or to code reality:

```text
phase 1 (TOML sunset) ----------> everything touching resource decode
0.14 removals ------------------> phase 2 harness modeling   (do not model shapes being deleted)
descriptor design --------------> phase 2 per-kind modeling  (do not bake in the switchboard)
phase 2 models -----------------> secret-source instances    (per-source config wants models)
descriptor + phase 2 -----------> harness scope framework    (per-scope config models + pipeline)
scope framework ----------------> artifacts materialization  (integrations own placement/state)
scope contract design ----------> observability integration contract (same trust model)
observability phase 1 ----------> structured control, distillation, vm auto-suspend
phase 2 surfaces ---------------> schema-derived onboarding and discovery
internal contracts proven ------> external plugin API
```

Two things this graph deliberately does not serialize:

- **Design versus code.** Removal work is mechanical while descriptor, scope-contract, and event
  vocabulary design is thinking work. They proceed in parallel without contention.
- **Observability implementation versus the capability track.** They touch nearly disjoint code
  (session runtime and tmux versus config, decode, and registries). Once their shared contract (the
  observation contract's shape) is designed, the two tracks run concurrently as bandwidth allows.

## Waves

- **Wave 0 (complete, 2026-08-05):** phase 1 merged to `main` via PR #316. The single declaration
  frontend every other wave assumes is in place.
- **Wave 1: the 0.14 cleanup release.** The deprecation-removal child SDD, built on post-phase-1
  `main` (the efforts overlap in session-template loading, manifest decode, and migration planning).
  Budget fixture conversion as first-class work. Fold in the pre-saga SDD closeouts listed in
  `current-state.md`. Ships as one breaking-cleanup release with phase 1.
- **Design track (parallel with wave 1):** settle, in rough order: the capability-kind descriptor
  contract; the reference-field metadata vocabulary; the scope-participation contract shared by
  harness scopes and observability, including session/run identity semantics; the instance-state
  store (designed once for instance specs, integration applied-state, and artifact ownership); and
  the first slice of the universal event vocabulary with its named consumers.
- **Wave 2: declarative schema phase 2, through the descriptor.** Release phase 2 from its hold as
  the first consumer of the descriptor design. Absorb the removals deferred to it (generic
  discriminator compatibility, the fate of `agw resource migrate` and its frozen TOML oracle). Honor
  the four open doors so instance-spec and living-graph work is not blocked.
- **Wave 3: secret-source instances.** The two-level model per `target-state.md`'s secrets rulings,
  including the capability mandate, the synthesized-source reference model, and the resolution-API
  evolution.
- **Wave 4: harness scope framework, one vertical slice.** The scope-participation contract made
  real: per-scope init methods and the setup pipeline (core, features, integrations), attachments at
  every ownership point, applied state, one vertical integration proving create/reinit semantics,
  workspace create-time materialization, upstream prerequisite reporting without implicit
  remediation, and the Claude template-field migration.
- **Wave 5: observability phase 1 (may start alongside waves 2 through 4).** Event vocabulary and
  session/run identity, session-level PTY observation and the input-interception investigation, one
  vertical fusion integration (Claude Code first; generalize the Codex notify channel as the push
  mechanism), a simple persisted transcript with replay, honest coverage declaration. The run-id
  schema change is small and self-contained and may land early if convenient. Two consumers land
  shortly after the stream exists: the distiller (wave 6) and VM auto-suspend (the suspend mechanics
  are vm-platform work designable independently; only the idle signal waits here).
- **Wave 6: artifacts and the learning loop's write-back path.** First-class agentic contributions
  through the integration materialization seam; revive distillation from the harvest against the
  real event stream. This closes the memory-learning loop.
- **Wave 7: structured control.** Observability phase 2 (validated intents, ACP projection,
  stale-decision rejection).
- **Closeout wave (gates the saga lock; operator ruling, 2026-08-06):** after the waves complete and
  before the saga locks, one comprehensive review of everything the saga changed, in strict priority
  order: (1) security above all, reviewing the accumulated whole as one attack surface (the
  trust-based integration discipline, the gated graph projection and guide content channel,
  secret-source resolution, the event stream) rather than per-change; (2) test consolidation and
  removal, with the working assumption that the accreted unit-test estate can be cut in half, maybe
  more, without sacrificing any coverage or quality (the pre-0.14 simplification pass does this
  early, so what remains here is a verification sweep rather than the trim itself); (3) code
  cleanup: file-size limits, vestigial code removal, package renaming and refactoring left behind by
  the waves' moves, and an SDD tombstoning sweep (operator ruling, 2026-08-08): superseded SDDs'
  contents are deleted down to their `locked.md` tombstones per the sdd skill, with
  `2026-07-01-resource-manifests` the first identified candidate (its lockfile already carries the
  supersession record) and individual SDDs tombstoned earlier whenever reading them actively
  misleads. Findings are fixed before the saga locks.
- **Wave 8: external plugin API.** Registration conformance, discovery, namespacing, versioning, and
  the distribution-trust model, promised publicly only once the internal contracts survive
  first-party use.

## Tracks

- **Installer-plugins child (pre-0.14 core slimming): seeded 2026-08-07, launchable whenever.** The
  R1 inventory has no dependencies; the plugin moves consume wave 2's descriptor registration and
  the guide topics consume onboarding's first slice, both of which precede the 0.14.0 cut this child
  gates (see release mapping).
- **Onboarding-and-discovery child (destination 1): seeds at wave 1 completion, runs parallel to
  wave 2.** Slotted as early as sensible without rework: it teaches the post-cleanup 0.14 surface,
  so nothing wave 1 removes gets taught, and its first slice (onboarding harness plugin and skills,
  consent-first probing, the machine-readable output contract) needs no schema emission. The
  schema-derived depth (generated samples, describe surfaces, editor integration, dynamic onboarding
  content) consumes wave 2's emission as it lands. Plan A onboarding does not depend on the wave 6
  artifacts layer at all.

Adjacent standalone work, explicitly out of saga scope (see `target-state.md`): the
named-console-template selector SDD, the companion-shell command and resilient session attach
unbundled from the herdr FRD, herdr itself (behind its own spike gate), and opportunistic doc/config
hygiene. An active saga does not pause other development: anything outside its scope can be picked
off whenever bandwidth allows, on its own merits and its own schedule.

## Release mapping

- **0.14.0 (held; operator ruling, 2026-08-06):** the breaking cleanup does not ship alone. The cut
  waits for the guide first slice (guide command core, `concept-onboarding`, the README bootstrap),
  so the release that rejects old inputs also ships the CLI that teaches the new ones; newcomers
  ride the forgiving 0.13.0 until then. That gate is satisfied: the guide command core and
  `concept-onboarding` merged 2026-08-08 via PR #428, and the README bootstrap shipped with the
  assistance phase (PR #480, 2026-08-13; the generated block pins version 0.14.0 or newer, so it
  resolves for operators when the release itself ships). The installer-plugins child (operator
  ruling, 2026-08-07; launchable whenever, see Tracks) also gates the cut: its moves are breaking
  and belong in the same well-cushioned release. The 0.13.0 warnings stay true because the version
  number attaches to the breaking content, not the date. If wave 2's generic-discriminator hard
  error lands in the same window, it folds in: one well-cushioned breaking release instead of two.
  The vm-platform mode contract (PR #444, merged 2026-08-08) folds in the same way: its
  written-old-shape hard errors ride the cushioned release, and its
  omission-equals-historical-default posture means manifests that never wrote the retired blocks
  cross without edits. The git-credential one-arm union restructure (operator ruling, 2026-08-08,
  ahead of credential minting) landed 2026-08-08 via PR #455, following the same pattern, together
  with the survey-confirmed sibling restructures: the env structural union with legacy
  null-companion canonicalization, the github repos/owner scope-union dissolution, and the
  install-command multi-test widening. That gate is satisfied. The secret-sources reference break
  rides the cut as well (operator ruling, 2026-08-08): direct backend references hard-error with the
  exact rewrite and no warn window, because prompt and env-var spellings cross unchanged through
  synthesized sources and the affected surface is effectively the operator's own onepassword config;
  wave 3's breaking slice therefore gates the cut alongside the installer-plugins child. Two later
  gates joined (2026-08-09/10): the safer-migrations dispatched task (operator ruling: pre-migration
  notice, backup, and restore are table-stakes UX for the release that migrates every existing
  install, and the backup is also the 0.14-to-0.13 rollback path), and the pre-0.14
  test-consolidation child as a soft gate (the trim runs while the context is loaded, before the
  repo draws post-release attention). **Resolved as cut (operator ruling, 2026-08-17, in
  `target-state.md`)**: the simplification pass's sweep absorbed the trim's estate and no longer
  gates the release, so the soft gate resolves with it; the sweep continues on its own merits. The
  resource-CLI grammar break shares the window if the operator blesses it: breaking surface changes
  belong in the same cushioned release. While `main` holds unreleased breaking changes, urgent
  operator fixes ship from a `0.13.x` backport branch.

  **Ruling (operator, 2026-08-12):** the grammar rewrite is no longer conditional. 0.14.0 does not
  ship until it lands, so it joins the cut as a hard gate. **Original sequence (operator,
  2026-08-13, revised the same day):** the simplification pass runs before the grammar rewrite, not
  after it. The original order was (1) that pass's wave 0, which establishes that always-on rules
  actually reach the agents they bind (issue #511) and lands the deletion criteria, (2) its wave 1
  deletions, (3) the grammar rewrite, (4) its reassessment. Rewriting the CLI grammar over a surface
  that still carries the deletable scaffolding means the rewrite carries it too. **Amendment
  (operator, 2026-08-15):** wave 0 is complete, and corrected PR #548 is the only remaining
  simplification-pass prerequisite. The onboarding effort then takes a one-time combined boundary
  and removes all survey-approved guide content plus its directly orphaned machinery in one wave
  before the grammar rewrite. Other wave 1 deletions and wave 2 run independently; the pass's
  reassessment and lock still wait for both. The temporary guide gap is accepted only on unreleased
  `main`; the grammar rewrite gates 0.14.0 and restores the settled command destinations before
  release. The 0.14 breaking-truth items (S5, C3, C4, C7) run as their own dispatched task in
  parallel, since folding them into the grammar rewrite would grow an already massive effort
  (operator, 2026-08-13).

  **Current release map (operator rulings, 2026-08-17, in `target-state.md`):** every gate above is
  satisfied (the CLI grammar rewrite merged and locked with PR #491; breaking-truth, installer,
  safer-migrations, and the README bootstrap closed earlier), and the simplification-pass gate is
  removed as recorded above. The grammar-native guide gate is satisfied (PR #593, merged
  2026-08-18). **0.14.0 shipped 2026-08-18**: release PR #402 merged with the issue #589 changelog
  repair applied on the release branch (both breaking-migration entries complete on the GitHub
  Release and the installed release-notes topic, tester-verified on the exact wheel), and the PyPI
  publication landed via trusted publishing after PR #600 fixed the release workflow's missing base
  ref (the first tag-time run's fingerprint guard failed closed in a shallow checkout; the
  re-dispatch against tag `v0.14.0` published with the `release` environment's tag-only policy
  temporarily bridged for the one run and verified restored). `agentworks-cli` 0.14.0 is live.
  **0.14.1 shipped 2026-08-19**: the field-evidence fixes (PRs #604 through #607) cut as a patch via
  the `Release-As: 0.14.1` override (PR #617, reframing the minor bump two small feat commits would
  have forced), release PR #610 merged, and the workflow published first-try; the tester's
  post-merge pass verified reproducible builds and agreeing version surfaces on the exact release
  head.

- **0.16.0 (held; operator ruling, 2026-08-26):** the release PR does not cut until the
  `2026-08-19-instance-model` child's instance-spec overlays (PR #670) and the harness integration
  config knobs ([issue #674](https://github.com/WayfarerLabs/agentworks/issues/674), per-session
  workload inputs across the Claude Code, Codex, and Grok Build integrations) are both on `main`.
  Instance specs and the knobs that configure them ship together, because a release carrying the
  spec mechanism without the settings it exists to carry teaches half a feature. The mechanics
  matter here: the release PR accumulates whatever is on `main` when it merges, so cutting early
  does not delay those entries to a later release, it silently ships 0.16.0 without them and pushes
  them to 0.17.0. Everything already accumulated (the two Azure SDK migrations and their raised
  floors, the cloud-identifier and name-grammar validation work, the terminal-restore fix, the guide
  and plugin documentation corrections) rides the same cut.

- **Later:** remaining waves map to releases as they prove out; no need to pin numbers now.

## Open ordering decisions

1. **Observability parallelism.** Whether wave 5 truly runs alongside waves 2 through 4 is a
   bandwidth call, not a dependency call. The design track's contract work keeps the option open.
