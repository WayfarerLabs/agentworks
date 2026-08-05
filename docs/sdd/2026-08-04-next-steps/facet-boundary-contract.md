# Facet Boundary Contract

- Status: Design-track artifact, draft for review
- Date: 2026-08-05
- Inputs: the harness-integration-scope and session-observability perspectives (`inputs/`), the
  harness-transcripts harvest, `target-state.md`'s settled rulings,
  `capability-descriptor-contract.md` (schema slots), and the session-runtime reconnaissance in
  `starting-state.md`

## Purpose

This artifact settles the facet model boundary shared by the harness-scope work (wave 4) and the
observability work (wave 5), plus the session/run identity semantics both depend on, so the two
seeds cannot diverge on their shared contract. Like the descriptor contract, it is a design decision
record: waves 4 and 5 own their plans and code, and open items are marked for the seed that carries
them.

## Constraints inherited (fixed input)

- One registered integration identity with independently declared, explicitly selected facets;
  absence of a facet means unsupported; each facet runs only in its owning resource's lifecycle;
  session operations diagnose upstream gaps and never repair them (`target-state.md`, harness
  facets).
- A facet's config surface is a descriptor schema slot: slot presence is the support claim
  (`capability-descriptor-contract.md`).
- Observation and control are facets of the same integration identity, and control requires the
  observation needed to validate and confirm its actions (observability perspective).
- One instance-state store serves per-instance specs, facet applied-state, and artifact ownership
  (`target-state.md`, four open doors).
- Heartbeats are liveness, not activity; the layered threat model bounds what observation claims
  (`target-state.md`, observability).

## The facet declaration

Every facet an integration supports declares, through its descriptor slot and facet metadata:

```text
Facet
    slot            the config model (slot presence IS support)
    owning_scope    vm | admin | agent | workspace | session
    lifecycle       which owning operation applies it (create, reinit, resume) and whether
                    reapplication is supported (idempotent apply) or the scope is fixed-at-create
    merge_policy    how declared config layers resolve for this facet
    references      resource and secret references via the standard annotated model metadata
    grants          the least-privilege set the facet receives: execution identity, target,
                    filesystem roots, secret access; nothing outside the declaration
    operations      readiness plus the facet's lifecycle operations (apply/reconcile for setup
                    facets; start/resume for the workload facet; collect, emit, current-state
                    exposure, and degradation reporting for observation; validated intents for
                    control)
    state           applied-state schema and version, persisted in the instance-state store
    requirements    prerequisites this facet satisfies for later session use, by stable key
```

### Facet taxonomy

Two families share this declaration but differ in lifecycle:

- **Setup facets** (vm, admin, agent, workspace): mutate their owning resource during its owning
  lifecycle operation, with idempotent apply where the lifecycle supports reapplication (vm and
  agent reinit) and create-time materialization where it does not (workspace). Their effects are
  recorded as applied state. Session-scope create-time materialization is not a fifth setup case: it
  belongs to the workload facet, whose config resolves and materializes once at session create per
  the fixed-at-create lifecycle.
- **Runtime facets** (session workload, observation, control): their operations run within one
  session run, though their state need not be run-scoped (see below). The workload facet is today's
  session object, unchanged in ownership. Observation collects and fuses sources into the universal
  event stream, exposes interpreted current state, and reports degradation and loss; control
  validates intents against that observed live state. Control MUST NOT be declared without
  observation, and the framework rejects that registration (conformance, not convention). The
  enforcement mechanism is a wave 4 descriptor addition per the descriptor's deferred-field
  discipline: slot-dependency metadata (a slot declares that it requires another slot), created when
  the control facet first exists.

Setup facets never run from session operations. Runtime facets never mutate upstream resources.
These are the same boundary stated from both sides, and they inherit the existing session-local
effects prohibition until each owning lifecycle is wired.

## Session and run identity

The identity model both waves build on, resolving the sharpest gap in `starting-state.md`:

- **`session_uuid`**: minted at session create, immutable, never reused. The operator-facing session
  name remains the human key and stays reusable; the uuid is what history keys on, so a
  delete-and-recreate under the same name can never splice histories.
- **`run_id`**: minted at each workload incarnation (create and every resume), unique within the
  session. The existing `boot_id` stays what it is today: VM reboot detection, not identity.
- Events and transcripts key on `(session_uuid, run_id)`. Runtime-facet state declares its scope
  explicitly: session-scoped state keys on `session_uuid` alone and deliberately survives resume
  (the workload facet's state is the load-bearing case: a harness's minted conversation identity is
  exactly what a later run needs to decide resume versus launch), while run-scoped state keys on
  `(session_uuid, run_id)` (observation's working state is the expected case). Setup facet state
  keys on the owning resource's identity, integration, facet, and attachment (the attachment key
  component is provisional pending wave 4's attachment shape); the state's schema version is an
  attribute of the record, not a key component, so upgrades migrate state rather than orphaning it.
- Run boundaries are explicit events: a resume ends the previous run (closing unfinished interaction
  pairs as expired-by-run-end, never silently) and starts a new one. Liveness signals attach to a
  run; activity signals attach to observed events within a run. VM auto-suspend consumes activity,
  never liveness.
- The schema change (two columns plus mint points at create and resume) is small and self-contained
  and may land early if convenient, per `phasing.md`; wave 5 owns it otherwise.

## State ownership

- Facet applied-state and runtime-facet state live in the single instance-state store, keyed as
  above, each record carrying its facet schema version. The existing per-session
  `harness_integration_state` blob keeps its current contract until wave 4 migrates the workload
  facet's state into the store as session-scoped state; the session-scoped key is what makes that
  migration mechanical (the blob already survives resume by design).
- Stable external-artifact identifiers that are canonical entity state stay explicit on the owning
  entity; the store holds facet state, not entity identity (harness-scope perspective, preserved).
- Secrets never enter persisted facet state or resolved-config snapshots; store references and
  redacted config only.

## Core versus integration

Core owns identity minting, PTY and input-interception primitives, event and intent schemas,
transport, fanout, persistence sinks, client authentication, and uniform requirement evaluation and
error framing. The integration owns source discovery, fusion, correlation, deduplication,
current-state interpretation, harness-specific delivery selection, and honest support and fidelity
declarations. A facet's grants bound what the integration touches; core performs tmux and PTY
operations on the integration's behalf rather than handing out raw access.

## Open questions for the wave 4 and wave 5 seeds

- The attachment and config shape at each template level, and whether admin setup shares the VM
  attachment or is independently configured (wave 4).
- Ordering and conflict reporting when multiple integrations attach at one broader scope (wave 4).
- The requirement-key and severity vocabulary session readiness exposes, and which probes may
  supplement persisted applied state (wave 4).
- The first slice of the universal event vocabulary and its source/origin/fidelity metadata (wave
  5).
- Where core intercepts terminal input without weakening direct PTY behavior (wave 5).
- Whether the observation facet declares per-source support individually or as one fused declaration
  (wave 5).
- The concrete instance-state store schema (shared design consumed by both waves and by the
  per-instance spec work; drafted with wave 4 unless the design track pulls it earlier).
