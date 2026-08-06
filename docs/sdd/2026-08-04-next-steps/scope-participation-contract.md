# Scope Participation Contract

- Status: Design-track artifact, draft for review
- Date: 2026-08-05
- Inputs: the harness-integration-scope and session-observability perspectives (`inputs/`), the
  harness-transcripts harvest, `target-state.md`'s settled rulings,
  `capability-descriptor-contract.md`, and the session-runtime reconnaissance in `starting-state.md`
- Supersedes: the earlier facet-model framing of this artifact (operator simplification,
  2026-08-05). "Facet" is retired as vocabulary; operators and devs see scopes, features, and
  harness integrations.

## Purpose

This artifact settles how core, features, and harness integrations participate across the platform's
scopes, plus the session/run identity semantics the runtime side depends on. It is the shared
contract for the wave 4 and wave 5 seeds. It is a design decision record: those efforts own their
plans and code, and open items are marked for the seed that carries them.

## The model

Scopes are fixed: vm, admin, agent, workspace, session. They are not extensible and no scope
registry exists; if the set ever changes, that is an ordinary contract change, not a framework
event.

Lifecycles are what Agentworks already provides. VMs and agents initialize and idempotently
reinitialize (setup). Workspaces initialize once at create (setup). Sessions start and resume
(runtime), fixed at create with narrow resume evolution. Admin-scope setup rides the VM lifecycle,
as the perspective already held: admin identity work happens during VM init and reinit (today's
admin plugin installs inside the VM initializer are the precedent). It surfaces through the same
user-level method as agents: `user_init` runs for the admin user during VM init and reinit, and for
each agent during agent init and reinit.

### The setup pipeline

At every setup scope, one pipeline runs in one order:

1. **Core runs first.** Core setup may emit env and agent artifacts at this scope.
2. **Features run next** (`vm-feature`, `agent-feature`, `workspace-feature`), in template
   declaration order. A feature operates at its scope only: it receives env-to-date (everything
   emitted before it, including env inherited from higher scopes, which core assembles and delivers)
   and may emit env and agent artifacts alongside its own side effects. Declared cross-feature
   dependencies are deferred until a real case demands more than declaration order.
3. **Enabled harness integrations run last**, receiving all env and agent artifacts for the scope.

Reinit (vm, agent) reruns the same pipeline idempotently. Env and agent artifacts are the two
currencies of the pipeline; their concrete schemas are the wave 4 seed's to settle, and the artifact
schema MUST NOT foreclose wave 6's artifact model (stable identity, attributed composition, typed
hooks): composition and hook semantics belong to wave 6.

### The integration API

Harness integrations are the only participants that straddle scopes, so the scope surface lives
directly on the one registered integration API as methods (names indicative): `vm_init`,
`user_init`, `workspace_init`, alongside the existing session `start` and `resume`. `user_init` is
one surface for user-level setup: it is invoked for the admin user during VM init and for each agent
during agent init, the invocation context says which user, and one method body serves both. The
per-scope orchestrators call these at the end of their pipelines for the integrations the owning
template selects. Env is delivered through the run targets; agent artifacts arrive as inputs. The
base class provides no-op defaults, and an integration implements what it supports; this
deliberately supersedes the perspective's absence-means-unsupported rule (review and testing catch a
mistyped override), and whether a lightweight supported-scopes report exists for doctor and guide
output is the effort lead's call. A per-scope invocation is constructed for its owning resource; it
never reuses a session instance's target identity, readiness cache, or state namespace, which stay
session-bound. The Claude-specific template fields (`claude_marketplaces`, `claude_plugins`) migrate
into the Claude integration's user-scope config as wave 4 work (they already carry the same shape on
admin config and agent templates today).

Config is simply part of the stuff. A template that selects an integration may attach a config blob
at that spot, and like every other blob in the declarative world it is validated by a model the
integration registers for that spot (vm, user, workspace, session; the user model serves both the
admin attachment on the vm-template and agent attachments on agent-templates). The same model may
serve several spots, and accepting no config at a spot means registering no model. That is the
entirety of "per-scope config models": ordinary blob validation, not a mechanism.

### Trust, not enforcement

Integrations MUST honor scope discipline: an invocation at one scope does not reach into other
sessions or mutate other scopes' resources, because those can affect other sessions. Core does not
and cannot enforce this; the harness-specific knowledge lives in the integration, and pretending
core can police filesystem or SSH behavior of trusted in-process code would be enforcement theater.
The actual mechanism is trust: code review and testing gate system plugins, and external plugins are
risky for exactly this reason, which is what wave 8's distribution-trust model exists to gate. This
is a deliberate boundary on the anchored-projections principle: projections govern surfaces where
enforcement is real; trusted in-process integration code is governed by trust, review, and
disclosure.

### Sessions and hoisting

The session receives everything: env and agent artifacts from all ancestor scopes plus its own,
passed to the harness integration at start and resume. The integration owns representation. Env is
naturally process-scoped. Content that cannot be represented at its own scope is hoisted into the
session, and the integration decides placement using its harness knowledge (for example,
session-scoped artifacts under a harness-specific workspace path keyed by `session_uuid`), including
deduplication and double-provisioning avoidance. Hoisting is isolation, not security: other sessions
being able to see user-scope artifacts is expected; the integration's job is that hoisted material
only takes effect for its own workload. Session-scope env and artifacts come from the session
template's own declarations; there is deliberately no session-feature initially (one gets added only
if real pressure emerges).

### Artifact conduct

Behavioral conventions integrations follow (review-enforced, same trust basis): claim the smallest
practical ownership unit; never silently adopt or overwrite repository, operator, or generator-owned
content; record applied state (destinations, strategies, hashes) so reinit can converge and drift is
reported rather than fought; secrets never enter persisted state or resolved configuration.

### State

Per-(owning resource, integration) state follows the proven session pattern
(`harness_integration_state`: namespaced blob, persisted by the owning manager, degrading to empty
on malformed content). VM and agent scoped state homes in the instance-state store when the design
track lands it; until then the interim home is the wave 4 effort's call. State records carry their
schema version as an attribute, so upgrades migrate rather than orphan.

### Upstream prerequisites

A session integration checks its own upstream prerequisites during readiness (it knows its harness)
using persisted applied state and inexpensive probes, and reports gaps with remediation pointing at
the owning operation (`agw agent reinit ...`), through core's standard error framing. Gaps carry
severity: a required prerequisite fails the operation, a recommended one warns and permits degraded
operation. It never repairs upstream state from a session operation.

## Session and run identity

The identity model both waves build on, resolving the sharpest gap in `starting-state.md`:

- **`session_uuid`**: minted at session create, immutable, never reused. The operator-facing session
  name remains the human key and stays reusable; the uuid is what history keys on, so a
  delete-and-recreate under the same name can never splice histories.
- **`run_id`**: minted at each workload incarnation (create and every resume), unique within the
  session. The existing `boot_id` stays what it is today: VM reboot detection, not identity.
- Events and transcripts key on `(session_uuid, run_id)`. Session-scoped integration state keys on
  `session_uuid` alone and deliberately survives resume (a harness's minted conversation identity is
  exactly what a later run needs to decide resume versus launch); run-scoped state keys on
  `(session_uuid, run_id)`.
- Run boundaries are explicit events: a resume ends the previous run (closing unfinished interaction
  pairs as expired-by-run-end, never silently) and starts a new one. Liveness signals attach to a
  run; activity signals attach to observed events within a run. VM auto-suspend consumes activity,
  never liveness.
- The schema change is small and self-contained and may land early if convenient, per `phasing.md`;
  wave 5 owns it otherwise.

## Runtime observation and control (wave 5)

Observation and control are integration capabilities under the same trust model: observation
collects and fuses sources into the universal event stream, exposes interpreted current state, and
reports degradation and loss; control validates intents against that observed live state and MUST
NOT exist without the observation needed to validate and confirm its actions. Core performs tmux and
PTY operations on the integration's behalf as a mechanics split (core owns the terminal machinery),
not as a permission system.

## Open questions for the wave 4 and wave 5 seeds

- Init method signatures, the env and artifact currency schemas, and how env rides the run targets
  (wave 4).
- Whether a supported-scopes report exists for doctor and guide, and its mechanism (wave 4).
- The interim state home before the instance-state store lands (wave 4).
- The admin attachment's spelling on the vm-template (it validates against the same user-scope model
  as agent attachments) (wave 4).
- The retry contract for a partially created workspace with some artifacts already written (wave 4).
- Ordering and conflict reporting when multiple integrations attach at one broader scope (wave 4).
- The first slice of the universal event vocabulary and its source/origin/fidelity metadata (wave
  5).
- Where core intercepts terminal input without weakening direct PTY behavior (wave 5).
