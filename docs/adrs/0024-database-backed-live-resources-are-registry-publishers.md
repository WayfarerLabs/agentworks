# 24. Database-Backed Live Resources Are Registry Publishers

Date: 2026-08-26

## Status

Accepted. Builds on [ADR 0016](0016-yaml-resource-manifests.md), which established the Registry's
publisher model, and [ADR 0019](0019-orchestration-layer-command-plans-over-node-graphs.md), which
established database-backed VMs, workspaces, agents, sessions, and consoles as live-resource graph
nodes.

## Context

The Registry was designed as a publish destination rather than a parser. Built-ins, capability rows,
plugins, and operator manifests publish resources into one mutable collection, then one
`Registry.finalize()` pass resolves references, applies each target kind's missing-reference policy,
materializes auto-declared resources, folds readiness, and freezes the retained dependency graph.

Database-backed live resources were projected separately after finalization. That was sufficient
while a database row only selected declared templates: inspection could attach a live-use edge to a
template already in the Registry. Instance-specific specs changed the premise. A persisted spec is
part of a live resource's desired declaration and can introduce a reference that no YAML resource
contains. The runtime could accept that reference through an operation-local fallback, but normal
finalization never saw it. An auto-declared secret could therefore prompt successfully in
`agent shell` while being absent from `secret list`, `secret describe`, `secret verify`, doctor, and
the retained resource graph.

Special-casing each inspection command would create several competing answers to the same structural
question. Mutating the Registry after finalization would discard the immutability and single-pass
guarantees its consumers rely on.

## Decision

The state database is another Registry publisher.

Registry assembly has one collection boundary and one finalization boundary:

1. Built-in, capability, plugin, and operator-manifest publishers contribute declared resources.
2. A creating or reinitializing command may first contribute typed pending-live-resource projections
   for its proposed declarations, claiming any live identity whose durable desired state it will
   replace.
3. A database publisher contributes typed projections of the remaining database-backed live
   resources and their outbound references from one snapshot.
4. `Registry.finalize()` runs once over the complete collection and freezes the result.

The Registry remains publisher-agnostic. It does not open the database, parse stored payloads, or
know which publishers an application uses. Bootstrap owns composition and gives the database
publisher one consistent read snapshot.

### Resource vocabulary

- A **declared resource** is supplied by built-in, plugin, manifest, or auto-declaration machinery.
- A **live resource** is a database-backed VM, workspace, agent, session, or console. "Live" means
  present in Agentworks state, not currently powered on, connected, or running.
- A **pending live resource** is the candidate node a creating command publishes only into its
  prospective Registry build.
- A capability implementation instance remains an implementation object held by a resource. It is
  not a graph node.

All three resource forms are first-class graph nodes with distinct node-type metadata. Operational
commands remain the owning interface for live resources; publishing them does not turn a database
row into an operator manifest or require `resource list` to replace the domain-specific list
commands.

### Projection and reference rules

A live or pending projection contains graph identity and outbound references, not a provider object
or an observed-runtime snapshot. Its intrinsic row relationships become edges, including
workspace-to-VM, agent-to-VM, session-to-workspace and optional agent, and console membership.
Configuration references come from the resource's fully resolved current desired declaration: the
selected template chain followed by its optional persisted instance layer. VM and admin declaration
slots resolve as one VM-owned desired decision. Projection uses the same typed domain resolvers and
`effective_references` extractors as lifecycle code; it never scans raw JSON for names and never
exposes plaintext environment values.

Because the projection participates before finalization, ordinary missing-reference policy applies.
A missing auto-declarable target is synthesized once, explicit declarations retain their existing
precedence, and all inbound references are retained on the one graph. A derived auto-declaration is
not separately persisted. It reappears on each build while any collected resource references it and
disappears after its final owner is removed or changed.

The frozen graph also derives the existing JSON v1 `used_by` compatibility projection during that
same finalization pass. This projection deliberately preserves its established per-kind meanings:
template and site kinds report direct live owners, secrets report sessions whose effective
environment reaches them, and unsupported kinds remain null. The ordinary graph retains the broader
direct live edges. Both views are pure reads of the same finalized snapshot; compatibility does not
authorize a second database scan or a post-finalization graph layer.

Durable live rows have one recovery-safety exception. A database row can outlive a selected
template, site, owner, or member row. Its publisher keeps the live node but emits that intrinsic
edge only when the target is present in the collected Registry; it never substitutes a default or
creates a fake target. If a selected declaration is absent, the publisher cannot claim to have
resolved an effective declaration and therefore emits no effective-config references for that slot.
R5's live description surface reports that selection as unresolved. Pending candidates do not get
this exception: every selected declaration and owner they propose must resolve before mutation.

A stored desired declaration that this release cannot decode produces the established typed
unsupported-or-malformed state result. The publisher must not silently drop the owner and return a
graph that claims to be complete. Provider liveness and applied-state comparison are separate
questions and do not affect publication.

### Prospective commands

A creating command publishes its candidate effective declarations into a fresh prospective
collection before mutation. Normal finalization validates their references and materializes their
auto-declared targets. The finalized prospective graph is operation-local. Failure discards it and
changes no durable publication. Success writes the live row and desired state through the existing
atomic lifecycle boundary; the next command reconstructs the equivalent live projection from the
database.

A reinitializing command uses the same ordering to claim the existing live identity with its
candidate projection before database publication. The database publisher skips that claimed
identity, so finalization evaluates the state the successful operation would retain rather than an
old desired payload it is explicitly replacing. Other database-backed resources still come from the
same snapshot.

## Consequences

- Secret and resource inspection consume the same finalized answer as runtime dependency planning. A
  secret referenced only by a persisted instance spec is no longer operationally usable but
  structurally invisible.
- Inspection does not add a second post-finalization database scan. Its live facts therefore cannot
  drift from the retained graph's database snapshot.
- The frozen graph remains a pure snapshot. Database-backed does not mean incrementally mutable;
  durable state is re-collected on the next Registry build.
- Registry construction that promises a complete resource graph must include an available,
  compatible state snapshot. Deliberately state-free surfaces may omit the database publisher only
  when their contract does not claim live-resource completeness.
- Doctor remains non-migrating and diagnostic. When database publication is unavailable, it builds a
  fresh declared-only registry, marks live-resource coverage unavailable, and continues the checks
  that are truthful over declarations. It never continues with the mutable Registry on which a
  publisher failed or describes the declared-only result as a complete graph.
- No database migration or standalone row for an auto-declared target is required.
- Database publication adds bounded decode and resolution work proportional to stored live
  resources. It performs no provider, network, secret-value, or SSH-key operation.

## Rejected alternatives

- **Patch `secret list` by scanning overlay JSON.** This would duplicate schema knowledge, miss
  other reference kinds and consumers, and risk treating plaintext values as structural data.
- **Add a second post-finalize graph layer.** This would leave Registry consumers and composite
  consumers with different resource sets and duplicate precedence, missing-target, and traversal
  semantics.
- **Mutate the finalized Registry.** This would invalidate retained readiness and inbound-edge facts
  and make command ordering observable.
- **Persist synthesized targets.** Auto-declared resources are derived graph facts. Persisting them
  would require ownership bookkeeping and garbage collection that reconstruction already supplies.
