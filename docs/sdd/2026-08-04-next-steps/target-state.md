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
4. **Harness integration as one identity with scoped facets.** VM, admin, agent, workspace, and
   session contributions are explicitly selected at their owning level, applied by their owning
   lifecycle, and never smuggled through session operations.
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
surfaces so they cannot drift. A live example of the artifact need (operator observation,
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

### Harness facets (destination 4)

One registered integration identity with independently declared, explicitly selected facets (VM,
admin, agent, workspace, session), each declaring support, owning scope and lifecycle, config model
and merge policy, references, grants, readiness and idempotent operations, and versioned applied
state. Absence of a facet means unsupported. Templates at each owning level select their
integrations; session operations diagnose upstream gaps but never repair them. Artifacts (rules,
skills, hooks) are harness-independent logical contributions materialized by integrations, with
file-level ownership, provenance, drift detection, and an applied-state ledger. The Claude-specific
template fields (`claude_marketplaces`, `claude_plugins`) migrate into the Claude integration's
facet config. Rulesync informs the artifact design but is not a runtime dependency.

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
