# Orchestration layer: high-level architecture

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

Builds on the FRD in this directory and on [`spike-findings.md`](spike-findings.md) (all four R11
bets hold on real types). The FRD owns the requirements and rulings; this HLA pins the shapes: where
code lives, the node protocol and key convention, the edge-derivation rule, how orchestrators drive
readiness and failure policy, the secret path, unwind, doctor, and the migration map. Exact
signatures land at LLD/implementation; nothing here invents beyond what the spike validated except
where explicitly marked as the tracer bullet's job to prove.

## Overview

```text
CLI commands (typer; thin, unchanged)
        |
        v
orchestrators: bespoke, one per command      <- the current service-layer roots,
  (vms/manager.py, sessions/manager.py, ...)    renamed by role, not moved
        |            \
        |             \ uses (as they emerge)
        v              v
node graph          orchestration helpers
  vm/box              walk() . preflight sweep . secret union+predict+resolve
  vm-site/px          scoped delivery . RealizationLog (unwind)
  git-credential/gh
  agent/box/dev ...
        |
        v
backing data: registry resources (declared recipes) . DB rows (live state)
              capability instances (bound config)
```

An orchestrator is just code (FRD: no required shape, no plan artifact). What is shared and
contractual is the NODE surface and the helpers' semantics. The layering rule: the orchestration
helpers depend only on the node protocol and the secrets framework, never on a domain; domains
implement their own nodes; orchestrators (the managers) drive both. This mirrors the capability
layering rule and keeps a capability-imports-domain violation as visible as it is today.

## Where the code lives

```text
cli/agentworks/orchestration/
  __init__.py
  node.py        # Node protocol, key convention, creatable-node teardown surface
  walk.py        # memoized multi-root walk over declared edges
  secrets.py     # secret union, central prediction, the scoped delivery reader
  readiness.py   # preflight sweep + the runup policy helpers (skip-and-degrade)
  unwind.py      # RealizationLog
```

Created LAZILY: the package starts with exactly what the tracer bullet needs and grows as migrated
commands force it (FRD: helpers emerge, no up-front framework). Node implementations live in their
domains, exactly as kinds do (`vms/nodes.py`, `agents/nodes.py`, `sessions/nodes.py`, ...);
capability instances implement the protocol directly on `capabilities/base.py` (R9), no adapter
class in production (the spike's `CapabilityInstanceNode` adapter becomes three small members on
`Capability`: `key`, `deps`, `secret_refs`).

## The node protocol

As validated by the spike, unchanged:

```python
@runtime_checkable
class Node(Protocol):
    @property
    def key(self) -> str: ...
    def deps(self) -> tuple[Node, ...]: ...
    def secret_refs(self) -> tuple[str, ...]: ...
    def preflight(self, ctx: RunContext) -> None: ...
    def runup(self, ctx: RunContext) -> None: ...
```

Readiness plus dependency declaration ONLY (FRD R1). Ops are domain-specific and un-unified;
creatable node kinds additionally expose `teardown()` (their half of unwind). Either readiness stage
may be a no-op.

**Key convention** (spike finding 3; fixes the spike's own `vm/s1` session collision):
`<node-kind>/<qualified-name>`, unique within a command's graph.

| node                           | key shape                   | example               |
| ------------------------------ | --------------------------- | --------------------- |
| live/pending VM                | `vm/<name>`                 | `vm/box`              |
| live/pending workspace         | `workspace/<vm>/<name>`     | `workspace/box/ws1`   |
| live/pending agent             | `agent/<vm>/<name>`         | `agent/box/dev`       |
| live/pending session           | `session/<vm>/<name>`       | `session/box/s1`      |
| capability instance            | `<owner-kind>/<owner-name>` | `vm-site/px`          |
| resolved template              | `<template-kind>/<name>`    | `vm-template/default` |
| harness instance (per session) | `harness/<vm>/<session>`    | `harness/box/s1`      |

VM-scoped names carry the VM in the key because their names are only unique per VM. Exact spellings
are pinned at LLD; the invariant is per-graph uniqueness, since memoization, cycle reporting, and
the unwind log all key off it (the node-graph analog of the registry's `(kind, name)`).

## Deriving the graph (the translation rule)

The load-bearing unknown after the spike (its edges were hand-wired; the walk was proven over a
GIVEN graph). The rule, stated once and implemented per node kind in each kind's `deps()`:

1. **Registry references translate by kind.** A node backed by a declared resource reads that
   resource's references (`referenced_resources()` / `validate_config` implied refs) and maps:
   - `secret`-kind references -> `secret_refs()` entries (secrets are NOT nodes; they are inputs the
     orchestrator resolves);
   - capability references -> a dependency edge to that capability's INSTANCE node, constructed from
     the reference site's config blob (the capability collapse), memoized per command by key (one
     `vm-site/px` node no matter how many consumers: the spike's `bind_platforms` dedup);
   - other resource references -> an edge to that resource's node.
2. **Row fields translate to live edges.** A live node derives edges from its DB row: a live VM's
   `site` field -> the site's platform instance node; a live agent's row -> its VM node. Live nodes
   have no registry resource of their own; the row IS the backing data.
3. **Pending nodes are constructed with their edges** by the orchestrator, from the resolved
   templates and rows it planned with: a pending agent depends on its agent-template node (whose
   references pull in the git-credential instances) and its VM node.

Nodes declare only their OWN edges; assembly is the walk's job. The tracer bullet's defining
obligation (FRD R8) is to run this rule end to end for one real command, deriving the graph from
declared references and rows with zero hand-wired edges, and reproduce the imperative behavior.

## Driving readiness

**Preflight.** The sweep is a helper over the walk's output: every participating node, against the
command-start context, before any prompt or mutation. Dependency-blindness stays structural (pending
targets are pending; command-start contexts carry only existing targets), and the readiness that
floats (the harness's required-commands) floats by querying its target node's pending-ness, exactly
as spiked.

**Runup timing.** Deferred to just before the node's first use, driven by the orchestrator as it
advances: realize a node, then runup the nodes whose first ops come next, against a context
reflecting the new reality. Command-shaped by design (FRD R4's invariant is only preflight-all ->
resolve-once).

**Runup failure policy is the orchestrator's** (FRD R4, spike-review carry). The node's runup keeps
the narrow contract (typed raise on definitive rejection; warn-and-continue INSIDE the node on
network indeterminacy). What a raise means is per-command code, with today's policies as the parity
table:

| today's caller                             | policy the orchestrator expresses                           |
| ------------------------------------------ | ----------------------------------------------------------- |
| `vm create` / `vm reinit` init             | credential rejection: skip that node's materials op, log,   |
|                                            | degrade the command to PARTIAL (today's `runup_and_filter`) |
| agent create/reinit                        | same skip-and-degrade                                       |
| `vm add-git-credential`                    | rejection is FATAL (nothing mutated yet)                    |
| `vm create` platform runup                 | rejection is FATAL (before any VM exists)                   |
| `[defaults] runup_git_credentials = false` | orchestrator skips the runup stage for those nodes          |

The skip-and-degrade pattern is the first `readiness.py` helper (today's
`git_credentials.runup_and_filter` generalized); the fatal case is a plain uncaught raise. This is
the runup analog of the rollback ruling: mechanism on the node, meaning in the orchestrator.

## Secrets

The path, end to end, replacing the resolver's three entangled jobs:

1. **Union**: `secret_union(walk(...))` over `secret_refs()` (spiked).
2. **Prediction**: central, over the same declarations, with `preview_resolution`'s EXACT current
   semantics, including the optimistic interactive-backend answer the spike surfaced (a prompt
   backend reports resolvable without probing; that is correct for operators and unchanged here).
   Doctor consumes the same computation (below).
3. **Resolve once** at the preflight boundary: one prompt session covering the union
   (`Resolver.resolve()` machinery reused; the walk replaces construct-time registration as the
   union's source).
4. **Scoped delivery**: the context's `secrets` becomes a reader scoped to the invoked node's
   declared names (a thin view over the resolved cache; shape at LLD). UN-SPIKED, so the tracer
   bullet must show a real capability runup reading through it; until then today's whole-cache
   reader is the fallback and delivery-scoping is a follow-up within the migration, not a blocker.

**Resolver retirement** (R5/R9) is sequenced, not big-bang: central prediction lands with the tracer
bullet; construct-time registration becomes dead weight as commands migrate (the walk supplies the
union); the `resolver` constructor parameter comes off `Capability` (and the vm-template seam the
spike flagged closes) in a dedicated cleanup step once no migrated command depends on it. Ops read
`ctx.secrets` per the completed declare/receive contract (PR #182's direction; proxmox's op-client
bridge dies in the same step).

## The context

`RunContext` is the node-facing surface and the spike needed ZERO changes to it: frozen,
re-assembled per stage by the orchestrator, fields unchanged (`config`, `admin_target`,
`agent_target`, `secrets`). Identity stays OFF the context (intrinsic to nodes; injected at
construction for leaves). One carry for the harness re-scope, recorded here so it is not lost: the
real harness target is agent-OR-admin, and the admin target is always realized, so `None` stays
reserved for genuine bugs and admin-mode sessions never trip the anti-silent-skip error.

## Unwind

`RealizationLog` as spiked: an ordered list appended on realize, read backwards on unwind, calling
each creatable node's `teardown()` with today's discipline (best-effort, a failed teardown warns and
never masks the original error, `UserAbort` re-raised). The teardown bodies are today's rollback
code relocated onto the nodes (`_rollback_ephemerals`' per-entity blocks, `create_vm`'s row delete);
the log is the minimal shared state the FRD's R12 promised to pin, and it is a list, not a `Plan`
class. Non-rollbackable windows stay pinned per command by its orchestrator.

## Doctor

Doctor is not a command with a plan; it is a scan over ALL declared resources. It shares the central
prediction computation (step 2 above) over the full declared reference graph and keeps calling
per-resource `preflight` exactly as today for its health rows. No node graph is built and no
behavior changes (R2/R5 parity). The one implementation note: `secrets.py`'s prediction helper takes
declarations, not a walk, so both a command's union and doctor's all-resources sweep are callers of
the same function.

## Migration map

**Tracer bullet: `agw vm add-git-credential`.** Chosen because it is the smallest command with a
real graph and it exercises the two remaining unknowns: end-to-end edge derivation (live VM row ->
site -> platform instance; credential name -> provider instance node, all from rows and declared
references, zero hand-wired edges) and a real runup with the FATAL policy, plus a real op (materials
write) reading scoped secrets. It has no pending nodes, which is fine: unwind landed in the spike
and gets its production proof in the next step.

Order after the tracer (FRD R8's double duty: de-risk AND crystallize helpers):

1. `vm add-git-credential` (tracer: derivation, fatal runup policy, scoped delivery)
2. `vm create` / `vm reinit` (pending nodes, unwind, skip-and-degrade policy, multi-capability
   graph)
3. `session create` including `--new-agent` (EARLY, the harness SDD's landing pad: the nested
   fan-out, the harness node, restart's command-shaped ordering follows with `session restart`)
4. `agent create` / `agent reinit`
5. remaining commands opportunistically (`vm delete`, `vm start/stop`, shell/exec roots), each a
   green, shippable unit

Each migrated command adds the R7 parity assertion ("no prompt after the resolve boundary") and
keeps the full suite green. Interim seams (an orchestrated command calling not-yet-migrated
machinery) are documented in `plan.md` per FRD R8.

## Design decisions (recap of rulings, with the HLA's additions)

- **Protocol, not hierarchy; composition as edges** (FRD R1). The HLA adds: `Capability` satisfies
  the protocol directly; no adapter layer in production.
- **Bespoke orchestrators; helpers emerge** (FRD, 2026-07-17 ruling). The HLA adds: the
  `orchestration/` package is created lazily, tracer-first.
- **Secrets central; nodes declare and receive** (FRD R5). The HLA adds: the retirement sequencing
  and the scoped-delivery fallback.
- **Unwind and runup-failure policy are the orchestrator's; mechanism is the node's** (FRD R4). The
  HLA adds: the parity policy table and the skip-and-degrade helper as its first shared form.
- **Identity intrinsic + injected** (FRD R3). The HLA adds: the key convention that makes node
  identity operational (memoization, cycles, unwind).

## Open questions (for plan.md / LLDs)

- Exact qualified-key spellings (the table above pins shape, not final strings).
- The scoped secret reader's shape and its interaction with `compose_env`'s whole-mapping consumer.
- `Capability` signature sequencing: when precisely the `resolver` parameter is removed, and the
  order of the proxmox op-client bridge removal relative to it.
- Node construction factories per domain (who builds `vm/box` from a row: a `vms/nodes.py` factory
  is assumed; confirm against `bind_platform`'s current callers).
- Console nodes: no migrated command needs one until late; introduce lazily (R8).
- Whether `readiness.py`'s policy helpers stay two functions (sweep, skip-and-degrade) or the tracer
  reveals a third shape.
