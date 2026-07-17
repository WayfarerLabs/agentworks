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
  agent/dev ...
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
  activation.py  # the activation gate: ensure_active + the held-active span
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
`<node-kind>/<name>`, plain, matching the registry's `(kind, name)` convention exactly.

| node                           | key shape                   | example               |
| ------------------------------ | --------------------------- | --------------------- |
| live/pending VM                | `vm/<name>`                 | `vm/box`              |
| live/pending workspace         | `workspace/<name>`          | `workspace/ws1`       |
| live/pending agent             | `agent/<name>`              | `agent/dev`           |
| live/pending session           | `session/<name>`            | `session/s1`          |
| capability instance            | `<owner-kind>/<owner-name>` | `vm-site/px`          |
| resolved template              | `<template-kind>/<name>`    | `vm-template/default` |
| harness instance (per session) | `harness/<session>`         | `harness/s1`          |

No VM qualification: agent, workspace, and session names are GLOBALLY unique in the current model
(`workspaces.name` is the table's primary key; agent and session lookups are by bare name;
`agents.linux_user` is globally unique), and global uniqueness carries load-bearing value the model
keeps (maintainer ruling, 2026-07-17). The one future that could revisit this (same-named agents on
different VMs, if agents ever go multi-VM) would be its own model change, not a key-spelling tweak,
and is deliberately not designed for. The spike's collision is fixed by the node KIND (`session/s1`
vs `vm/s1`), not by qualification. Memoization, cycle reporting, and the unwind log all key off
these strings.

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

## The activation gate

Commands that touch an EXISTING VM converge its power state first: the activation gate (today's
`ensure_active` / `keep_active`) OPENS after BUILD and BEFORE preflight-all, so every readiness
probe that reaches the target (the harness's required-commands, any tool-state check) queries a
real, live environment instead of failing against a stopped VM (maintainer ruling, 2026-07-17). Our
model created this ordering question by giving preflight a real environment (today's preflights
never SSH), so the model owns the answer.

**It is not a new protocol stage, and not a preflight side effect.** Preflight is read-only, so it
cannot start a VM; the gate is the ORCHESTRATOR driving the live VM node's own power-state OPS
(`status`, `start`, and the hold-active span below), exactly the domain ops today's `ensure_active`
calls. Power state is VM-node vocabulary, not a shared-protocol method, so nothing about it touches
the thin `Node` surface (readiness plus deps); it is one more piece of orchestrator choreography
over node ops, and `ensure_active` / `keep_active` become an `activation.py` orchestration helper.

**This placement is provisional, and cheap to revisit** (maintainer note, 2026-07-17). Activation
could plausibly become an optional protocol stage (an `ensure_available`-style hook that most node
kinds no-op), and the case for keeping it as domain ops now is only that power state is meaningful
for so few node kinds that a protocol slot would sit empty on nearly all of them. If driving it
through domain ops turns awkward as commands migrate (for example, a second node kind grows a
real "make yourself available" step, or the gate helper starts special-casing node types), that is
the signal to fold it into the protocol. The tracer bullet and `vm create` will exercise it first;
revisit then if it chafes.

Five properties keep it crisp:

- **It is a SPAN, not a point** (WSL2, and any future platform that must be HELD active rather than
  merely started). The gate opens before preflight and stays open through the whole command, closing
  at the end: `keep_active` today is `ensure_active` plus `with platform.vm_active(vm)`, and WSL2's
  `vm_active` spawns a keepalive subprocess that must live for the operation's duration. So the
  orchestrator wraps the command body in the active span (`vm_active`, the VM node's span-op); a
  platform with nothing to hold gets a no-op context. The span closes on both success and failure,
  after any unwind.
- **The node is the authority on whether it MAY auto-start.** Agentworks distinguishes auto-stopped
  from operator-stopped (`vms.operator_stopped`, set by `vm stop`, cleared by `vm start`).
  Auto-start happens ONLY for an auto-stopped VM; a manually stopped one refuses, and the gate is
  exactly where the live VM node says so: a typed `StateError` ("manually stopped so it will not be
  auto-started", with the `agw vm start` hint), raised within the node's own scope before any secret
  is touched. Explicit `agw vm start` is the operator's own mutation (it clears the flag and
  starts); the gate's auto-start respects it. The re-read-the-flag race guard today's
  `ensure_active` does is preserved.
- **It is MAINTENANCE, not plan mutation.** Power-state convergence is idempotent declared-state
  maintenance (the carve the harness SDD already made, kept here): it is never rollback-tracked (a
  VM auto-started for a command that later fails stays up, the span just closes), and it does not
  bend "preflight-all before any mutation," which governs the command's PLAN.
- **Its secret needs are narrow, known, and resolved JUST-IN-TIME, outside the boundary pass.** The
  COMMON case is the platform's API credential (observing and starting a stopped VM needs it:
  `proxmox-token` for proxmox, the Azure credential for a deallocated azure VM); the REPAIR case is
  the Tailscale auth key (a VM whose tailnet registration lapsed must rejoin before it is reachable
  at all). Both resolve through the normal backend chain, orchestrator-owned, at the gate; this is
  the one sanctioned resolution outside the boundary pass. It is pre-walk-away interactivity in the
  same bucket as the confirm gates and the first-create system-slug prompt, so R7's "no prompt after
  the resolve boundary" assertion is untouched, and env-backed setups see no interaction at all.
  Gate-resolved values SEED the boundary pass so the same secret never resolves or prompts twice in
  one command.
- **The rejoin path carries messaging.** When the gate must rejoin tailscale, the surface encourages
  REUSABLE auth keys (a non-reusable key turns every restart into a rejoin problem); a failed rejoin
  is a typed error raised before preflight, with the VM left as maintenance put it.

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

## The context, and how identity actually travels

`RunContext` is the node-facing surface and the spike needed ZERO changes to it: frozen,
re-assembled per stage by the orchestrator, fields unchanged (`config`, `admin_target`,
`agent_target`, `secrets`).

**The context is SCOPE-FREE, and that is precisely what makes it passable as-is.** A context is
"pass it anywhere" only if it contains nothing node-specific, and one command's graph spans
different scopes: in a single `session create` the git-credential nodes are a system-global concern,
the platform node a site concern, the harness a session concern. A context that carried "whose
invocation is this" would have to be re-scoped per node, which is exactly the road that produced the
harness SDD's `OperationIdentity` + `level` machinery. Scope-free, ONE frozen command-start context
serves the entire preflight sweep, literally the same object handed to every node. (Contexts still
vary where the RUNTIME world varies: a later stage carries secrets, and a materials op carries its
target's transport. Timing-dependent, never scope-dependent.)

**Identity travels by construction, not by a delivery mechanism.** "Injection" oversold it: it is
ordinary constructor arguments flowing parent to child while the graph is being BUILT. Whoever
constructs a node hands it what it belongs to, and the constructor is always something that already
holds that data: the session's node factory builds its harness bound to
`(harness_config, session_name, target_agent_node)`, all three of which the factory has because it
is building that session. The same is already true today at every level: `GitCredentialProvider`
gets its `owner_name` at construct. No orchestrator reach-down into deep nodes exists, because
construction is compositional (each factory builds its immediate children); the orchestrator only
roots it. At lifecycle time nodes then carry their identity and the context carries the world.

One carry for the harness re-scope, recorded here so it is not lost: the real harness target is
agent-OR-admin, and the admin target is always realized, so `None` stays reserved for genuine bugs
and admin-mode sessions never trip the anti-silent-skip error.

## Unwind

`RealizationLog` as spiked: an ordered list appended on `mark_realized`, read backwards on unwind,
calling each creatable node's `teardown()` with today's discipline (best-effort, a failed teardown
warns and never masks the original error, `UserAbort` re-raised). The teardown bodies are today's
rollback code relocated onto the nodes (`_rollback_ephemerals`' per-entity blocks, `create_vm`'s row
delete); the log is the minimal shared state the FRD's R12 promised to pin, and it is a list, not a
`Plan` class. Non-rollbackable windows stay pinned per command by its orchestrator.

## Create vs use: the same graph, different pending sets

There is no separate "create mode" in this model. Creating a resource and using an existing one
build the SAME graph shapes; the difference is which nodes are pending, and therefore whether the
roll-forward realizes anything. Realization itself is bespoke mutation plus a recorded flag flip:
the mutation choreography is the command's authored code (ops stay un-unified, FRD R1), and
`log.mark_realized(node)` records the pending-to-realized transition that readiness queries and
unwind reads backwards. `teardown()` is the node's own inverse. Four mechanics, pinned explicitly:

- **The walk ROOTS at the command's target node(s).** The orchestrator constructs the node for what
  the command is about (the pending session; for batch commands, each target: the walk is
  multi-root) and everything else enters through declared edges. This is how FRD R4's "commands name
  only their direct resources" cashes out: naming a resource means constructing (or rooting at) its
  node; the transitive world is the graph's job.
- **Pending-to-realized is a mutation OF the node, by design.** The same object flips (one-way,
  `realized` false to true) and absorbs its realization artifacts (the created DB row), so every
  edge holder sees the transition without rewiring: the harness's target reference IS the agent node
  that just got realized. The node graph is the model's one deliberately mutable runtime record,
  with a single writer (the orchestrator, via `log.mark_realized` immediately after the bespoke
  mutation succeeds); a node's key, identity, and edges stay immutable. Contexts, by contrast, stay
  frozen snapshots (R6): mutable graph, immutable views.
- **`log` is a command-local `RealizationLog`**, instantiated by the orchestrator at the top of its
  mutation phase (`log = RealizationLog()`, the `unwind.py` helper). It lives on no context, no
  node, and no global; it is the production form of the closure locals today's
  `_rollback_ephemerals` captures, and it is the ONLY materialized plan-state in the model.
- **Realizing a resource is ORCHESTRATOR choreography composed of node ops; `log.mark_realized` is
  only the bookkeeping at its end.** No node drives its dependencies' creation (that would be the
  resource-driven fan-out R4 rejects): creating the agent is agent-node ops (user, home, store) plus
  the git-credential nodes' materials ops, sequenced by whichever orchestrator is running; the
  session node's own realizing slice is just tmux plus its row. The reusable unit is therefore the
  PHASE-FREE realization choreography per creatable kind, factored as domain code and called by any
  orchestrator that creates that kind: `agent create` wraps it in its own phases,
  `session create --new-agent` calls the same body inside its phases. This is what dissolves today's
  nesting hack, where the nested `create_agent` is a full command root that must be handed
  `git_tokens` and phase suppression to stop it re-running resolve and banners: a body never
  resolves and never frames phases, by construction. The flag-flip is NAMED `mark_realized`
  (settled, maintainer ruling 2026-07-17; the spike's `realize()` spelling dies with the spike)
  precisely so it cannot be read as doing the work.

Two walkthroughs make it concrete.

**Use: `session restart`** (everything exists; no realization record at all):

1. BUILD: the session row names its agent, workspace, and VM; the domain factories construct live
   nodes from those rows (the VM's `site` field pulling in the vm-site instance node per the
   derivation rule); session-template re-resolution by the stored name yields
   `(harness, harness_config)`, and the session factory constructs the harness node with the session
   name and the live agent node as target.
2. OPEN THE ACTIVATION GATE (a span held through the command): converge the VM's power state
   (maintenance; refuse if operator-stopped; just-in-time gate secrets if a start or rejoin is
   needed; hold active for platforms that require it) so the probes that follow query a live target.
3. PREFLIGHT-ALL over the walk rooted at the live session node: one scope-free command-start
   context; the harness's target is realized, so required-commands probes NOW, pre-resolve and
   pre-kill; any failure aborts with the old session still running.
4. Command-shaped middle, exactly today's proven order: the BROKEN/confirm gates, then the resolve
   (restart's env-chain pass sits after its gates), then env composition.
5. OPS: kill (a session-node domain op), `harness.restart(ctx)` returns the pane command, tmux
   re-create. No `RealizationLog` exists because nothing is pending; there is nothing to unwind, and
   the post-kill non-rollbackable window stays pinned per command exactly as today.

**Create: `session create --new-agent`** (target nodes pending; realization drives the middle):

1. BUILD: live VM node from its row; PENDING workspace node; PENDING agent node (deps: its
   agent-template node, whose declared references pull in the git-credential instance nodes, plus
   the VM node); the harness node constructed by the session factory with the CHOSEN session name
   and the pending agent as target; PENDING session node (deps: harness, agent, workspace, VM, and
   its resolved session-template node, exactly as the vm-template hangs off a pending VM). Names are
   chosen up front, so every node's identity is complete while it is still pending. The walk roots
   at the pending session node, the command's one target.
2. OPEN THE ACTIVATION GATE (span, held through the command): converge the existing VM's power state
   (maintenance; refuse if operator-stopped; just-in-time gate secrets if needed; hold active for
   platforms that require it), so preflight probes a live target.
3. PREFLIGHT-ALL, same one scope-free context: predictions run for every declared secret; the
   harness sees its target pending and defers. Dependency-blindness is structural, not special-
   cased.
4. SECRETS: union from the walk, central prediction, the single resolve (seeded with any
   gate-resolved values). The walk-away point.
5. ROLL-FORWARD, orchestrator-authored in dependency order, opening with `log = RealizationLog()`:
   run the workspace mutation (today's `create_workspace` body) and `log.mark_realized(workspace)`;
   run the agent mutation (today's `create_agent` body, its git-credential runups firing just before
   the materials write under the skip-and-degrade policy) and `log.mark_realized(agent)`;
   `harness.runup` now probes (target realized, fires once); `harness.start(ctx)` returns the pane
   command; tmux create plus the session row, `log.mark_realized(session)`.
6. FAILURE anywhere post-resolve: `log.unwind()` tears down whatever realized, in reverse (session,
   then agent, then workspace, as far as realization got), with today's discipline. The activation
   span closes last, on success or failure, releasing any keepalive.

`vm create` is the same shape as the second walkthrough with the pending VM as the target node, and
every other command sits somewhere on the same spectrum. The layers hold throughout: the
orchestrator owns build, phases, secrets, realization order, and unwind; nodes own their readiness,
their identity, their ops, and their teardown; the walk and the log are the only shared state.

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
- **Identity intrinsic, traveling by construction** (FRD R3). The HLA adds: the scope-free-context
  argument (pass-as-is holds BECAUSE identity is off the context) and the key convention that makes
  node identity operational (memoization, cycles, unwind).
- **Preflight gets a LIVE target, at the cost of a narrow just-in-time prompt** (maintainer ruling,
  2026-07-17). The considered alternative, denying preflight active targets so it never needs a gate
  secret, was rejected: it re-imposes the exact limitation the model exists to remove (a readiness
  check that cannot see the world until after the prompt), it demotes preflight to little more than
  the resolvability-prediction pass, and it does not even save the operator a prompt, since the
  platform credential is needed to provision regardless, just later. The gate's exception is bounded
  (narrow known secrets, pre-walk-away, seeded into the boundary pass, zero interaction for
  env-backed setups, exercised only when a VM is actually stopped), so it is a small, contained
  price for a preflight that can actually probe. Accepted deliberately.

## Open questions (for plan.md / LLDs)

- Final key spellings for the template and harness rows of the key table (the live-resource keys are
  settled: plain `kind/name` over the globally unique names).
- The scoped secret reader's shape and its interaction with `compose_env`'s whole-mapping consumer.
- `Capability` signature sequencing: when precisely the `resolver` parameter is removed, and the
  order of the proxmox op-client bridge removal relative to it.
- Node construction factories per domain (who builds `vm/box` from a row: a `vms/nodes.py` factory
  is assumed; confirm against `bind_platform`'s current callers).
- The gate-to-boundary cache seeding path: `Resolver.resolve` deliberately refuses post-pass
  registration today, so the LLD pins how the activation gate's just-in-time values pre-seed the
  boundary pass without weakening that guard.
- Console nodes: no migrated command needs one until late; introduce lazily (R8).
- Whether `readiness.py`'s policy helpers stay two functions (sweep, skip-and-degrade) or the tracer
  reveals a third shape.
