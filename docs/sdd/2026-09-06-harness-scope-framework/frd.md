# Harness Scope Framework: Functional Requirements

- Status: Seed, awaiting an effort lead
- Date: 2026-09-06
- Saga: `docs/sdd/2026-08-04-next-steps/` (wave 4)
- Governing inputs: `scope-participation-contract.md` (the settled design), `target-state.md`
  section "Harness scopes (destination 4)", `capability-descriptor-contract.md`, and
  `message-2026-08-16-capability-config-shape.md`
- Authorship: seeded by the saga lead per the `saga-lead` skill. The effort lead owns the HLA, plan,
  and LLDs, and owns this document's revisions once the effort is picked up

## Why this exists

Agentworks has five scopes (vm, admin, agent, workspace, session) and exactly one participant that
straddles them: the harness integration. Today that participant only exists at the session scope.
`HarnessIntegration` (`cli/agentworks/capabilities/harness_integration/base.py:202`) declares one
abstract operation, `start` (`base.py:307`), and the kind descriptor requires only that operation
(`cli/agentworks/capabilities/harness_integration/kinds.py:119`). Everything a harness needs at a
broader scope is therefore either absent or smuggled in through a core-owned surface that names the
harness directly. The standing example is the pair of Claude-specific fields on VM and agent
templates: core carries `claude_marketplaces` and `claude_plugins` because there is nowhere else to
put them.

The result is that harness knowledge leaks into core at every scope except the one where the
framework exists. This effort makes the scope-participation contract real so that harness-specific
behavior lives in the harness integration at every level it operates, and core owns only the
pipeline that invokes it.

## Scopes and facets

These two words are not synonyms and the difference is load-bearing, so it is defined here once
before any requirement uses it.

A **scope** is where a lifecycle lives. Scopes belong to core. There are five, and the set is fixed:
vm, admin, agent, workspace, session. Each names a resource core sets up and the operation that sets
it up, which is why admin and agent are separate: admin identity work rides the VM lifecycle
(`agw vm reinit NAME`) while an agent has its own (`agw agent reinit NAME`), and the admin
attachment is spelled on the vm-template while agent attachments are spelled on agent templates.

A **facet** is the level a capability is driven at: the pairing of one level's API methods and its
config, nothing more. Facets belong to capabilities. There are four: vm, user, workspace, session.

Core owns the mapping between them, and it is fixed:

| Scope     | Facet     | Init method      | Driven by                 |
| --------- | --------- | ---------------- | ------------------------- |
| vm        | vm        | `vm_init`        | VM init and reinit        |
| admin     | user      | `user_init`      | VM init and reinit        |
| agent     | user      | `user_init`      | Agent init and reinit     |
| workspace | workspace | `workspace_init` | Workspace create          |
| session   | session   | `start`          | Session start and restart |

Admin and agent collapse into one facet because a harness does the same thing for both: set up a
user. They stay separate scopes because their lifecycles and owning resources differ. The
consequence to hold onto is that **an integration author never writes the word admin or agent**:
they implement `user_init` and read the invocation context to learn which user they were called for.
Only core knows a scope.

The practical test: if the question is _when_ something runs, it is a scope question and the answer
is core's. If the question is _what an integration implements or configures_, it is a facet question
and the answer is the capability's.

## Who this is for

- **Operators**, who select integrations on the template that owns each resource and attach
  configuration there, and who expect VM, agent, and workspace setup to converge on reinit rather
  than accumulate drift.
- **Integration authors** (first-party today; external plugins are wave 8), who implement only the
  scopes their harness needs and get no-op defaults for the rest.
- **Core maintainers**, who need the harness-specific knowledge out of core surfaces.

## Functional requirements

**R1. A setup pipeline runs at every setup scope, in one fixed order.** Core setup runs first and
may emit env and agent artifacts. Features run next in template declaration order, each receiving
env-to-date (including env inherited from broader scopes, which core assembles and delivers) and
each able to emit env and agent artifacts alongside its own side effects. Enabled harness
integrations run last, receiving all env and agent artifacts for the scope. Reinit reruns the same
pipeline idempotently.

**R2. The integration API carries one init method per facet.** `vm_init`, `user_init`, and
`workspace_init` (names indicative) join the existing session surface on the one registered
integration API. `user_init` is a single surface invoked for the admin user during VM init and
reinit, and for each agent during agent init and reinit; the invocation context says which user, and
one method body serves both.

**R3. Unimplemented scopes are no-ops, not errors.** The base class provides no-op defaults and an
integration implements only what it supports. This supersedes the originating perspective's
absence-means-unsupported rule: review and testing catch a mistyped override.

**R4. Integrations declare config per facet.** Config follows the same four facets the methods do,
for the same reason: a capability declares a fixed set of facet configs exactly as it declares a
fixed set of API methods, and consumers choose which facet they drive. Core asks `config_for(facet)`
(name indicative), so producers never need to know their consumers. A capability with a single
config declares it without naming any facet, so the ordinary case stays invisible. The association
is introspectable at finalize, before any method runs. Validation consumes exactly one facet's
schema per blob; offering no config for a facet means there is nothing to validate there.

**R5. Integration config is ordinary capability config.** It belongs to the consuming resource (the
vm, agent, workspace, or session template that selects the integration) and is validated the way all
capability config is validated: by core against capability-provided schema, one blob at a time as
the graph walk reaches each resource. Per-facet config is a harness-integration specialty, not a new
framework mechanism.

**R6. Env and agent artifacts are the pipeline's two currencies.** Their schemas are this effort's
to settle, subject to R12.

**R7. Sessions receive everything, and the integration owns representation.** A session receives env
and agent artifacts from all ancestor scopes plus its own. Content that cannot be represented at its
own scope is hoisted into the session, and the integration decides placement using its harness
knowledge, including deduplication and double-provisioning avoidance. Hoisting is isolation, not
security: other sessions being able to see user-scope artifacts is expected, and the integration's
obligation is that hoisted material only takes effect for its own workload.

**R8. Per-scope invocations are constructed for their owning resource.** An invocation never reuses
a session instance's target identity, readiness cache, or state namespace; those stay session-bound.

**R9. Applied state is recorded so reinit converges and drift is reported.** Per-(owning resource,
integration) state records what was applied (destinations, strategies, hashes). Records carry their
schema version. Secrets never enter persisted state or resolved configuration.

**R10. Upstream prerequisites are reported, never repaired from a session operation.** A session
integration checks its own upstream prerequisites during readiness using persisted applied state and
inexpensive probes, and reports gaps through core's standard error framing with remediation pointing
at the owning operation (`agw agent reinit NAME`). Gaps carry severity: a required prerequisite
fails the operation, a recommended one warns and permits degraded operation.

**R11. The Claude-specific template fields migrate into the Claude integration's config.**
`claude_marketplaces` and `claude_plugins` leave the VM admin config and agent templates and become
user-facet config on the Claude integration. This is the acceptance test for the whole effort: if
core still names a harness after this lands, the framework did not do its job.

**R12. The artifact schema must not foreclose wave 6.** Stable identity, attributed composition, and
typed hooks belong to wave 6's artifact model; this effort's schema must leave room for them without
implementing them.

**R13. One vertical integration proves create and reinit end to end**, including workspace
create-time materialization, rather than the framework landing with only unit-level evidence.

## Settled constraints, not to be reopened

These are recorded rulings. A child SDD builds on them; changing one is an operator decision routed
through the saga lead.

- **The five scopes are fixed.** There is no scope registry and no extensibility mechanism. If the
  set ever changes, that is an ordinary contract change, not a framework event.
- **Admin-scope setup rides the VM lifecycle.** Admin identity work happens during VM init and
  reinit, surfaced through the same user-level method as agents.
- **Cross-feature dependency declaration is deferred.** Template declaration order is the ordering
  mechanism until a real case demands more.
- **Features are harness-agnostic by definition.** Anything harness-specific is harness-integration
  config, never a feature.
- **There is deliberately no session-feature.** One gets added only if real pressure emerges.
- **Scope discipline is trust-based.** Core does not and cannot enforce that an integration stays in
  its lane; code review and testing gate system plugins, and wave 8's distribution-trust model gates
  external ones. Do not build enforcement theater here. This is a deliberate boundary on the
  anchored-projections principle: projections govern surfaces where enforcement is real, and trusted
  in-process integration code is governed by trust, review, and disclosure.
- **Artifact conduct is conventional and review-enforced:** claim the smallest practical ownership
  unit; never silently adopt or overwrite repository, operator, or generator-owned content; record
  applied state so reinit converges; secrets never enter persisted state.
- **Rulesync informs the artifact design but is not a runtime dependency.**

## What changed since the scope-participation contract was written

The contract is dated 2026-08-05. Three of its statements are now stale against `main` at
`f1937456`, and the effort lead should build on the code, not on the contract's snapshot.

1. **The instance-state store landed, so the interim-state-home question is closed.** The contract
   left the pre-store home to this effort's judgement. It no longer needs one:
   `cli/agentworks/db/instance_state.py` ships a typed store whose applied-state surface is
   `replace_applied_slices` (`:529`), `clear_applied_slice` (`:575`), and `inspect_owner_state`
   (`:593`). `AppliedStateKey` (`:37`) is a closed enum, and `_APPLIED_KEYS_BY_KIND` (`:62`) already
   carries `agent`, `workspace`, and `session` as kinds owning no keys yet. Wave 4 is the first
   consumer to register keys for them, and does so without touching the table. The saga lead's
   2026-08-24 forward-compatibility ruling governs unknown keys: a well-formed key this release does
   not understand is data, not corruption, and reads skip it while partial replacement preserves it.
2. **Session start and resume are one method, not two.** The contract says the per-scope methods sit
   "alongside the existing session `start` and `resume`". At HEAD there is one abstract `start`, and
   resume is expressed by `HarnessLaunchIntent` (`base.py:71`), whose members are `CREATE`,
   `RESUME_ONLY`, `RESUME_OR_NEW`, and `FORCE_NEW`, with a `starts_fresh` property. The session
   facet is therefore one method taking an intent.
3. **The harness-integration contract is at version 3, not 1.** `kinds.py:117` reads
   `contract_version=3`, matching all four in-tree integrations. Adding per-scope methods changes
   the contract and needs a version bump; whether the new methods join `required_operations` (today
   `frozenset({"start"})`, `kinds.py:119`) is an R3 question, and the no-op-default rule argues they
   should not. Note that these versions are internal: every implementation is first-party, so the
   bump is a mechanical sweep, not an ecosystem event.

## Open questions this effort owns

Carried forward from the contract, minus the one that closed:

1. Init method signatures, the env and artifact currency schemas, and how env rides the run targets.
2. Whether a supported-scopes report exists for doctor and guide output, and its mechanism.
3. The admin attachment's spelling on the vm-template (it validates against the same user-scope
   model as agent attachments).
4. The retry contract for a partially created workspace with some artifacts already written.
5. Ordering and conflict reporting when multiple integrations attach at one broader scope.

The capability-API reevaluation is chartered into this effort rather than scheduled separately. The
seed material in `message-2026-08-16-capability-config-shape.md` carries the `config_at(level)`
shape sketch, the three preserved `base.py` constraints, the who-constructs-versus-who-calls-in
caution, and the pre-design call-site discovery walk.

## Out of scope

- The universal event vocabulary, session and run identity plumbing, PTY observation, and anything
  else on the observability track (wave 5). Note that `scope-participation-contract.md` settles the
  identity model for both waves; this effort consumes it only if a requirement here needs it.
- The artifact composition and hook model (wave 6), beyond R12's obligation not to foreclose it.
- External plugin distribution and its trust model (wave 8).
- Harness integration config knobs for per-session workload inputs (issue #674), which shipped ahead
  of this charter by operator ruling on 2026-08-26 and deliberately without an SDD. That work does
  not establish this effort's scope, and knobs landing early is not a precedent for treating the
  pipeline, per-scope init methods, attachments, applied state, or the vertical integration as
  settled.

## Definition of done

The effort is functionally complete when a harness integration can participate at vm, user, and
workspace scope through its own API and its own config; when core carries no harness-specific field
at any scope (R11 discharged); when reinit at each setup scope converges rather than accumulating,
proven by the vertical integration; and when an integration that implements nothing beyond `start`
behaves exactly as it does today.
