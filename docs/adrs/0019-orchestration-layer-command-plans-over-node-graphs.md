# 19. Orchestrate Commands as Plans over Node Graphs

Date: 2026-07-18

## Status

Accepted. Builds on the capability/declarable resource split of
[ADR 0016](0016-yaml-resource-manifests.md); the capability lifecycle contract this layer drives
(validate/construct/preflight/runup/ops) is documented in `cli/agentworks/capabilities/README.md`.

## Context

Every service-layer command used to hand-wire its own world. Each command bound its VM platform
imperatively, collected the secrets it thought it needed at whatever point it happened to need them,
ran whatever readiness checks its author remembered, kept the VM alive through ad hoc gate calls,
and carried a bespoke rollback closure for failures. Four problems kept recurring:

1. **Composition was duplicated and drifted.** Which resources a command touches, and therefore
   which platforms to bind, which secrets to collect, and what to check, was restated per command
   instead of derived from the declared resource references and DB rows that already encode it.
2. **Secret handling was scattered.** Prompts could land mid-command, the same secret could be
   resolved twice, and readiness checks forked on where a secret happened to come from.
3. **Capability instances were entangled with secret machinery.** Each instance was constructed
   against a per-operation "resolver" object that did three jobs at once (registering the command's
   secret union at construct time, predicting resolvability in preflight, and serving values to
   ops), so instances held a value source and the union's membership was a side effect of
   construction order.
4. **Failure handling was bespoke.** Rollback lived in per-command closures with no shared record of
   what had been created, and power-state management (starting a stopped VM, holding it against idle
   shutdown) was interleaved differently in every command.

## Decision

A command is a **plan over a graph of nodes**, driven by a **bespoke orchestrator**: the authored,
command-specific composition code at the top of the service layer. There is one orchestrator per
command, plural by design; no engine, no declarative plan artifact, and no required shape. What is
shared and contractual is the node surface and a small set of helpers under
`cli/agentworks/orchestration/` (`node`, `walk`, `secrets`, `readiness`, `activation`, `unwind`)
that depend only on the node protocol and the secrets framework, never on a domain. Domains
implement their own nodes; orchestrators drive both.

### Readiness and Node, two complementary contracts

- **`Readiness`** is two verbs: `preflight` (pre-resolve, dependency-blind, read-only) and `runup`
  (post-resolve, authenticated, read-only, deferred to just before the ops it gates). Capability
  instances satisfy it and nothing more.
- **`Node`** is `Readiness` plus graph identity: `key`, declared `deps`, declared `secret_refs`.
  Only consuming resources (a vm-site, a git-credential) and live resources (a VM, a workspace, an
  agent, a session, whether existing or pending) are nodes. A capability instance is **held** by a
  node and its readiness is **composed** by the holder; it is structurally not a node and is never
  walked. Keys are plain `<kind>/<name>` over naturally globally-unique names.
- Ops stay domain-specific and deliberately un-unified, on the instances and node kinds. Creatable
  node kinds additionally expose `teardown()`, their half of unwind.

### The graph is derived, never hand-wired

A translation rule, implemented per node kind, turns existing data into the graph: a declared
resource's references map by referent (`secret`-kind references become `secret_refs` entries,
references to other declared resources become dependency edges, a capability reference with config
at the reference site becomes a held instance); a live node's DB row fields become edges (a VM row's
`site` field is its edge to the vm-site node); pending nodes are constructed with their edges by the
orchestrator, names chosen up front so identity is complete while still pending. One object per key
is a construction invariant the walk enforces loudly: every holder of "the same" node shares one
object, so the pending-to-realized flip is observed everywhere without rewiring.

### Two-layer identity and the run context

- **Layer 1, intrinsic self-identity**: a node's own `kind/name` and, for live nodes, the ancestor
  names its row carries. Path-independent, which is what keeps a node reached by several paths (a
  shared credential under two consumers) well-defined.
- **Layer 2, the operation scope**: why the command runs. A frozen `OperationScope` built once per
  command and identical on every node's context, keyed by a level (SYSTEM, VM, WORKSPACE, AGENT,
  SESSION) whose field rules the constructor enforces. It is descriptive, never power-granting: a
  node reads the LEVEL (the skip/defer/probe/error fork; a SYSTEM-scoped doctor scan legitimately
  skips session-only checks) and treats the names as error framing. A node addresses through its own
  layer-1 identity, never through scope names.
- The power-granting world (execution targets, resolved secrets) is reached through plain accessor
  methods on the run context (`ctx.admin_target()`, `ctx.agent_target()`, `ctx.secret(name)`),
  pass-through today, shaped so a later permission model can gate them without changing the
  node-facing signature.

### The activation gate

Commands that touch an existing VM converge its power state through a gate that opens after build
and **before** the preflight sweep, and stays open as a span through the whole command (platforms
with idle shutdown are held active; unwind runs inside the span). The node is the authority on
auto-start: an operator-stopped VM refuses with a typed error, including a re-read of the intent
flag to close the concurrent-stop race. Gate secrets (the platform API credential; the Tailscale
rejoin key on the repair path) resolve just-in-time through the normal backend chain, the one
sanctioned resolution outside the boundary pass: narrow known names, entirely before the walk-away
point, skipped on the fast path, and **seeded** into the boundary pass so no secret ever resolves or
prompts twice in one command.

### Secrets: declare, union, predict centrally, resolve once, deliver scoped

The path end to end: capabilities and nodes **declare** secret references; the command's union is
computed from the walked plan's `secret_refs` (never from construction side effects); resolvability
is **predicted centrally** over declarations, by the node holding the instance, with doctor
consuming the same computation; the union resolves in **one pass** at the preflight boundary (one
prompt session, at a predictable moment, after preflight passes and before anything mutates); and
values are **delivered scoped**: `ctx.secret(name)` hands a node or instance only the names it
declared, refusing anything else with a typed error. Capability construction binds `(name, config)`
and touches no secret machinery at all; scoped delivery through the context is the only way an
instance ever sees a secret value. The per-instance resolver object is retired; what remains is an
orchestrator-owned boundary resolver at each composition root.

### Unwind

Failures after the boundary unwind through a `RealizationLog`: a command-local ordered record,
appended as each pending node's realizing mutation completes, read backwards on failure to call each
node's `teardown()`. The discipline is best-effort: a failed teardown warns and never masks the
original error, an operator abort is re-raised and never downgraded, and non-rollbackable windows
stay pinned per command by its orchestrator. Best-effort reverse-order unwind, rather than a
Terraform-style taint-and-leave, is a conscious parity-driven choice: it preserves the rollback
behavior the commands already had under the migration's behavior-parity rule, and is not a fresh
design decision.

## Consequences

### Positive

- One prompt session per command, at a predictable point, with the no-double-resolve property held
  structurally (union from the plan, gate values seeded, scoped delivery over the cached pass).
- Scoped secret delivery is a security invariant, not a convention: an instance cannot read a secret
  it did not declare, and cannot hold a value source of its own.
- Graphs are derived from declared references and rows, so a command's composition cannot drift from
  the declarations; readiness, prediction, and doctor's health rows share one computation each.
- Failure behavior is uniform and recorded: what was created is what unwinds, in reverse order.
- The migration landed command by command, always green, with the full pre-existing test suite as
  the behavior oracle; the imperative machinery it replaced was deleted as its last caller migrated.

### Negative

- Bespoke orchestrators mean composition code per command rather than a single engine. Accepted
  deliberately: the command-shaped middles (confirm gates, banners, special cases) stay authored and
  readable, and there is no framework to fight or maintain.
- The gate's placement means a stopped VM can see two prompt bursts (gate, then boundary) where a
  single post-resolve burst might have been arranged; both sit before the walk-away point, and
  nothing resolves twice. A confirmed-reachable VM costs nothing.
- Preflight is dependency-blind by construction (it runs before anything is created), so checks that
  need mid-command state belong in runup; authors must place checks by stage.
- Within the single boundary prompt session, prompting order follows the walk's deterministic
  first-encounter order; it is stable, but it is the graph's order, not an author's.

## Alternatives Considered

- **A declarative plan engine (a generic executor interpreting plan artifacts).** Rejected: the
  commands' middles are irreducibly command-shaped, and an engine would either grow escape hatches
  for every special case or force the cases into configuration. Shared semantics live in small
  helpers instead.
- **Capability instances as graph nodes.** Rejected on identity: inline instances (an agent
  template's feature map) have no globally-unique name, so putting them on the graph would force
  owner-qualified keys everywhere. Held-and-composed keeps every key natural.
- **Taint-and-leave failure handling.** Rejected for parity (see Unwind above): the commands already
  rolled back best-effort in reverse order, and preserving that observable behavior was a
  requirement of the migration.
- **Denying preflight a live target so it never needs a gate secret.** Rejected: it re-imposes the
  exact limitation the layer exists to remove (readiness that cannot see the world until after the
  prompt) and saves nothing, since the platform credential is needed regardless, just later.
