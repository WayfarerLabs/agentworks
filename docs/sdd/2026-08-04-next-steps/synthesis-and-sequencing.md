# Next-Steps Synthesis and Sequencing

- Status: Synthesis draft for operator review
- Date: 2026-08-05
- Baseline: Agentworks 0.13.0 (`v0.13.0`), branch `docs/next-steps`
- Inputs: the six perspective documents in this directory (including the 2026-08-05 updates to the
  declarative-schema and session-observability perspectives and the new user perspective), code
  reconnaissance at HEAD, and review of the unmerged draft SDDs on `feat/harness-transcripts-sdd`,
  `feat/herdr-integration-sdd`, `feat/named-console-template`, and `feat/declarative-schema-sdd`

## Purpose

This document synthesizes the six perspectives into one roadmap-level ordering. It records what the
perspectives already fix, what code reconnaissance verified or corrected, the dependency structure
between the workstreams, and a recommended wave sequence with explicit operator decision points. It
is the bridge between the perspective-gathering stage of this SDD and whatever requirements
artifacts follow.

## Roadmap Through-Lines

Every sequencing choice below is tested against these seven destinations. They are the "where we
ultimately want to go" that individual efforts must not paint over.

1. **One declarative resource model.** Registration-time Pydantic models are the single authority
   for validation, reference extraction, schema emission, samples, and describe surfaces. One decode
   frontend (YAML manifests), no lockstep twins.
2. **A capability framework that scales by kind.** A core-owned capability-kind descriptor replaces
   the per-kind switchboard, so adding a kind is a registration, not a coordinated edit across
   adapter tables, graph stamps, publishers, and snapshot logic.
3. **Harness integration as one identity with scoped facets.** VM, admin, agent, workspace, and
   session contributions are explicitly selected at their owning level, applied by their owning
   lifecycle, and never smuggled through session operations.
4. **The session event stream as a platform.** Every integration fuses its best available sources
   into one Agentworks-owned, best-effort event vocabulary. Transcripts, live frontends, ACP,
   structured control, and audit sinks are all downstream consumers of that one representation.
5. **The memory-learning loop.** Learnings flow out of sessions over the same event stream, a
   high-trust distiller curates them across all sessions and agents, and the agentic-artifacts layer
   (rules, skills, hooks) is the reviewed write-back path into future sessions. This was present as
   "distillation" in the 2026-07-29 harness-transcripts FRD, was dropped when the observability
   perspective reframed that work, and is restored here as a first-class downstream consumer. It is
   why the event vocabulary must stay analysis-friendly and Agentworks-owned.
6. **An operator experience that scales with the surface.** Onboarding, capability discovery, and
   schema discovery are derived from the same registries, schemas, and samples the framework makes
   authoritative, so they cannot go stale, and every surface serves humans and agents alike
   (discoverable CLI, machine-readable output, shipped skills). The user perspective's
   skills-plus-CLI pattern is one investment serving all of these.
7. **A stable plugin boundary, last.** External plugin promises come only after the internal
   contracts (descriptor, schema, facets) have been proven by first-party use.

## Verified Current State

Reconnaissance confirmed the perspectives are building on accurate ground, with a few corrections
worth carrying into the follow-on efforts.

### Declarative schema

Phase 1 (TOML sunset) is complete on `feat/declarative-schema-sdd`: the hard error is in, the
fixture suite is converted and green, the records are written, and the final review approved it. It
merges to `main` alone via the retargeted PR #316; the SDD stays unlocked while phase 2 holds at the
phase gate until this SDD settles the descriptor and the 0.14 removal ordering. `main` is a strict
ancestor of the branch, so the merge is clean.

The updated perspective also records forward pressure this roadmap must not wall off: per-instance
configuration (an optional instance `spec` merged as one more layer atop the template rollup) and
the "living graph" it implies (the dependency graph becoming a function of config plus live instance
state, a future SDD of its own). Phase 2 and the descriptor work keep four doors open:
source-agnostic reference extraction, a general layer-stack merge rather than a template-only chain,
graph post-finalize immutability staying a registry/fold property rather than a model-layer
assumption, and one instance-state store designed once for both instance specs and harness
applied-state (the capability and harness-scope perspectives converge on the same store).

### Deprecation removal targets

Every surface in the removal perspective exists at HEAD as described. Corrections for the removal
FRD:

- `agw vm console` is not a thin alias. It is a separate legacy implementation
  (`cli/agentworks/sessions/console.py`, ~218 lines) unrelated to the canonical `agw console`
  family's `multi_console` code. Removal deletes a module, not a wrapper, and the call-graph caution
  in the perspective is warranted for a different reason than it assumed: there is no shared
  implementation to protect, but there is a whole subsystem to retire.
- Two aliases are silently accepted today with no warning at all: `[paths].code_workspaces` and
  `agw vm shell --provisioner`. Their removal is a hard behavior change with zero prior notice, so
  the removal release notes must call them out explicitly.
- The `[user]` section warning bypasses the aggregated deprecation channel (raw stderr print), an
  inconsistency that dissolves once the alias is removed.
- `output.phase()` and `env_compat.py` are fully dead (zero production callers); `UserConfig` is a
  one-line alias. These are trivial deletions.
- The TOML resource path still only warns at HEAD on `main`; the hard error is phase 1's, on its
  branch. This confirms the two efforts are complementary halves of one breaking release.

### Capability framework

- Exactly four kinds exist, and the switchboard is real: the kind set is independently enumerated in
  at least six places (adapter table, graph kind set and readiness dispatch, registry loaders,
  bootstrap publication, plugin snapshot/restore, manifest kind sections), plus a five-method
  adapter class, a publisher, an entry dataclass, and a `ResourceKind` strategy repeated per kind. A
  guard test makes a fifth kind fail loudly, but every site still needs hand-extension. This is the
  concrete duplication surface the descriptor collapses.
- The secret-source recommendation goes with the grain of the code, not against it: the backend
  module's own docstring already names the graduation signal ("when a backend needs config shared
  across many secrets... graduate the backend to a declarable instance kind, the secret-backend
  analog of vm-site"). Wave 3 fulfills a documented anticipation.
- There is no Pydantic anywhere in `cli/` today, and the tagged-union `{name: ...}` config shape
  already works at HEAD for three surfaces (vm-site, git-credential, the session harness selector),
  implemented three times independently. Phase 2 therefore consolidates an existing pattern behind
  one contract rather than introducing a new one.
- One irregularity worth fixing during descriptor work: `_VMPlatformKind` lives in `vms/kinds.py`,
  outside `capabilities/`, unlike its three siblings.

### Session runtime (observability groundwork)

- Sessions have no run/incarnation identity. `sessions.name` is the sole key and is reusable after
  delete-and-recreate; `boot_id` exists only to detect VM reboots. Any transcript keyed by session
  name alone will splice unrelated histories. This is the single sharpest schema gap for the
  observability effort.
- There is no PTY observation, no input interception, no event or fanout infrastructure, and no
  supervisor or heartbeat. tmux owns the PTY (one tmux server per session on a private socket);
  Agentworks only ever pulls scrollback via `capture-pane`.
- The one existing push-style precedent is the Codex `notify` recorder
  (`plugins/codex/recorder.py`): the harness invokes an Agentworks-provisioned script with a
  structured JSON payload per turn, which today extracts a single thread id and discards the rest.
  This is the embryo of the "harness reports events" channel.
- The Claude integration only probes for its transcript file's existence to decide resume-vs-launch;
  nothing reads transcript content yet.

### Open SDD ledger

- Release-spanning, blocked on 0.13.0 having shipped (it has): `2026-08-03-harness-integration`
  (phase 2 removal + closeout) and `2026-08-04-session-resume` (phase 6 removal + closeout).
- Operationally complete, needing verification and `locked.md` only: `2026-03-29-proxmox-provider`
  (20/20 checked), `2026-05-03-session-enhancements` (69/69 checked).
- `2026-03-26-mise-integration` shows 0/30 checked but the implementation demonstrably landed
  (`vms/initializer/mise.py`, `sources.py`, config fields). Its checkboxes need evidence-based
  reconciliation before locking, per the deprecation perspective's rule.
- Unmerged drafts: `2026-07-29-harness-transcripts` (superseded in substance by the observability
  perspective, except its distillation half, which through-line 5 restores),
  `2026-07-29-herdr-integration` (explicitly not scheduled, gated on a spike, but unbundles two
  standalone wins), `2026-07-19-named-console-template-selector` (small, mechanical, ready).

## Dependency Structure

The load-bearing edges, each traceable to a perspective or to code reality:

```text
phase 1 (TOML sunset) ----------> everything touching resource decode
0.14 removals ------------------> phase 2 harness modeling   (do not model shapes being deleted)
descriptor design --------------> phase 2 per-kind modeling  (do not bake in the switchboard)
phase 2 models -----------------> secret-source instances    (per-source config wants models)
descriptor + phase 2 -----------> harness facet framework    (facets declare model + merge policy)
facet framework ----------------> artifacts materialization  (facets own placement/applied state)
facet contract design ----------> observability integration contract (observation is a facet)
observability phase 1 ----------> structured control, distillation, vm auto-suspend, herdr
phase 2 surfaces ---------------> schema-derived onboarding and discovery
internal contracts proven ------> external plugin API
```

Two things this graph deliberately does not serialize:

- **Design versus code.** The removal work is mechanical while the descriptor, facet, and event
  vocabulary design is thinking work. They proceed in parallel without contention.
- **Observability implementation versus the capability track.** They touch nearly disjoint code
  (session runtime and tmux versus config, decode, and registries). Once their shared contract (the
  observation facet's shape) is designed, the two tracks can run concurrently as bandwidth allows.

## Recommended Sequence

### Wave 0: merge phase 1 (done, awaiting merge)

Phase 1 is complete and approved; merge the retargeted PR #316 to `main`. Every other wave assumes a
single declaration frontend.

### Wave 1: the 0.14 cleanup release

Run the deprecation-removal effort as its own modest SDD, per its perspective's shape, on top of the
merged phase 1 (they overlap in `loaders_sessions.py`, `manifests/decode.py`, and
`migrate/planning.py`; sequencing them avoids a messy rebase). Budget the fixture-conversion cost up
front, phase 1's chief lesson. Fold in the SDD closeouts: finish and lock harness-integration and
session-resume, lock proxmox and session-enhancements, reconcile mise's stale checkboxes with
evidence. Ship 0.14.0 as one coherent breaking-cleanup release: TOML resource hard error plus the
expired compatibility removals, with release notes that explicitly cover the silently-accepted
aliases being retired.

### Design track (parallel with waves 0 and 1)

Continue this SDD's requirements work while the cleanup executes. It must settle, in roughly this
order:

1. The capability-kind descriptor contract and what stays domain-owned.
2. The reference-field metadata vocabulary (secrets, resource refs, future facet refs) sized to
   avoid a second redesign.
3. The facet model boundary shared by harness scopes and observability (a facet declares support,
   owning scope, config model, merge policy, grants, state schema), including session/run identity
   semantics.
4. The instance-state store, designed once for per-instance specs, facet applied-state, and artifact
   ownership records, since three perspectives now converge on it.
5. The universal event vocabulary's first slice and its named consumers: the distiller/learner and
   the VM auto-suspend activity signal. The updated threat model's layering (observation fidelity,
   collector survivability, adversarial assurance) frames what this slice may honestly claim.

### Wave 2: declarative schema phase 2, through the descriptor

Unhold phase 2 and execute it as the first consumer of the descriptor design: per-kind models
registered via the descriptor's config-schema contract, tagged-union hardening (old sibling shape to
a hard error, in-place manifest upgrade), schema emission, and live samples. Absorb here the
deferred removals that belong to it: the generic discriminator compatibility and the fate of
`agw resource migrate` and its frozen TOML oracle. Honor the four open doors recorded under
"Declarative schema" above so the later instance-spec and living-graph work is not blocked.

### Wave 3: secret-source instances

Introduce the two-level `secret-backend` / `secret-source` model on top of phase 2's models: migrate
the active chain and mappings, normalize the backend registry away from constructed singletons, and
take the resolution-API evolution (typed outcomes, failure categories, cleanup) in the same effort.

Operator mandate (2026-08-05): secret backends are ordinary capabilities, full stop. They live in
the `capabilities/` tree on the shared capability contract, and the descriptor work is free to
massage the base abstraction to make that true; the backend/source split is the mechanism for the
mandate, not an exemption from it (backend parallels `vm-platform`, source parallels `vm-site`). The
one piece the descriptor design must address deliberately is lifecycle layering: secret resolution
runs upstream of every other capability's runup, so a source's own lifecycle sits one stage earlier,
and a source's config must not depend on secrets served by another source unless the active chain's
ordering is explicitly promoted to a resolution order.

Operator design input for the wave 3 FRD (2026-08-05): a source exposes KV secrets and maps to one
backend with that source's config; per-source mapping to multiple backends is not required. On
reference shape, the settled direction (operator, 2026-08-05) is the synthesized-source model: every
per-secret reference names a source, and zero-config backends get synthesized sources under their
current names (`env-var`, `prompt`) so the simple case keeps its current spelling with only one
concept in the model. Direct backend references become a deprecated compatibility path rather than a
permanent second branch. Breaking changes are acceptable across this work provided each one ships
with a deprecation runway and migration helpers (the 0.13 to 0.14 pattern: warn and migrate in one
release, reject in the next). The simple case must not get more verbose.

### Wave 4: harness facet framework, one vertical slice

Implement the facet model designed earlier: attachments at VM/admin/agent/workspace/session
ownership points, the applied-state ledger, one vertical integration proving create/reinit
semantics, workspace create-time materialization with file-level ownership, upstream requirement
reporting without implicit remediation, and the migration of `claude_marketplaces` /
`claude_plugins` into the Claude integration's facet config.

### Wave 5: observability phase 1 (may start alongside waves 2 through 4)

Implement the designed event vocabulary and session/run identity, session-level PTY observation and
the input-interception investigation, and one vertical fusion integration (Claude Code is the
natural first: transcript file, hooks, PTY liveness; generalize the Codex notify channel as the push
mechanism). Persist a simple transcript with replay. Declare coverage honestly. The run-id schema
change is small and self-contained; if a DB migration is convenient during wave 1, it may land
early, but nothing in waves 1 through 4 depends on it.

Two consumers land on this stream shortly after it exists. The distiller (wave 6) is one. The other
is VM auto-suspend: a platform feature where a VM suspends itself after a period of inactivity, with
transcript-derived activity as the general idle signal. Liveness heartbeats explicitly do not count
as activity; the event vocabulary must keep "the workload is alive" and "someone is doing something"
distinguishable, which the observability perspective's heartbeat semantics already require for
honest loss reporting. The suspend mechanics themselves are vm-platform capability work
(per-platform suspend support) and can be designed independently; only the idle signal waits on this
wave.

### Wave 6: artifacts and the learning loop's write-back path

First-class agentic contributions (rules, skills, hooks) through the facet materialization seam,
with composition, provenance, and drift detection. Revive the distillation design from the
harness-transcripts FRD against the real event stream: distiller reads transcripts, curates across
sessions, proposes reviewed PRs. This closes the memory-learning loop.

A live example of the need, from this roadmap's own development: this workspace authenticates GitHub
through a custom git credential helper that serves fine-grained PATs by full HTTP path, which is
environment knowledge an agent currently has to be told in conversation. A feature that provisions
such a helper should emit exactly that fact as a skill so future agents in the environment use it
unprompted (operator observation, 2026-08-05).

### Wave 7: structured control and console rendering

Observability phase 2 (validated intents, ACP projection, stale-decision rejection). Revisit herdr
after its spike, which by then can use the authoritative-state path its FRD calls the convergence
point with this event stream.

### Wave 8: external plugin API

Registration conformance, discovery, namespacing, versioning, and the distribution-trust model,
promised publicly only once the internal contracts have survived first-party use.

### Operator-experience track (incremental, accelerates after wave 2)

The user perspective's onboarding, capability-discovery, and schema-discovery needs do not form one
big wave; they are a track that rides the others. Skills, CLI discoverability conventions, and
machine-readable output can start anytime and deliver value immediately. The schema-derived parts
(generated samples, describe surfaces, editor integration, dynamic onboarding content) become cheap
and stale-proof only once wave 2's schema emission exists, so the track's deeper investments
deliberately trail it.

The delivery model is settled in principle (the user perspective's plan A): the operator's existing
vanilla workstation harness drives onboarding, consuming harness-specific plugins or marketplace
entries published from the Agentworks repo. The onboarding agent deliberately sits outside
Agentworks (a managed agent should not modify the system it runs in), so this track does not depend
on the wave 6 artifacts layer at all, which decouples it further and lets it start early. Onboarding
itself must be idempotent and rerunnable, and conspicuously consent-first about probing the
operator's machine; trust is established in the first minutes or not at all.

### Recorded but unscheduled

Future SDDs this roadmap acknowledges without slotting: the living graph (per-instance specs
introducing post-finalize graph updates; the four open doors keep it unblocked) and the herdr
rendering backend (gated on its spike). Both have their triggers recorded rather than dates.

### Continuous lane: small standalone wins

Independent of the waves, schedulable as breathers: the named-console-template selector SDD (verify
PR #200's migration numbering first), the companion-shell command and resilient session attach
unbundled from the herdr FRD, and opportunistic doc/config hygiene.

## The Roadmap-SDD Model

This effort is a new artifact species: a roadmap SDD. It is a meta SDD that generates and tracks
ordinary SDDs rather than shipping an implementation of its own. The working model, to be refined as
we live with it:

- Its artifacts are the perspectives, this synthesis, the cross-cutting contract decisions from the
  design track, and a child-SDD ledger tracking each spawned effort and its status. Whether the
  contract decisions become an FRD/HLA analog or something new is deliberately left to discover in
  the process; the conventional artifact set was designed for a single effort, and forcing it here
  would be premature.
- Each wave executes as its own ordinary SDD (or an already-open one, like declarative-schema),
  authored against the settled contracts and closed out normally.
- The roadmap SDD stays open until every child SDD is closed, then locks. Its ledger is the
  roadmap's source of truth for what is spawned, in flight, blocked, and done, the same role plan
  checkboxes play in an ordinary SDD, one level up.
- All roadmap state lives on `main` so it is visible across the individual SDD tracks: every change
  (a new child SDD, a status change, a design revision) is a PR merged to `main`, and child SDDs
  point back to this roadmap SDD so the roadmap is discoverable from any effort.
- Division of labor (operator ruling, 2026-08-05): the roadmap lead seeds each child SDD with its
  FRD, plus critical architectural constraints the roadmap has already settled, but not the full HLA
  and not the plan. A separately launched effort lead picks the SDD up from `main` on a new branch
  and owns its HLA, plan, and implementation per the standard development process. The roadmap lead
  reviews each child SDD's PRs before merge.
- Terminology (operator ruling, 2026-08-05): this is a roadmap and its child SDDs, led by a roadmap
  lead and effort leads. The word "program" is deliberately avoided.
- Once the model has survived its first few child-SDD cycles, promote the roadmap-SDD concept into
  the `sdd` skill as its permanent home, so future roadmap efforts follow it without reading this
  SDD. Until then, this section is the working definition.

## Issue Intake (2026-08-05)

A sweep of the open issue tracker pulled the following issues into the roadmap. Each carries a
comment pointing back here so it is not worked out-of-band; issues not listed stay standalone.

| Issue      | Lands in                                                                           |
| ---------- | ---------------------------------------------------------------------------------- |
| #76        | Wave 1 SDD closeouts (stale lockfile half only; the derive audit stays standalone) |
| #165       | Continuous lane: the named-console-template selector SDD                           |
| #170       | Wave 2 rider (expires metadata belongs in the envelope/metadata modeling)          |
| #205, #212 | Design track: instance-state store and per-instance specs; future living-graph SDD |
| #214       | Wave 2 (live samples, uniform validation; unknown keys go hard-error there)        |
| #242       | Waves 4 and 5 (harness-owned adoption plus resume re-pointing)                     |
| #257       | Operator-experience track (machine-readable output contract)                       |
| #311       | Wave 2 (structural reference extraction from annotated models)                     |
| #370       | Wave 3 (resolution-API evolution owns the batching question)                       |
| #373       | Wave 4 (environment-appropriate defaults belong to facet config)                   |
| #374       | Design track descriptor plus wave 3 (two-level model, not the current base class)  |
| #387       | Waves 4 and 6 (workspace facets and features own post-clone setup)                 |
| #390, #391 | Operator-experience track (schema-derived samples and plan A onboarding)           |

Notes: #242 also picks up the 0.14 rename (it lands as `session resume --update-template`); the
capability-contract sibling cluster #368 through #374 splits, with #370/#373/#374 rolled in and the
platform-specific siblings staying standalone; #362 (codex enhancements) stays standalone as an
open-ended research placeholder.

## Release Mapping

- **0.14.0:** wave 0 plus wave 1. One breaking-cleanup story: single declaration frontend, expired
  compatibility removed, SDD ledger clean.
- **0.15.x:** wave 2, likely with the generic-discriminator hard error as its headline breaking
  change, plus whichever of waves 3 and 5 complete.
- **Later:** waves 4, 6, 7, 8 map to releases as they prove out; no need to pin numbers now.

## Decisions Already Fixed by the Perspectives

These are treated as settled input, not reopened here: phase 1 completes independently; phase 2
holds at the gate and executes through the descriptor; 0.14 removals precede phase 2's harness
modeling; the generic deprecation framework survives the cleanup; secret backends stay in the
capability model with a two-level instance split; wider harness scopes become facets, not methods on
the session-bound object; PTY parsing is a legitimate observation source; ACP is a projection, never
the system of record; Rulesync informs but is not a runtime dependency.

## Open Decisions for the Operator

1. **Observability parallelism.** Whether wave 5 truly runs alongside waves 2 through 4 is a
   bandwidth call, not a dependency call. The contract design in the design track is what keeps the
   option open.
2. **Herdr.** Stays gated on its spike per the 2026-07-30 ruling. The ephemeral-agents direction and
   wave 5's authoritative state reporting are the two signals that would justify revisiting.

Settled by operator ruling (2026-08-05): the artifact shape follows the roadmap-SDD model above
(per-wave child SDDs under this open roadmap SDD, artifact forms discovered as we go); 0.14.0
bundles phase 1's TOML hard error with the expired-compat removals as one breaking-cleanup release;
and the harness-transcripts FRD is harvested and then deleted, branch included, once its useful
content (distillation, record-store, and any other still-relevant requirements) is verifiably
extracted, so a superseded draft is not left around to confuse. A further ruling the same day:
secret backends are ordinary capabilities on the shared contract, with the descriptor free to
massage the base to make that true (see wave 3).

## Immediate Next Actions

1. Merge the retargeted PR #316 (phase 1 is complete and approved; operator is handling the merge).
2. Draft the deprecation-removal FRD from its perspective plus the corrections recorded here, and
   start wave 1.
3. In parallel, begin the design track's first artifact: the capability-kind descriptor contract.
4. Harvest the harness-transcripts FRD into this SDD, verify completeness, then delete its branch.
5. Stand up the child-SDD ledger for this roadmap SDD as the first experiment in its artifact forms.
