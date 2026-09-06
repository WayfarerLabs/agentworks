# Harness Scope Framework: Functional Requirements

- Status: Active, architecture under review
- Date: 2026-09-06
- Saga: `docs/sdd/2026-08-04-next-steps/` (wave 4)
- Governing inputs: `scope-participation-contract.md` (the settled design), `target-state.md`
  section "Harness scopes (destination 4)", `capability-descriptor-contract.md`, and
  `message-2026-08-16-capability-config-shape.md`
- Ownership: the operator owns these requirements. The effort lead owns the HLA, plan, and LLDs and
  maintains explicitly authorized FRD revisions. The saga lead seeded this document.
- Artifact refinement: operator-authorized 2026-09-06; see the ruling below. The FRD amendment and
  corresponding HLA revision are reviewed together.

## Why this exists

Agentworks has five scopes (vm, admin, agent, workspace, session) and exactly one participant that
straddles them: the harness integration. Today that participant only exists at the session scope.
`HarnessIntegration` (`cli/agentworks/capabilities/harness_integration/base.py:202`) declares one
abstract _operation_, `start` (`base.py:308`), which is the only entry in the kind descriptor's
`required_operations` (`cli/agentworks/capabilities/harness_integration/kinds.py:119`). It declares
one other abstract method, `_probe_target` (`base.py:322`), which is internal machinery rather than
an operation; a subclass must still supply it. Everything a harness needs at a broader scope is
therefore either absent or smuggled in through a core-owned surface that names the harness directly.
The standing example is the pair of Claude-specific fields on admin config
(`cli/agentworks/vms/admin.py:142`) and agent templates (`cli/agentworks/agents/templates.py:42`):
core carries `claude_marketplaces` and `claude_plugins` because there is nowhere else to put them,
and installs them from a VM-init step (`cli/agentworks/vms/initializer/driver.py:741`).

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
(`agw vm reinit NAME`) while an agent has its own (`agw agent reinit NAME`), and the admin's
configuration is spelled on the admin template (`cli/agentworks/vms/admin.py:66`) while agent
configuration is spelled on agent templates. How the admin _attachment_ is spelled is open question
3 below, and this paragraph says nothing about it.

A **facet** is the level a capability is driven at: the pairing of one level's API methods and its
config, nothing more. Facets belong to capabilities. There are four: vm, user, workspace, session.

Core owns the mapping between them, and it is fixed:

| Scope     | Facet     | Init method      | Driven by                          |
| --------- | --------- | ---------------- | ---------------------------------- |
| vm        | vm        | `vm_init`        | VM init and reinit                 |
| admin     | user      | `user_init`      | VM init and reinit                 |
| agent     | user      | `user_init`      | Agent init and reinit              |
| workspace | workspace | `workspace_init` | Workspace create                   |
| session   | session   | `start`          | Session create, start, and restart |

Only `start` is a name that exists today. The three init-method names are indicative, exactly as R2
says; final naming is the effort lead's.

Admin and agent collapse into one facet because a harness does the same thing for both: set up a
user. They stay separate scopes because their lifecycles and owning resources differ. The
consequence to hold onto is that **an integration author never writes the word admin or agent**:
they implement `user_init` and read the invocation context to learn which user they were called for.
Core owns the scope mapping and lifecycle dispatch. Origin metadata may describe a scope without
making it an integration API choice: integration code uses one user method for either identity.

An artifact originates at a scope and retains that owning resource and producer throughout delivery.
A facet identifies the integration API invoked to handle it, not a new location where it originated.
The corresponding origin facet is derived from core's mapping rather than independently authored.
Actual resource relationships govern delivery: agent and workspace are separate ancestors of a
session, not scopes nested inside one another.

The practical test: if the question is _when_ something runs, it is a scope question and the answer
is core's. If the question is _what an integration implements or configures_, it is a facet question
and the answer is the capability's.

## Who this is for

- **Operators**, who select integrations on the template that owns each resource and attach
  configuration there, and who expect VM and agent reinit to converge and workspace creation to
  materialize its configured artifacts without accumulating drift.
- **Integration authors** (first-party today; external plugins are wave 8), who implement only the
  setup facets their harness needs and get no-op defaults that defer artifacts for the rest.
- **Core maintainers**, who need the harness-specific knowledge out of core surfaces.

## Functional requirements

**R1. A setup pipeline runs at every setup scope, in one fixed order.** Core setup runs first and
may emit env and agent artifacts. Features run next in template declaration order, each receiving
env-to-date (including env inherited from broader scopes, which core assembles and delivers) and
each able to emit env and agent artifacts alongside its own side effects. Enabled harness
integrations run last, receiving the completed env and local artifacts plus applicable inherited
artifacts still deferred for that integration, as specified in R7. Reinit reruns the same pipeline
idempotently. User setup selects `user-feature` capabilities: one kind serves both admin and agent
users, with the invocation context identifying the user.

**R2. The integration API carries one init method per facet.** `vm_init`, `user_init`, and
`workspace_init` (names indicative) join the existing session surface on the one registered
integration API. `user_init` is a single surface invoked for the admin user during VM init and
reinit, and for each agent during agent init and reinit; the invocation context says which user, and
one method body serves both.

**R3. Unimplemented setup facets are no-ops, not errors.** The base class provides no-op defaults
that defer every input artifact. An integration implements the setup facets it needs; the existing
session launch contract remains required. There is no setup failure merely because a facet defers an
artifact. R7's final session-facet enforcement still applies. This supersedes the originating
perspective's absence-means-unsupported rule: review and testing catch a mistyped override.

**R4. Integrations declare config per facet.** Config follows the same four facets the methods do,
for the same reason: a capability declares a fixed set of facet configs exactly as it declares a
fixed set of API methods, and consumers choose which facet they drive. Core asks through
`config_for`, which already exists (`cli/agentworks/capabilities/base.py:339`); what this effort
adds is the facet argument and a capability that offers more than one config. Producers never need
to know their consumers. A capability with a single config declares it without naming any facet, so
the ordinary case stays invisible. The association is introspectable at finalize, before any method
runs. Validation consumes exactly one facet's schema per blob; offering no config for a facet means
there is nothing to validate there.

**R5. Integration config is ordinary capability config.** It belongs to the consuming resource (the
vm, agent, workspace, or session template that selects the integration) and is validated the way all
capability config is validated: by core against capability-provided schema, one blob at a time as
the graph walk reaches each resource. Per-facet config is a harness-integration specialty, not a new
framework mechanism. Integrations must be explicitly selected on the resource, even when all config
uses defaults. A setup resource can select multiple integrations, each with its own config; an
available plugin or default config never implicitly attaches one. A session has one explicit
effective integration selection, including when it selects the generic shell.

**R6. Env and typed agent artifacts are the pipeline's two currencies.** Artifact shapes start with
hints plus a reduced Rulesync model of rules and skills. A hint is a small setup fact, such as the
existence of an env variable or that an authentication feature made a tool available. Hints provide
advisory context; a producer needing stronger guidance can emit a rule or skill instead. The name
describes the content, not optional delivery: hints follow R7 like other artifacts. A hint remains
separate from a rule, so the integration may collect such facts into a native rule or a session
launch prompt without manufacturing one rule per fact. A rule preserves its instructional content
and applicability, including always-applying and path-specific rules. A skill preserves its name,
discovery description, instructions, and supporting files as a package with discovery/invocation
semantics. Converting a skill into prompt text is not equivalent handling. Core and features may
emit both env and artifacts; the producer contract must permit a later manual template-artifact
surface without redesign, but that surface is not required now. Concrete schemas are the effort
lead's to settle within these requirements and R12.

**R7. Integrations defer what they cannot handle; core rejects final session deferral.** Native
placement at the defining scope is the ordinary case. Each facet invocation receives local artifacts
plus applicable inherited artifacts not already handled for that integration and resource path. The
integration decides representation and returns only the artifacts it defers, with a reason for each.
Omission from a successful deferred result means handling for that invocation; there is no separate
handled-item acknowledgment ledger. Failed, stale, or absent invocations do not imply successful
handling.

Deferral belongs to an integration and its applicable resource path. Claude handling a user skill
does not handle it for another integration or another user. A session receives accumulated env from
its ancestors and its own scope, but artifact payloads already handled upstream do not need to reach
its session-facet invocation. Core preserves originating scope, resource identity, and producer, and
combines applicable ancestor paths without delivering the same originating item twice. Different
artifacts are not duplicates merely because their text matches.

The session facet returns the same deferred collection as setup facets. Any remaining entry becomes
an immediate core error before launching the workload, preserving the integration's explanation and
artifact origin in standard error framing. Ordinary execution failures remain errors independently
of deferral. Core enforces this returned contract; integration code and testing establish that
omitted artifacts were actually handled. Scope discipline remains trust-based.

A session-specific representation must preserve the artifact's semantics and affect only that
workload. Writing session-only artifacts into shared user or workspace auto-discovery is not an
acceptable fallback. If no suitable representation exists, the integration defers and core refuses
launch. This is workload isolation, not secrecy: other sessions being able to read artifacts does
not itself violate the contract. Applied-state receipts remain available to readiness even when
handled payloads are filtered out.

**R8. Per-scope invocations are constructed for their owning resource.** An invocation never reuses
a session instance's target identity, readiness cache, or state namespace; those stay session-bound.

**R9. Applied state is recorded so reinit converges and drift is reported.** Per-(owning resource,
integration) state records what was applied (destinations, strategies, hashes). Records carry their
schema version. Successful contributions and integration-specific deferred outputs also survive
between lifecycle operations, so session creation does not rerun ancestor setup to reconstruct
inputs. A changed input or incomplete operation must not reuse old deferral output as proof of
handling. Secrets never enter persisted state or resolved configuration.

**R10. Upstream prerequisites are reported, never repaired from a session operation.** A session
integration checks its own upstream prerequisites during readiness using persisted applied state and
inexpensive probes, and reports gaps through core's standard error framing with remediation pointing
at the owning operation (`agw agent reinit NAME`). Gaps carry severity: a required prerequisite
fails the operation, a recommended one warns and permits degraded operation. A prerequisite may
require or recommend successful setup of a particular facet for its bound resource. In particular, a
session integration can require its user facet for the session's actual user; setup for another user
cannot satisfy that prerequisite. A facet with no prerequisite remains optional. These are the
consuming integration's readiness requirements, not global required/optional flags on facet schemas.

**R11. The Claude-specific template fields migrate into the Claude integration's config.**
`claude_marketplaces` and `claude_plugins` leave the VM admin config and agent templates and become
user-facet config on the Claude integration. This is the acceptance test for the whole effort: if
core still carries harness-specific fields or setup dispatch after this lands, the framework did not
do its job. The generic shell integration remains available through explicit selection; core must
not silently substitute it when the effective session integration selection is absent.

**R12. The artifact schema must not foreclose wave 6.** Hint, rule, and skill shapes are concrete
now, with origin attribution and a producer-local identity that survives delivery. Global stable
identity, attributed composition, and limited typed hooks remain wave 6 work. Hooks must be able to
join later as a distinct artifact kind with explicit event and execution semantics, without
flattening artifacts to strings or rebuilding their delivery protocol. MCP server configurations are
another future artifact kind; their structured connection/configuration semantics must likewise fit
this delivery model. This effort does not implement MCP configuration delivery, hook execution,
global identity, or composition.

**R13. One vertical integration proves create and reinit end to end**, including grouped setup
hints, native rules, and complete skill packages at user and workspace scopes, workspace create-time
materialization, downstream filtering of handled payloads, and terminal refusal for an
unrepresentable artifact. The framework must not land with only unit-level evidence.

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

## Operator ruling: artifact delivery, 2026-09-06

The operator approved the reduced rules/skills model, native placement with integration-owned
deferral, shared deferral results with core enforcement at the session facet, and immutable origin
metadata. The operator then clarified that small hints describing Agentworks setup remain a separate
artifact kind, available for integration-owned grouping into a rule or launch prompt. The name
"hints" distinguishes these advisory setup facts from broader harness instructions; producers can
emit rules or skills for stronger guidance. MCP server configurations are explicitly recognized as a
future artifact kind alongside limited hooks. The operator then explicitly authorized updating this
FRD and publishing it together with the corresponding HLA revision in the existing draft review PR.
R1, R3, R6, R7, R9, R12, and R13 express that refinement; scope/facet terminology and R11's generic
shell integration are clarified alongside it.

This ruling supersedes the saga scope-participation contract and target-state wording that every
session receives all artifact payloads. Their other constraints still apply. The saga lead owns
reconciling those shared artifacts; this effort does not edit them. Cross-feature dependency
declarations remain deferred pending a separate decision.

## Operator ruling: user-feature naming, 2026-09-06

> user-features is for sure the right name. We don't want different capability kinds for agent vs
> admin.

R1 records `user-feature` as the single capability kind for user setup.

## Operator ruling: explicit integration enablement, 2026-09-06

> harness integrations must be explicitly enabled, even if the config is pure default.

R5 requires explicit resource selection. R11 retains the generic shell as a selectable integration
and removes its implicit runtime fallback.

## What changed since the scope-participation contract was written

The contract is dated 2026-08-05. Three of its statements are stale against `main`, and a fourth
correction below belongs to a sibling artifact rather than to the contract. Build on the code rather
than on any of their snapshots. Item 4 matters most, so read it first: **part of what the contract
describes as future work has already shipped.** The contract itself now carries these corrections
inline.

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
3. **The kind's contract version is 3.** The scope-participation contract is silent on this; the
   claim being corrected is in a sibling artifact,
   `docs/sdd/2026-08-31-session-console-lifecycle/locked.md`, which states the harness-integration
   contract "remains version 1". That artifact is locked and belongs to another effort, so it is
   flagged here rather than edited. `kinds.py:117` reads `contract_version=3`, matching all four
   in-tree integrations. Adding per-scope methods changes the contract and needs a version bump;
   whether the new methods join `required_operations` (today `frozenset({"start"})`, `kinds.py:119`)
   is an R3 question, and the no-op-default rule argues they should not. Note that these versions
   are internal: every implementation is first-party, so the bump is a mechanical sweep, not an
   ecosystem event.
4. **The facet vocabulary and `config_for` are already in the code, so do not reintroduce them.**
   `Capability.config_for` is a shipped classmethod (`cli/agentworks/capabilities/base.py:339`)
   whose docstring already defines a facet as the level a capability is driven at (vm, user,
   workspace, session), already states that facets are deliberately not scopes and that core owns
   the mapping, and already records why the signature takes no facet argument: because no capability
   offers more than one config yet. `cli/agentworks/capabilities/README.md` carries the same
   contract, including the admin-and-agent-to-user mapping. This effort therefore extends a declared
   hook rather than inventing one, and the `config_at(level)` spelling sketched in
   `message-2026-08-16-capability-config-shape.md` is superseded by the shipped name. That message
   is still worth reading for its other three contributions; it is not the authority on the hook's
   name.

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
- Global artifact identity, attributed composition, and hook execution (wave 6), beyond R12's
  obligation not to foreclose them. The hint/rule/skill shapes and origin tracking in R6/R7 are in
  scope.
- MCP server configuration delivery, beyond R12's obligation not to foreclose it.
- A manually authored template-artifact surface, while preserving its immediate follow-on path.
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
behaves exactly as it does today when no artifact inputs are supplied. With artifact inputs,
completion also requires faithful hint/rule/skill representation or a final deferral error, with no
session payload duplication or widening of session-only applicability.
