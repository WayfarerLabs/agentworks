# Harness Scope Framework: Functional Requirements

- Status: Active, architecture approved; implementation authorized
- Date: 2026-09-06
- Saga: `docs/sdd/2026-08-04-next-steps/` (wave 4)
- Governing inputs: `scope-participation-contract.md` (the settled design), `target-state.md`
  section "Harness scopes (destination 4)", `capability-descriptor-contract.md`, and
  `message-2026-08-16-capability-config-shape.md`
- Ownership: the operator owns these requirements. The effort lead owns the HLA, plan, and LLDs and
  maintains explicitly authorized FRD revisions. The saga lead seeded this document.
- Current scope: facets and native harness setup only, by the latest operator ruling on 2026-09-07.
  All artifact functionality is deferred until facets ship. The authorized FRD amendment and
  corresponding HLA revision are reviewed together; earlier artifact rulings are historical context.

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
Core owns the scope mapping and lifecycle dispatch. Resource context identifies the owning scope
without making it an integration API choice: integration code uses one user method for either
identity. Actual resource relationships govern env inheritance and prerequisites: agent and
workspace are separate ancestors of a session, not scopes nested inside one another.

The practical test: if the question is _when_ something runs, it is a scope question and the answer
is core's. If the question is _what an integration implements or configures_, it is a facet question
and the answer is the capability's.

## Who this is for

- **Operators**, who select integrations on the template that owns each resource and attach
  configuration there, and who expect VM and agent reinit to converge and workspace creation to
  apply its native harness settings without accumulating drift.
- **Integration authors** (first-party today; external plugins are wave 8), who implement only the
  setup facets their harness needs and get no-op defaults for the rest.
- **Core maintainers**, who need the harness-specific knowledge out of core surfaces.

## Functional requirements

**R1. A setup pipeline runs at every setup scope, in one fixed order.** Core setup runs first,
assembling the owning resource's env with applicable inherited env. Explicitly enabled harness
integrations run next, receiving that env in the owning resource's invocation context. This applies
to all five scopes and all four facets, including the VM facet. Reinit reruns the same pipeline
idempotently. The current model is core env, then harness integrations. Features and artifact
functionality are future work; this effort introduces no placeholder APIs for them.

**R2. The integration API carries one init method per facet.** `vm_init`, `user_init`, and
`workspace_init` (names indicative) join the existing session surface on the one registered
integration API. `user_init` is a single surface invoked for the admin user during VM init and
reinit, and for each agent during agent init and reinit; the invocation context says which user, and
one method body serves both.

**R3. Unimplemented setup facets are no-ops, not errors.** The base class provides no-op defaults.
An integration implements the setup facets it needs; the existing session launch contract remains
required. The shell integration keeps its ordinary session behavior and uses no-op setup defaults.
This supersedes the originating perspective's absence-means-unsupported rule: review and testing
catch a mistyped override. R10's explicit consuming prerequisites still apply.

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

**R6. Env is the pipeline's shared input.** Core assembles env for each owning resource and passes
it to the harness invocation using the existing env precedence and reserved-variable conventions.
Integrations consume the completed env without duplicating core's composition rules. User and
workspace env follow the actual resource relationships when a session is assembled. This requirement
does not introduce an artifact currency, `artifacts` block, artifact producer, or artifact payload
storage.

**R7. Deferred: artifact propagation and deferral.** The earlier artifact-delivery requirement is
reserved for the follow-on effort. It imposes no current invocation fields, return values,
propagation machinery, final-deferral checks, or acceptance gate. Historical origin and placement
rulings below remain context for that future design.

**R8. Per-scope invocations are constructed for their owning resource.** An invocation never reuses
a session instance's target identity, readiness cache, or state namespace; those stay session-bound.

**R9. Applied state is recorded so reinit converges and drift is reported.** The existing
instance-state facility records versioned metadata receipts per owning resource and integration:
what native setup was applied, the relevant destinations, selected strategies, ownership, and
comparison hashes. Readiness consumes these receipts rather than rerunning ancestor setup. Changed
config or an incomplete operation must not reuse stale receipts as proof of current successful
setup. State contains metadata, not raw settings, env values, or artifact contents; secrets never
enter persisted state or resolved configuration. Existing workflow export/restore handling must
preserve these typed receipt semantics; this adds no VM restore workflow or persisted producer
model.

The same facility drives idempotent cleanup when provisioned plugins, other native setup entries, or
whole attachments are removed. At the owning reconciliation or deletion operation, compare current
desired state with recorded applied ownership and remove obsolete owned resources wherever the
native mechanism permits safe removal. Repeated cleanup must converge, including when the resource
is already gone. This is not a promise to reverse every side effect: intentional retention policies,
including R15's treatment of settings, remain explicit. Unsupported or unsafe removal reports what
remains with useful evidence. Do not discard required cleanup records or claim success before the
disposition is known.

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

**R12. Deferred: artifact kinds, formats, and composition.** Hints, rules, skills, subagent
definitions, and later limited hooks and MCP configuration belong to the future artifact effort.
This effort commits no artifact schema, normalized or wire format, acquisition mechanism, or runtime
dependency. Rulesync reuse is evaluated with that future design, not implemented here.

**R13. One vertical integration proves create and reinit end to end** through the real CLI, using
ordinary local native plugin, settings, and env fixtures. Coverage proves the five scopes' mapping
to four facets, including the VM facet and shared user facet; explicit attachment and per-facet
config; env delivery; workspace create-time settings; required and recommended upstream
prerequisites for the actual user; and ownership-safe repeated setup and cleanup. Claude Code and
Codex user marketplace/plugin setup and all four native settings policies are demonstrated without
external service dependencies. The vertical does not introduce features or an artifact pipeline, and
the framework must not land with only unit-level evidence.

**R14. Native harness setup config applies at its defining resource.** Claude Code and Codex both
offer user marketplace/plugin configuration and user/workspace native settings mappings. A shared
schema does not combine user and workspace declarations or copy one into the other. Native harness
rules determine how separately provisioned layers compose at launch.

Workspace plugin installation is deferred from this effort. A later implementation may offer it only
with a native mechanism that preserves applicability to the originating workspace, including when
installation needs the eventual session user's identity. A user-global installation cannot satisfy
that promise, and a project declaration alone is not proof of usable plugins. Physical package/cache
location and activation scope are separate facts. This effort does not add a native deferred-setup
protocol to support that optional follow-on.

**R15. Harness integrations can provision native settings from workstation files.** User and
workspace attachments for Claude Code and Codex accept an explicit source file and a destination
settings role owned by that facet. The operator can choose complete replacement, merging with source
keys winning, merging with destination keys winning, or leaving the entire destination untouched if
it already exists. The integration owns native format parsing, scope-valid destinations, and
application; core supplies the source-path and file-transfer facilities. A source names a file on
the workstation running the owning setup operation, not an implicit path on the guest. Setup copies
or merges a snapshot; it is not a live mount and session start never rereads workstation files.

The selected policy is explicit authorization for its stated treatment of existing settings at that
destination. It does not grant ownership of other files or override another integration's recorded
claim. Reinit applies the same policy to current source and destination; removal must never silently
delete pre-existing settings. Missing/unreadable sources or invalid inputs needed by the selected
policy produce an owning setup error before this integration changes native settings or plugins.
Mapping and receipts follow the existing no-persisted-secrets contract; raw workstation settings are
not stored in resolved config or applied-state payloads. The HLA must settle nested-key/array
behavior, interaction with explicit plugin config, and cleanup ownership.

**R16. Deferred: shell artifact filesystem delivery.** No artifact directories, workload artifact
index, discovery variable, publication or cleanup mechanism, or Git exclusions are introduced for
shell by this effort. Its setup facets remain no-ops under R3. The early `session_uuid` persistence
and invocation-context slice was justified only by artifact ownership and is no longer required or
delivered here. The saga coordinates ownership of that identity work with sibling efforts.

## Settled constraints, not to be reopened

These are recorded rulings. A child SDD builds on them; changing one is an operator decision routed
through the saga lead.

- **The five scopes are fixed.** There is no scope registry and no extensibility mechanism. If the
  set ever changes, that is an ordinary contract change, not a framework event.
- **Admin-scope setup rides the VM lifecycle.** Admin identity work happens during VM init and
  reinit, surfaced through the same user-level method as agents.
- **Features remain future functionality.** Their future position is between core and harness
  integrations. No feature capability or ordering mechanism is implemented by this effort, including
  test features. The earlier declaration-order guidance applies when feature execution is added;
  cross-feature dependency declaration remains deferred until a real case demands more.
- **Future features are harness-agnostic by definition.** Anything harness-specific is
  harness-integration config, never a feature. Future user setup uses one `user-feature` kind for
  both admin and agent users.
- **There is deliberately no session-feature.** One gets added only if real pressure emerges.
- **Scope discipline is trust-based.** Core does not and cannot enforce that an integration stays in
  its lane; code review and testing gate system plugins, and wave 8's distribution-trust model gates
  external ones. Do not build enforcement theater here. This is a deliberate boundary on the
  anchored-projections principle: projections govern surfaces where enforcement is real, and trusted
  in-process integration code is governed by trust, review, and disclosure.
- **Native setup ownership is conventional and review-enforced:** claim the smallest practical
  ownership unit; never silently adopt or overwrite repository, operator, or generator-owned
  content. R15 authorizes only the explicitly selected settings destination and policy; record
  applied state so reinit converges; secrets never enter persisted state.
- **Artifacts are entirely deferred.** No placeholder artifact APIs, formats, storage, or
  installation mechanisms land with facets. Rulesync reuse is a question for the future artifact
  effort.

## Operator ruling: artifact delivery, 2026-09-06

**Historical; superseded for current scope by the facets-first ruling below.** Artifact delivery and
its requirement references in this record apply to the earlier design, not the current effort.

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

This naming ruling remains applicable to future user setup: `user-feature` is the single capability
kind for both admin and agent users. The 2026-09-07 scope ruling below supersedes its earlier
implementation timing; no feature capability kind is introduced in this effort.

## Operator ruling: explicit integration enablement, 2026-09-06

> harness integrations must be explicitly enabled, even if the config is pure default.

R5 requires explicit resource selection. R11 retains the generic shell as a selectable integration
and removes its implicit runtime fallback.

## Operator refinement: native setup and workstation settings, 2026-09-06

> both CC and Codex have a notion of workspace plugins ("project" scope). WE should include those
>
> user marketplace/plugins are installed at user scope vs workspace ones that are installed at
> project scope
>
> One thing that I really want here is the ability to map settings files from the workstations.

The operator then clarified:

> IF those still end up scoped to a user, then we simply can't offer them. It would be a lie.
>
> Or maybe that would be a deferral case? The session could then install the plugin for the user in
> the given workspace, right?

The operator subsequently emphasized that workspace plugins are optional: "either we do it right or
we don't do it at all" and "workspace plugins are not something to lose sleep over". R14 retains the
applicability requirement for follow-on work and leaves workspace plugin installation out of this
effort. Codex user marketplace/plugin setup remains included. R15 adds workstation file mapping with
the four requested treatments of existing settings. The HLA proposes concrete policy names and merge
semantics. These native setup inputs remain in the current effort. The accompanying historical
request for a diagram of artifact deferral now belongs to the future artifact effort; the current
flow diagram shows core env entering the owning harness invocations.

## Operator ruling: shell filesystem delivery, 2026-09-07

**Historical; shell artifact functionality is deferred by the facets-first ruling below.** The
idempotent-cleanup direction remains active for native plugin/settings setup under current R9.

The operator approved shell handling artifacts as files in appropriate resource locations and
selected `~/.agentworks-artifacts/session/<session_name>/` in the actual user's home for
session-specific material. The operator also agreed to user-only access, explicit workload
discovery, ownership tied to session identity rather than name alone, and lifecycle cleanup. R16
records this concrete shell contract. R7 still rejects any remaining final deferral; shell now has a
representation for the initial artifact kinds. The operator further clarified that the existing
instance-state facility must drive idempotent cleanup of previously provisioned integration
resources, including artifacts and plugins, wherever safe removal is possible; R9 records that
requirement without promising reversal of every side effect. These changes were held for the next
full feedback round at the operator's request.

## Operator ruling: core artifact declarations, 2026-09-07

**Historical; the required artifacts block is superseded for current scope by the facets-first
ruling below.** Feature implementation remains deferred.

> I don't really want to drag features into this effort. We should mention them as future
> functionality but ideally we can keep them out.
>
> But we need an `artifacts` block just like we have an `env` block. This seems like the bare
> minimum that we have to do here. Technically this is part of the core.
>
> And so we start with a core (env and artifacts) -> harness integration model. Later features just
> slot between the two. And then later we add hint emission to other core stuff but that's outside
> this scope, too.

This ruling replaces feature implementation and test feature capabilities with declarative core
artifact inputs at all five owning scopes. R1, R6, and R13 carry the resulting producer, packaging,
and acceptance requirements. The operator also requested Unix line-ending normalization. Feature
execution and automatic hint emission from other core setup remain future work. Earlier rulings
about feature naming and the eventual pipeline position remain design guidance, not implementation
requirements for this effort. This supersedes any feature implementation requirement in the saga
scope-participation contract or target-state; reconciling those shared artifacts belongs to the saga
lead. This effort continues to own only its explicitly authorized FRD and HLA changes.

## Operator refinement: artifact acquisition, 2026-09-07

**Historical; all artifact acquisition is deferred by the facets-first ruling below.** The source
choices and normalization intent are future design context, not current acceptance requirements.

The operator requested support for workstation filesystem and Git skill references, with room for
future packaged distributions. Acquisition belongs at the input edge; the pipeline must have one
normalized format regardless of where an artifact came from. R6 includes local and Git acquisition
now, with a shared validation, normalization, and snapshot path and retained source provenance. Git
revisions resolve to immutable commits before integrations run, and later consumers use captured
contents. Packaged distributions remain a future acquisition extension, without a package manager or
registry in this effort. This supersedes the earlier local-first proposal that deferred direct Git
acquisition. Git-backed native settings are a separate future requirement; R15 continues to accept
workstation settings files.

## Operator ruling: facets first, artifacts deferred, 2026-09-07

The operator selected the option to defer all artifact functionality until facets ship. This is the
current scope ruling and supersedes earlier requirements to deliver artifact declarations, kinds,
acquisition, normalization, propagation, deferral, snapshots, installation, or shell artifact files
in this effort. R7, R12, and R16 keep their identifiers as deferred requirements. Core env feeds
harness integrations; features remain deferred. No placeholder artifact APIs are included.

The retained work is the five-scope/four-facet integration framework, including VM participation,
explicit enablement, per-facet config, resource-bound invocations and prerequisites, native user
marketplaces/plugins, workstation native settings mappings, and ownership-aware idempotent cleanup.
The early session identity slice is no longer part of this effort; the saga coordinates sibling
ownership. Shared saga artifacts are not edited here.

### Future artifact context, not this effort's contract

The operator selected a separate artifact SDD as the intended next effort after this framework,
rather than a later phase inside this SDD. Deliver and validate facets and native setup first, then
design artifact details against that foundation. The broader artifact-capable integration model
remains unfinished until the successor delivers it; completing this SDD does not claim otherwise.
The saga owns recording that sequence and establishing the successor's charter. The successor may
extend the facet interfaces; this SDD does not freeze an artifact-ready API in advance.

The follow-on includes hints, rules, skills, and subagent definitions, followed by limited hooks and
MCP configuration. Evaluate Rulesync's canonical formats and generation machinery for reuse then;
Agentworks still owns resource scopes, facet dispatch, applicability, and provisioned-resource
ownership. This does not select a dependency, format, or implementation now.

Retain the design questions and prior direction about immutable origin, native placement at the
defining scope, integration-owned deferral, and final session handling. Future producers run before
harness integrations, with features between core and integrations when feature execution is added.
Workstation, Git, and potentially packaged acquisition should converge at the input edge on one
normalized representation with source provenance. Text normalization, complete skill packages,
subagent semantics, stable captured contents, and safe lifecycle cleanup are matters for that
follow-on design and its acceptance criteria. No acquisition, format, storage, or delivery mechanism
is committed by this facets effort.

The operator's leading proposal is a future declarative `artifact-bundle` resource kind that owns
artifact ingestion and gives each bundle an ID for consuming resources to reference. A bundle's
source identity is distinct from each consumer's scope and applicability. The kind, its schema, and
revision, update, and storage semantics remain future design decisions; this effort introduces no
bundle registration or API.

The operator's routing direction for the successor is that each producing integration facet chooses
where its deferrals should next be handled. A VM facet can route to user, workspace, or directly to
session, skipping intermediate facets. It makes that decision from its own inputs and concerns,
without inspecting downstream instances or whether their integrations are enabled. Core preserves
and delivers the result; it does not choose a harness-specific preferred destination.

Each owning resource calculates its result independently for its current inputs. Later consumers
reuse that result without consuming it globally: one VM can serve many users and workspaces, and one
user can serve many sessions. Changed owning inputs can require recalculation through the owning
lifecycle; creating or enabling a descendant must not require the producer to reconsider its route.
A user invocation processes applicable VM deferrals without consulting workspaces or sessions. The
session combines direct VM-to-session input with applicable user and workspace results and accounts
for anything it cannot handle. The operator reopened the earlier mandatory-error policy: the
successor must settle when an unresolved item blocks launch or receives another explicit treatment.

The successor must still specify core handling of skipped intermediate integrations without silent
loss or implicit activation, distinguish origin from destination-specific applicability, and avoid
duplicate effects when paths meet. Handling for one user or workspace cannot discharge another's
obligations. These principles guide that SDD, not a deferral protocol delivered here. The workspace
facet remains in this SDD for native project settings; retaining it does not require VM artifacts to
route through it.

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
   consumer to register the keys its native setup requires, without touching the table. The saga
   lead's 2026-08-24 forward-compatibility ruling governs unknown keys: a well-formed key this
   release does not understand is data, not corruption, and reads skip it while partial replacement
   preserves it.
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

Carried forward from the contract, minus the questions that closed:

1. Init method signatures and how env rides the run targets. R6 settles env as the shared input;
   concrete invocation details belong to the LLD.
2. Whether a supported-scopes report exists for doctor and guide output, and its mechanism.
3. The admin attachment's spelling on the vm-template (it validates against the same user-scope
   model as agent attachments).
4. The retry contract for a partially created workspace with native setup already applied.
5. Ordering and conflict reporting when multiple integrations attach at one broader scope.

The capability-API reevaluation is chartered into this effort rather than scheduled separately. The
seed material in `message-2026-08-16-capability-config-shape.md` carries the `config_at(level)`
shape sketch, the three preserved `base.py` constraints, the who-constructs-versus-who-calls-in
caution, and the pre-design call-site discovery walk.

## Out of scope

- All artifact functionality: the `artifacts` block, kinds and producer APIs, acquisition,
  normalized or wire formats, propagation/deferral, snapshot storage, artifact installation, and
  shell artifact files. R7, R12, and R16 are deferred; the future context above does not require
  placeholders now.
- Session and run identity schema/context work, including the previously proposed early
  `session_uuid` slice, the universal event vocabulary, PTY observation, and other observability
  work. The saga coordinates their owning effort; this effort does not deliver them.
- Workspace plugin installation and a native deferred-setup protocol; see R14. Native project
  settings remain in scope, while standalone rule and skill delivery is deferred with artifacts.
- Feature capability kinds, registration, config, APIs, execution, and dependencies, including test
  feature capabilities. Future features fit between core and harness integrations; `user-feature`
  remains the future kind for both admin and agent users.
- Automatic hint emission from core setup behavior and Rulesync integration or runtime reuse.
- Git-backed native settings acquisition. R15 retains workstation file sources.
- External plugin distribution and its trust model (wave 8).
- Harness integration config knobs for per-session workload inputs (issue #674), which shipped ahead
  of this charter by operator ruling on 2026-08-26 and deliberately without an SDD. That work does
  not establish this effort's scope, and knobs landing early is not a precedent for treating the
  pipeline, per-scope init methods, attachments, applied state, or the vertical integration as
  settled.

## Definition of done

The effort is functionally complete when a harness integration can participate at VM, user, and
workspace facets through its own API and config alongside its existing session facet; all five
owning scopes receive the correct core env; core carries no harness-specific fields or setup
dispatch (R11); and reinit converges without accumulating native setup. The real-CLI vertical uses
ordinary local env, native plugin, and settings fixtures, with no features, artifact pipeline, or
external service dependencies.

Explicit selection, multiple setup integrations with separate config, and required/recommended
prerequisites for the actual bound user are proven. Claude Code and Codex user marketplace/plugin
setup is demonstrated, user and workspace settings are not flattened, and workstation mappings
exercise all four R15 policies. Metadata receipts support readiness, drift reporting, and safe
idempotent removal or explicit retention when desired native setup changes or an attachment is
removed. The ordinary shell session retains its behavior with no-op setup facets. Deferred artifact
requirements and the withdrawn early session identity slice do not gate this effort.
