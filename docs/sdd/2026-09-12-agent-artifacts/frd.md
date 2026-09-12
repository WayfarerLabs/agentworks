# Agent artifacts: functional requirements

## Status and authority

Requirements seed, 2026-09-12. The operator directed the harness-scope-framework lead to begin this
separate successor after PR 761 merged. This document carries forward decisions made in that effort
and proposes the first delivery scope for review. The proposed scope and open choices below are not
yet accepted requirements. No artifact implementation or wire format is approved by this seed.

The predecessor is [harness-scope-framework](../2026-09-06-harness-scope-framework/frd.md); this
child participates in the [next-steps saga](../2026-08-04-next-steps/target-state.md). The effort
lead owns the response and draft requirements; accepted requirements belong to the operator. Saga
sequencing and its ledger remain the saga lead's to maintain.

## Problem and intended outcome

Agentworks can configure harnesses at VM, user, workspace and session facets, but it cannot yet
carry reusable agent knowledge through those facets. Operators must use dotfiles, repository files,
or launch prompts to make rules, skills and environment knowledge available to a harness. Those
approaches obscure where an item belongs and when it has already been applied.

An operator should declare artifact bundles once, reference them from the resources that need them,
and let the selected harness integration apply them at the appropriate facet. Acquisition choices
must converge before propagation. Core must preserve origin and route unresolved inputs without
knowing native harness layouts, duplicating delivery, or implicitly activating integrations.

## Relationship to the completed implementation

PR 761 merged as `7c744828184ccb0ad9ffd90a8a02226384fb384e`. It supplies explicit integration
activations, facet configuration, owner-bound setup, native settings/plugin reconciliation, session
prerequisites, and instance-state storage. It deliberately supplies no artifact declaration,
normalized bundle, propagation, deferral, or publication mechanism.

This effort may extend that implementation and its facet interfaces. Locking the predecessor SDD
freezes its historical specification, not the code it produced. New behavior and any changed
contracts belong in this SDD and in permanent documentation alongside the implementing change.

The predecessor remains open for its retained live acceptance and cleanup obligations; see its
[acceptance plan](../2026-09-06-harness-scope-framework/plan.md#acceptance-and-closeout). This seed
neither waives those obligations nor transfers them silently. Artifact design can proceed against
the merged foundation while predecessor acceptance is completed independently. Future artifact work
is not itself a reason to keep the predecessor unlocked.

## Users

- Operators who supply reusable rules, skills and small facts about an Agentworks environment.
- Harness integration authors who translate those inputs into their harness's native representation.
- Agents and administrators who need the intended information available for their actual user and
  session without changing other users' environments.

## Decisions carried forward

These are recorded operator decisions, not new proposals in this seed. The predecessor's
[successor context](../2026-09-06-harness-scope-framework/frd.md#future-artifact-context-not-this-efforts-contract)
and this session's authenticated direction are their source.

### Artifact kinds and acquisition boundary

Hints are small contextual facts, such as the availability of an environment variable or configured
GitHub authentication. They remain distinct from rules, skills and a session's initial prompt. A
producer needing stronger behavior can emit a rule or skill. The model must also account for
subagent definitions, and leave room for limited hooks and MCP configuration later.

Workstation files, Git references and potentially packaged distributions are acquisition concerns.
Once acquired, all sources enter one normalized representation; integrations do not carry separate
filesystem, Git and archive ingestion paths. Normalize text line endings to Unix LF. Preserve
complete skill packages, including their supporting files, rather than reducing a skill to its entry
document.

The operator's leading proposal is a declarative `artifact-bundle` resource that owns ingestion and
gives consumers an ID to reference. Its exact schema, supported source set and update behavior are
still design decisions. Evaluate Rulesync's canonical model and generation machinery for reuse;
reuse is not an approved runtime dependency. Agentworks retains ownership of resource scopes,
activation, routing and provisioned-resource lifecycle.

### Scope, facet and placement

A scope identifies an owning resource and its context. A facet is a scoped part of a capability.
Harness integrations use VM, user, workspace and session facets; admin and agent scopes use the same
user facet for their respective actual users. The admin user is not an ancestor of an agent. User
and workspace are separate ancestors of a session, not a nested pair.

Native placement at the defining scope is the ordinary case. The integration decides whether an
inner invocation needs anything further. Core attaches immutable origin: owning scope, resource
identity and producer. Origin facet follows core's scope-to-facet mapping; configuration does not
author an independent, potentially contradictory facet of origin. Bundle identity and consumer
origin are separate facts.

### Routing and inactive integrations

An activated producing facet can apply inputs and return what it defers, with reasons and intended
next facets. A VM facet can route to user, workspace or directly to session. It chooses solely from
its own inputs and responsibilities, without discovering downstream instances or checking whether
those descendants activate the integration.

Each owner calculates its result independently for its current inputs. Later consumers reuse it;
creating another user, workspace or session does not rerun the producer or consume its result
globally. A user facet processes applicable VM inputs without consulting workspaces or sessions. The
session combines direct VM input and applicable user/workspace results. Handling for one user or
workspace cannot discharge another's obligations.

**Without a VM integration activation, all defined inputs remain unhandled and go directly to the
session facet.** Core does not guess user versus workspace placement or broadcast those inputs down
both branches. An inactive intermediate facet likewise leaves its applicable routed inputs unhandled
for the session. Resolution can be lazy for the session's selected integration and actual ancestors;
agent initialization need not enumerate inactive integrations or store empty passthrough results for
them. Origin survives every skipped facet.

An explicitly activated but unimplemented facet is an error. That is distinct from an inactive facet
and from an implemented facet that needs no native changes. Successful setup alone never means an
artifact was handled. No separate per-item acknowledgment ledger is required merely to express
deferral. The successor must settle the exact result contract and how core detects and reports
inputs still unhandled at the final session facet. Silent loss is not an acceptable result; whether
every unresolved input blocks launch remains an open operator choice.

### Pipeline and lifecycle

Start with core env and artifact declarations, then harness integrations. Features may later run
between the two. This effort does not introduce features or add hint emission to unrelated core
setup. The future user feature capability is named `user-features` for both admin and agent users.

Integrations must be explicitly activated, including defaults-only configuration. Applying artifacts
must respect the selected owner's actual identity and native placement. Session handling must not
silently provision shared user or workspace state. Existing instance state supports idempotent
reconciliation and cleanup of previously provisioned effects wherever ownership and native tooling
make that possible. Parent deletion retains the existing core lifecycle; artifact bookkeeping must
not become a prerequisite for deleting a VM and everything within it.

The shell integration publishes artifacts as files with a documented discovery contract. There is no
session filesystem: session-specific files belong beneath the actual user's home in an
`.agentworks-artifacts` area with a session component, not in the shared workspace. The exact layout
and identity/reuse semantics belong in the design. File publication must not be described as proof
that a shell workload interpreted or obeyed the content.

## Proposed first delivery scope

The following requirements are proposals for this new SDD. They make the agreed direction concrete
without selecting the wire format or native implementation prematurely.

**R1. Declare and consume bundles through ordinary resources.** Introduce `artifact-bundle` and an
`artifacts` block on the appropriate owning resources, using ordinary resource references,
inheritance, validation and discovery. Multiple consumers can reference a bundle without sharing
completion or placement state. Defining a bundle alone does not install it or activate an
integration.

**R2. Acquire workstation and Git sources.** Support workstation files/directories and Git sources
in the first delivery. Define how a Git reference selects a revision and package root, when source
changes are captured, and how an operator deliberately updates captured content. A setup operation
uses consistent captured inputs. Errors identify the source and consumer without revealing
credentials or content. A packaged distribution source remains a later extension unless research
shows an existing format makes it a small, justified addition.

**R3. Normalize hints, rules, skills and subagent definitions.** Carry these four kinds through one
internal representation with source provenance and complete supporting files. Preserve binary assets
while normalizing text line endings. Acquisition must reject package paths that escape the declared
boundary; the design must state link and file-type handling. Hooks and MCP configuration are
recognized extension directions, not first-delivery functionality.

**R4. Apply and defer through explicit facets.** Implement the carried-forward owner-independent
routing model, including lazy inactive-facet passthrough, direct-to-session fallback when VM
activation is absent, and convergence of user/workspace paths without duplicate delivery. Preserve
origin and destination applicability separately. Define deterministic ordering and collision
behavior for multiple bundles and producers before implementation.

**R5. Publish honestly through the shipped integrations.** Specify an artifact-kind and facet
support matrix for shell, Claude Code, Codex and Grok Build. Implement native delivery where the
harness has a suitable mechanism, and explicit unsupported outcomes where it does not. Do not infer
capability merely from a similarly named harness setting or from files being present. Include worked
examples for native placement, deferral, inactive ancestors and two concurrent sessions. The matrix
must identify any first-delivery limitation before the HLA is approved.

**R6. Give hints an appropriate representation.** Integrations may aggregate small hints into a
native rule or include them in launch context when supported. Keep hint provenance and lifecycle
clear without manufacturing one rule per fact. Hints are supplied through the artifact mechanism;
automatic emission by install commands, env declarations or features is separate future work.

**R7. Keep sessions and owners isolated.** Session publication under a user's home must respect that
user's access boundaries and must not unintentionally affect another session in the same workspace.
Define cleanup and replacement behavior for session restart, deletion and name reuse. A session
consumes applicable ancestor results without repairing ancestor setup implicitly.

**R8. Reconcile owned effects.** Repeated setup converges. Removing a reference, changing a bundle,
removing an activation or deleting an owning resource has an explicit outcome for previously
provisioned artifacts. Remove owned effects where safe; retain and diagnose ambiguous or unowned
content. Preserve recoverable progress after failure through the existing instance-state facility.
Avoid a new universal reconciliation engine or restoring the predecessor's rejected per-key settings
ownership ledger.

**R9. Explain unresolved delivery.** Diagnostics identify the selected integration, original owner,
artifact and the reason it remains unhandled. Core enforces the final-session disposition chosen in
the HLA; omission, warning or success must not disguise dropped inputs. Inspection and reference
commands must not acquire artifacts, contact remotes, read settings sources or mutate setup merely
to explain declared configuration.

**R10. Validate without unrelated external services.** Use simple local artifacts, local Git
repositories and dedicated test integrations to prove ingestion, routing and cleanup. Exercise real
harness tooling and a scoped live VM where placement and ownership are the claim. Avoid model calls
and external marketplace dependencies. Publish the native capability matrix, source/update rules,
worked manifests and migration guidance with the behavior they explain.

## Explicit exclusions

- Feature execution, inter-feature dependencies and automatic core hint emission.
- General environment management or replacing the existing env precedence rules.
- Hook execution, MCP server management and artifact generation/distillation from runtime sessions.
- Workspace plugin installation, a harness plugin marketplace, or a new package distribution
  service. Artifact bundles and native harness plugins are distinct concepts.
- A permissions/grants engine for first-party integrations, or a second instance-state system.
- A mandatory Rulesync runtime dependency or a new archive format chosen without research.

## Decisions for requirements and architecture review

1. Accept or narrow the proposed first-delivery sources: workstation plus Git, with packaged
   distribution later. The normalized representation must not depend on this scheduling choice.
2. Decide final-session handling of unresolved artifacts. The recommended starting point is a launch
   error with integration-supplied reasons, but the prior decision deliberately left room for an
   explicit softer outcome. This seed does not settle that policy.
3. Approve the researched native support matrix before committing to first-delivery coverage. Define
   honest outcomes for harnesses that cannot express a kind or placement.
4. Set source refresh, stable content identity, ordering and name-collision semantics in the HLA.
   These must compose with resource reuse and concurrent sessions without global consumption.

## Definition of done

The operator can acquire a reusable bundle, reference it from actual owning resources, and launch a
session whose selected integration receives exactly the applicable inputs still needing handling.
Inactive ancestors and the VM/user/workspace diamond cannot lose or duplicate an input. Supported
native representations and shell files have the promised placement and lifecycle; unsupported cases
receive the agreed explicit disposition. Repetition, update, removal, failure and session reuse are
demonstrated with observed evidence. Permanent documentation stands alone, and the predecessor's
remaining acceptance is not misrepresented as artifact completion.
