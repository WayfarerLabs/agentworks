# Capability Perspective on Next Steps

- Status: Initial assessment
- Date: 2026-08-04
- Baseline: Agentworks 0.13.0 (`v0.13.0`)

## Purpose

This document records an architectural assessment of the Agentworks capability APIs before the
project adds substantially more capability kinds. It is an input to the requirements and design work
that will follow in this SDD, not a functional specification or an implementation plan.

The assessment covers:

- Fitness of the shared capability model for current and future extension points.
- Fitness of the plugin-facing implementation contract.
- The unofficial shared-base status of secret backends and their long-term shape.
- The current harness-integration API and its planned expansion into machine, agent, workspace, and
  session concerns.
- The relationship between these APIs and the planned Pydantic-based declarative schema model.

## Executive Assessment

The capability model has a strong conceptual foundation and good lifecycle semantics. It is fit for
first-party implementations of the existing kinds. The implementation framework beneath it is not
yet generalized enough to support rapid growth in capability kinds or a stable third-party API.

The immediate recommendation is to preserve the lifecycle model while consolidating registration,
schema declaration, runtime validation, and per-kind dispatch before adding many more kinds.

| Area                                          | Assessment                                    |
| --------------------------------------------- | --------------------------------------------- |
| First-party implementations of existing kinds | Fit for purpose                               |
| Many implementations of existing kinds        | Mostly fit                                    |
| Many new capability kinds                     | Too much duplicated framework wiring          |
| Stable third-party plugin contract            | Not ready to promise                          |
| Secret backends                               | Belong in the capability model                |
| Current harness-integration session API       | Strong for session workloads                  |
| Multi-scope harness setup                     | Right concept, wrong current runtime object   |
| Declarative Pydantic schemas                  | Best opportunity to consolidate the framework |

## What Should Be Preserved

### Capability and consumer separation

The model correctly distinguishes four concepts:

1. A capability kind is a core-owned extension point.
2. A capability resource is a registered implementation of that kind.
3. A capability instance is an implementation bound to one usage's configuration.
4. A consuming resource is declarable data that selects and configures a capability.

This separation keeps implementation code out of declarable resources while preserving useful
resource attribution, graph relationships, inspection, enablement, and readiness.

Capability kinds should remain core-owned. Generalizing their registration does not imply allowing
plugins to invent new kinds. A new kind changes core orchestration and should continue to receive
core design review.

### Lifecycle semantics

The current lifecycle is one of the strongest parts of the design:

1. Configuration declaration and validation are pure.
2. Construction is cheap and never resolves secrets or performs I/O.
3. `preflight` is read-only, runs before secret resolution, and is dependency-blind.
4. Secrets normally receive one cached resolution pass at the operation boundary, with narrowly
   sanctioned gate and repair paths that preserve no-duplicate-resolution and no-duplicate-prompting
   guarantees.
5. `runup` is read-only, authenticated, and deferred until immediately before the operation.
6. Operations own mutation and their idempotency requirements.

The split prevents unnecessary prompts, avoids checking resources before earlier phases create them,
and gives authenticated failures a clean boundary before mutation. Future work should preserve these
semantics even if the concrete context and schema APIs change.

### Other strong choices

- Configuration-derived resource and secret references are attributed to the consuming resource.
- Plugin registration prepares every contribution before mutating registries, giving it atomic
  failure behavior.
- Capability instances are held and composed by graph nodes instead of masquerading as graph nodes.
- Disabled, unsupported, not ready, and absent are distinct states.
- `OperationScope` enforces identity invariants rather than relying only on convention.
- Harness state is namespaced by integration so changing an integration does not leak another
  integration's state into it.

## Shared Framework Findings

### The conceptual model is more generalized than the implementation

The current framework is a closed switchboard for four kinds. Adding a capability kind requires
coordinated changes across:

- The capability adapter table.
- The graph's hard-coded capability-kind set.
- Implementation lookup and readiness dispatch.
- Bootstrap publication.
- Plugin-registry snapshot and restore behavior.
- A new implementation registry, resource-row type, publisher, and consumer gate.

Tests can detect several omissions, but detection does not eliminate duplicated wiring. As the kind
count grows, this structure will produce boilerplate and semantic drift.

### Recommended capability-kind descriptor

Introduce a core-owned, typed capability-kind descriptor registered once per kind. Its exact shape
belongs in later design work, but it should provide or reference:

- Kind identifier and implementation contract version.
- Required implementation base class or protocol.
- Implementation registry and factory policy.
- Capability-row factory.
- Configuration schema contract.
- Readiness strategy.
- Enablement and consumer-gating behavior where applicable.

Graph stamping, plugin registration, row publication, inspection, and framework consistency checks
should derive from this descriptor registry. Domain-specific operations should remain on each kind's
own interface.

### Registration is not yet a safe third-party boundary

Registration currently validates descriptor structure, known kinds, names, and collisions, and it
preconstructs secret backends before atomic seating. It does not prove kind conformance, including
that the class:

- Implements the selected capability kind.
- Supplies all required metadata.
- Is constructible with the expected inputs.
- Implements the required operations.
- Declares a compatible schema and API version.

The current use of `type`, `object`, `Any`, and casts is acceptable for curated in-repository system
plugins. It is not enough for an external plugin SDK. External discovery, namespacing, compatibility
negotiation, and plugin versioning are also not yet complete.

Stronger registration validation would improve compatibility and failure behavior, but would not
sandbox or make arbitrary third-party code trustworthy. External plugins execute host-side code. A
public plugin system therefore also needs the separately designed distribution-trust and explicit
operator-enablement model.

Until those contracts exist, the capability API should be described as an internal extension
framework rather than a stable third-party API.

### Runtime context will become a compatibility pressure point

`RunContext` usefully encodes the current operation world, but its fixed admin target, agent target,
secret reader, config, and scope fields also make it a growing service locator. New kinds that need
other services would require core changes to the shared context.

The stage timing semantics should stay stable, but future contexts should expose typed,
least-privilege services or grants. A capability should receive only the targets, secrets, and
services declared for the operation it is performing.

## Declarative Schema Direction

The Pydantic migration should be treated as an API redesign opportunity, not only a dataclass
replacement.

Today a capability implementation independently interprets the same raw mapping in two methods:

```python
dependencies(owner, config)
validate(owner, config)
```

The split exists for a good reason. Graph discovery must be total and non-throwing even when a
configuration block is malformed. Asking extension authors to maintain two matching raw parsers is
nevertheless fragile.

A registered Pydantic model should become the authoritative source for:

- Typed runtime configuration.
- Defaults and required fields.
- Unknown-field behavior.
- Resource and secret references.
- Reference usage descriptions.
- Generated schema documentation and samples.

Pydantic validation alone is insufficient. The model system needs explicit resource-reference field
types or annotations so the framework can discover graph edges without invoking domain code. It must
also preserve source-location framing and predictable behavior for malformed reference fields.

The existing invoked `validate` and `dependencies` methods can remain behind a temporary adapter for
current implementations. New capability implementations should use the declarative contract, and the
two paths should not remain equal permanent APIs.

## Secret Backends

### Classification

Secret backends are already capability resources in most meaningful respects. They have a capability
kind, participate in plugin registration, publish read-only rows, contribute readiness, and own
their mapping validation and lookup behavior.

They belong in the capability model. Their mismatch is the runtime representation, not the domain
classification. Forcing them to inherit the current `Capability` base solely for uniformity would
not improve the design.

### Current scalability ceiling

Unlike the other capability registries, the secret-backend registry stores constructed singleton
instances. The adapter and graph layers carry special cases for that representation.

More importantly, there is no configured-instance layer. The active chain and each secret's mapping
name a backend implementation directly. That cannot naturally represent:

- Work and personal accounts for one vault vendor.
- Multiple Vault clusters or Connect endpoints.
- Human-authenticated and service-account variants.
- Different configuration or readiness for two instances of the same backend.

### Recommended two-level model

Retain `secret-backend` as the implementation or driver capability and introduce a declarable,
configured source resource. The final noun should be selected during requirements work; the examples
below use `secret-source`.

```text
secret-backend/onepassword
    secret-source/work
        backend: onepassword
        config: ...
    secret-source/personal
        backend: onepassword
        config: ...
```

The active chain and per-secret mappings would name source instances. Built-in zero-config sources,
such as `env-var` and `prompt`, can be synthesized under their current names to preserve the simple
case.

The backend registry should store definitions, classes, or factories uniformly. An operation should
construct a source client with a bounded lifetime so authentication sessions, connections, cleanup,
and configuration-dependent readiness have an honest home.

### Resolution API evolution

The current ordered batching, soft fallthrough, explicit-mapping hard failure, and no-cross-command
cache behavior are good foundations. The next version should consider:

- Typed per-secret outcomes instead of omitted dictionary entries and batch-level exceptions.
- Explicit soft miss, mapping error, authentication error, and backend outage categories.
- Policy-aware interaction requirements instead of a static `interactive` boolean.
- Timeouts, cancellation, and client cleanup.
- An async-capable contract for future controllers or concurrent remote sources, with a clear sync
  bridge if the CLI remains synchronous.
- Redaction rules for lookup identifiers that may contain sensitive metadata even when they are not
  secret values.

## Harness Integration

### Current session contract

The current API is strong as a driver for one session workload. An instance is deliberately bound to
one session, VM, workspace, launch target, mutable session-state namespace, and one-operation
readiness cache. The identity guard and shared target-object contract are valuable safety measures
within that scope.

The API should continue to own session workload launch and resume behavior. Over time, a structured
launch specification containing command or argv, working directory, environment additions, display
notes, and state changes would be safer and more composable than a raw shell command string.

### Multi-scope setup belongs to the integration concept

Installation, authentication, agent configuration, workspace publication, launch, and resume are
conceptually facets of one integration. The `harness-integration` name is appropriate for that end
state.

They should not become additional methods on today's session-bound `HarnessIntegration` instance.
That object cannot honestly represent one integration acting across a machine, several agents,
several workspaces, and many sessions:

- Machine setup may run before a session exists.
- Agent and workspace setup may run without creating a session.
- Setup state belongs to the resource being reconciled, not to a later session row.
- Reusing an object with a target and readiness cache risks stale identity and cross-target state.
- Setup requires idempotent apply or reconcile semantics, checkpoints, retries, and sometimes
  teardown, not `start` and `resume` command generation.

### Recommended facet model

Keep one stateless registered integration identity with independently declared facets:

```text
harness-integration/codex
    vm-setup facet
    admin-setup facet
    agent-setup facet
    workspace-setup facet
    session-workload facet
```

Each facet should declare:

- Whether it is supported.
- Its owning scope and resource.
- Its Pydantic configuration model and merge policy.
- Resource and secret references.
- Required target, privilege, filesystem roots, and other grants.
- Readiness and idempotent reconciliation operations.
- State schema, state version, and retry behavior.

Here, `vm-setup` means machine-scoped setup within the existing VM lifecycle. `admin-setup` means
setup of the admin identity within that same VM lifecycle, not a new machine or admin resource kind.
Both would be declared from the VM template and converged through VM initialization and `vm reinit`.

Absence of a facet should mean unsupported. Default inherited no-op methods would conceal mistakes.

The lifecycle owner should explicitly select the facet. VM/admin, agent, and workspace setup
configuration cannot live only on a session template because those resources can be created without
that session. The VM, agent, and workspace templates should carry their corresponding integration
attachments. A future integration-profile resource could bundle common attachments for convenience,
but should be introduced only if it develops real operator-facing identity and reuse pressure, and
should expand into explicit owner-scoped selections.

A central integration dispatcher should enforce enablement, schema validation, scoped secret
delivery, target privileges, state loading and persistence, and consistent error framing. Existing
VM, agent, workspace, and session managers should continue to own transaction and rollback
boundaries.

Setup state should be keyed by the owning resource, integration, facet, optional attachment
identity, and schema version. A dedicated state store is likely more scalable than adding an opaque
JSON column to every resource table. Stable external-artifact identifiers that are canonical entity
state should remain explicit on the owning entity rather than disappearing into an opaque facet
state blob.

## Recommended Sequence

Before adding a large family of capability kinds:

1. Remove the harness and session compatibility surfaces explicitly scheduled for removal in 0.14 so
   new abstractions do not preserve those obsolete shapes. Other deprecated surfaces retain their
   separately decided timelines.
2. Define the typed capability-kind descriptor and capability contract version.
3. Define Pydantic schema and resource-reference metadata contracts.
4. Normalize registries around definitions or factories and strengthen registration-time conformance
   checks.
5. Introduce configured secret-source instances and migrate the active chain and mappings.
6. Specify the multi-scope harness-integration facet model, selection rules, ordering, privileges,
   idempotency, and state ownership in a dedicated design phase.
7. Stabilize external plugin discovery and compatibility only after the preceding internal contracts
   are proven.

Development of additional implementations for existing kinds can continue when needed. Creation of
many new kinds should wait until steps 2 through 4 are designed, because every new kind added before
that point multiplies framework plumbing and increases the compatibility surface that later work
must migrate.

## Questions for the Remaining SDD Artifacts

- What is the smallest useful `CapabilityKindDescriptor` contract, and which behaviors must remain
  domain-owned rather than descriptor-driven?
- How does declarative reference extraction remain useful when part of a configuration block is
  malformed?
- Which parts of the capability API will be public and versioned, and which remain internal?
- What is the final noun and migration model for a configured secret-backend instance?
- Should integration attachments be direct template fields, references to an integration profile, or
  both?
- How are multiple setup facets ordered, and how are conflicting filesystem or configuration writes
  detected or owned?
- What persistent state and schema-migration guarantees do setup facets receive?
- What concrete least-privilege grants can replace the current all-purpose runtime context over
  time?
