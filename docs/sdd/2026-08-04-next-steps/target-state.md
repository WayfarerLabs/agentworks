# Target State

- Status: North star, accumulating settled rulings
- Last updated: 2026-08-05

This document describes where Agentworks is going across this roadmap effort, synthesized from the
perspectives in `inputs/`. It is the target of these waves, not a forever vision: when
`current-state.md` agrees with this document and every child SDD is locked, the roadmap is done.
Every phasing choice is tested against these destinations, and individual efforts must not paint
over them. Settled operator rulings are recorded here with dates; a child SDD builds on these rather
than reopening them.

## The seven destinations

Destination 1 is the priority; the rest are not strictly ordered.

1. **An operator experience that scales with the surface.** Onboarding, capability discovery, and
   schema discovery are derived from the same registries, schemas, and samples the framework makes
   authoritative, so they cannot go stale, and every surface serves humans and agents alike
   (discoverable CLI, machine-readable output, shipped skills). The user perspective's
   skills-plus-CLI pattern is one investment serving all of these.
2. **One declarative resource model.** Registration-time Pydantic models are the single authority
   for validation, reference extraction, schema emission, samples, and describe surfaces. One decode
   frontend (YAML manifests), no lockstep twins.
3. **A capability framework that scales by kind.** A core-owned capability-kind descriptor replaces
   the per-kind switchboard, so adding a kind is a registration, not a coordinated edit across
   adapter tables, graph stamps, publishers, and snapshot logic.
4. **Harness integration as one identity with per-scope contributions.** VM, admin, agent,
   workspace, and session contributions are explicitly selected at their owning level, applied by
   their owning lifecycle, and never smuggled through session operations.
5. **The session event stream as a platform.** Every integration fuses its best available sources
   into one Agentworks-owned, best-effort event vocabulary. Transcripts, live frontends, ACP,
   structured control, audit sinks, the distiller, and the VM auto-suspend idle signal are all
   downstream consumers of that one representation.
6. **The memory-learning loop.** Learnings flow out of sessions over the event stream, a high-trust
   distiller curates them across all sessions and agents, and the agentic-artifacts layer (rules,
   skills, hooks) is the reviewed write-back path into future sessions. This was present as
   "distillation" in the harvested harness-transcripts FRD
   (`inputs/harness-transcripts-harvest.md`), was dropped in the observability reframe, and is
   restored as a first-class consumer. It is why the event vocabulary must stay analysis-friendly
   and Agentworks-owned.
7. **A stable plugin boundary, last.** External plugin promises come only after the internal
   contracts (descriptor, schema, facets) have been proven by first-party use.

## Settled contracts and rulings by area

### Operator experience (destination 1)

Plan A for onboarding: the operator's existing vanilla workstation harness drives setup, consuming
harness-specific plugins or marketplace entries published from the Agentworks repo. The onboarding
agent deliberately sits outside Agentworks (a managed agent must not modify the system it runs in),
so the agentic-artifacts layer is not the delivery path for onboarding skills. Onboarding is
idempotent and rerunnable, and conspicuously consent-first about probing the operator's machine.
Discovery and schema help are derived from registries, schema emission, live samples, and describe
surfaces so they cannot drift.

The teaching surface (operator rulings, 2026-08-05): `agw guide [topic ...]` serves skill-shaped
markdown for agents and humans alike, blending static authored content with dynamic content from the
live system. Topics span resource kinds (with live instance lists), specific resources, capability
implementations, and `concept-` prefixed meta topics (collision-free and discoverable by
completion). Output is markdown only. Every kind, implementation, and plugin contributes its own
topics through one generic contract, with built-in content living beside the kind it documents, and
contributed content is data rendered through locked-down templating, never code, against a
pared-down, read-only projection of the resource graph anchored by `me` shorthand (the resource the
topic documents), with rendering side-effect-free. The published harness plugins reduce to thin
bootstraps (install, disclose, run `agw guide`), which makes cross-harness parity structural. The
command is named `guide`, not `skill`, reserving the skill noun for the artifacts layer (destination
6's rules, skills, hooks). Reference surfaces (describe, schema, samples) and the teaching surface
render the same underlying sources: wave 2 owns the sources and the reference surfaces, the
onboarding child owns the guide. A live example of the artifact need (operator observation,
2026-08-05): this workspace authenticates GitHub through a custom git credential helper serving
fine-grained PATs by full HTTP path, environment knowledge an agent currently must be told in
conversation; a feature provisioning such a helper should emit exactly that fact as a skill.

### Declarative model (destination 2)

Adopted from the declarative-schema effort as fixed input: the model-as-authority contract, the
two-walker split (a total, never-raising reference extractor plus a field-documentation walk), the
tagged-union capability config discriminated on `name` and assembled per kind at the
post-registration boundary, the error-framing bridge, and validation on effective (merged) config
keyed to the finalize fold. Phase 2 executes through the descriptor, not ahead of it.

Four doors stay open for per-instance configuration and the future living graph: source-agnostic
reference extraction, a general layer-stack merge rather than a template-only chain, graph
post-finalize immutability staying a registry/fold property rather than a model-layer assumption,
and one instance-state store designed once for instance specs, facet applied-state, and artifact
ownership records (three perspectives converge on that store).

### Capability descriptor (destination 3)

A core-owned, typed capability-kind descriptor registered once per kind, from which graph stamping,
plugin registration, row publication, inspection, and consistency checks derive. Kinds remain
core-owned; domain operations remain on each kind's interface. The descriptor owns the config schema
contract and the per-kind tagged-union assembly.

**Secret backends are ordinary capabilities, full stop** (operator mandate, 2026-08-05). They live
in the `capabilities/` tree on the shared capability contract, and the descriptor work is free to
massage the base abstraction to make that true; the backend/source split is the mechanism for the
mandate, not an exemption from it (backend parallels `vm-platform`, source parallels `vm-site`). The
one piece the descriptor design must address deliberately is lifecycle layering: secret resolution
runs upstream of every other capability's runup, so a source's own lifecycle sits one stage earlier,
and a source's config must not depend on secrets served by another source unless the active chain's
ordering is explicitly promoted to a resolution order.

### Secrets (destinations 2 and 3)

The two-level `secret-backend` / `secret-source` model: a source exposes KV secrets and maps to one
backend with that source's config; per-source mapping to multiple backends is not required. The
settled reference shape (operator, 2026-08-05) is the synthesized-source model: every per-secret
reference names a source, and zero-config backends get synthesized sources under their current names
(`env-var`, `prompt`) so the simple case keeps its current spelling with only one concept in the
model. Direct backend references become a deprecated compatibility path rather than a permanent
second branch. The resolution API evolves in the same effort: typed per-secret outcomes, explicit
failure categories, policy-aware interaction requirements, timeouts and cleanup, and bounded-
lifetime source clients. The simple case must not get more verbose.

### Harness scopes (destination 4)

One registered integration identity with per-scope participation (operator simplification,
2026-08-05, superseding the earlier facet framing; see `scope-participation-contract.md`). The
integration API carries per-scope init methods (vm, agent, workspace) alongside session start and
resume, called by the per-scope orchestrators at the end of each setup pipeline: core first, then
features in declaration order receiving env-to-date, then integrations receiving all env and agent
artifacts for the scope. Templates at each owning level select their integrations and may attach
per-scope config, validated by per-scope config models. Scope discipline is trust-based: core does
not enforce harness behavior; code review and testing gate system plugins, and wave 8's
distribution-trust model gates external ones. Sessions receive all ancestor env and artifacts, and
the integration owns hoisted representation, deduplication, and double-provisioning avoidance
(isolation, not security). Session operations diagnose upstream gaps but never repair them. Artifact
conduct is conventional: smallest ownership unit, no silent overwrite of repository or
generator-owned content, applied state recorded, drift reported. The Claude-specific template fields
(`claude_marketplaces`, `claude_plugins`) migrate into the Claude integration's agent-scope config.
Rulesync informs the artifact design but is not a runtime dependency.

### Observability (destinations 5 and 6)

The universal event vocabulary is Agentworks-owned and independently versioned; ACP is a projection,
never the system of record. Integrations own fusion of every useful source (PTY parsing is
legitimate); core owns identity, primitives, transport, and persistence. Session/run identity
distinguishes the stable logical session from one workload incarnation. Heartbeats are liveness, not
activity: the vocabulary must keep "the workload is alive" and "someone is doing something"
distinguishable, because VM auto-suspend keys on the latter. The layered threat model (observation
fidelity, collector survivability, adversarial assurance) frames what any slice may honestly claim.
The distiller consumes the record store and proposes reviewed PRs, never direct commits; harness
memory is a cache, the repository is the system of record, and distillation is the flush.

### Compatibility posture (all destinations)

Breaking changes are acceptable across this roadmap provided each ships with a deprecation runway
and migration helpers: warn and migrate in one release, reject in the next (the 0.13 to 0.14
pattern). Deprecations are dropped on their scheduled release rather than accumulating: wave 1
restores that baseline by clearing every expired surface, and each later breaking wave clears its
own runway on schedule so the target state carries no expired compatibility. The generic deprecation
framework survives every cleanup.

### Cross-cutting: anchored projections (all destinations)

A recurring principle, now named (operator agreement, 2026-08-05), that child SDDs should test
designs against: contributions declare rather than do, and access arrives as an anchored, typed
projection rather than ambient authority. Instances already settled across this roadmap: the `me`
anchored template projection, per-integration state namespacing, declared secret references resolved
at the operation boundary, core performing tmux and PTY operations on integrations' behalf, and the
universal event representation. The principle governs surfaces where enforcement is real; trusted
in-process integration code is governed by trust, review, and disclosure instead (operator ruling,
2026-08-05), which is why harness scope discipline is a reviewed convention, not a grant system. The
review question for any new contribution surface: what does the contribution see, and where is that
view enforced, and if it cannot be, who reviewed the trust?

The template projection is expected to be the resource graph itself in a gated access mode, not a
second structure kept in lockstep: powers (secret readers, run targets, capability API objects) sit
behind callables a mode can gate, while universal facts are plain data on the nodes. Gated modes
expose only already-materialized data; nothing lazily computes through a power while wearing
attribute syntax. Gating by permission check or by leaving powers unwired are both legitimate
mechanisms, chosen per surface and done properly. Authored content still carries the teaching; the
graph carries the dynamic truth, and no effort should over-index on pushing everything into the
graph. Where a projection is impossible (the workstation agent sits outside the platform), the
principle inverts to disclosure, per the onboarding security disclosure.

## Explicitly out of scope

These are not part of this roadmap's target state. They are recorded so their triggers are not lost,
and so no wave accidentally forecloses them:

- **The living graph** (per-instance specs introducing post-finalize graph updates). A future SDD;
  the four open doors under destination 2 keep it unblocked.
- **The herdr rendering backend.** Gated on its spike per the 2026-07-30 ruling; the
  ephemeral-agents direction and observability's authoritative state reporting are the revisit
  triggers.
- **The named-console-template selector SDD** (`2026-07-19`, drafted pre-roadmap) and the
  companion-shell and resilient-attach wins unbundled from the herdr FRD. Standalone work that
  proceeds independently of this roadmap.
