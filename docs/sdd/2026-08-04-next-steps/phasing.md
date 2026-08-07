# Phasing

- Status: Active sequencing
- Last updated: 2026-08-05

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
  Budget fixture conversion as first-class work. Fold in the pre-roadmap SDD closeouts listed in
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
- **Closeout wave (gates the roadmap lock; operator ruling, 2026-08-06):** after the waves complete
  and before the roadmap locks, one comprehensive review of everything the roadmap changed, in
  strict priority order: (1) security above all, reviewing the accumulated whole as one attack
  surface (the trust-based integration discipline, the gated graph projection and guide content
  channel, secret-source resolution, the event stream) rather than per-change; (2) test
  consolidation and removal, with the working assumption that the accreted unit-test estate can be
  cut in half, maybe more, without sacrificing any coverage or quality; (3) code cleanup: file-size
  limits, vestigial code removal, package renaming and refactoring left behind by the waves' moves.
  Findings are fixed before the roadmap locks.
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

Adjacent standalone work, explicitly out of roadmap scope (see `target-state.md`): the
named-console-template selector SDD, the companion-shell command and resilient session attach
unbundled from the herdr FRD, herdr itself (behind its own spike gate), and opportunistic doc/config
hygiene. An active roadmap does not pause other development: anything outside its scope can be
picked off whenever bandwidth allows, on its own merits and its own schedule.

## Release mapping

- **0.14.0 (held; operator ruling, 2026-08-06):** the breaking cleanup does not ship alone. The cut
  waits for the guide first slice (guide command core, `concept-onboarding`, the README bootstrap),
  so the release that rejects old inputs also ships the CLI that teaches the new ones; newcomers
  ride the forgiving 0.13.0 until then. The installer-plugins child (operator ruling, 2026-08-07; no
  dependencies, runs any time) also gates the cut: its moves are breaking and belong in the same
  well-cushioned release. The 0.13.0 warnings stay true because the version number attaches to the
  breaking content, not the date. If wave 2's generic-discriminator hard error lands in the same
  window, it folds in: one well-cushioned breaking release instead of two. While `main` holds
  unreleased breaking changes, urgent operator fixes ship from a `0.13.x` backport branch.
- **Later:** remaining waves map to releases as they prove out; no need to pin numbers now.

## Open ordering decisions

1. **Observability parallelism.** Whether wave 5 truly runs alongside waves 2 through 4 is a
   bandwidth call, not a dependency call. The design track's contract work keeps the option open.
