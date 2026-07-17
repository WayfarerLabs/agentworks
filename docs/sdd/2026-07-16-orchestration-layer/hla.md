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
  node.py        # Readiness + Node protocols, key convention, creatable-node teardown surface
  walk.py        # memoized multi-root walk over declared edges
  secrets.py     # secret union, central prediction, the scoped delivery reader
  readiness.py   # preflight sweep + the runup policy helpers (skip-and-degrade)
  activation.py  # the activation gate: ensure_active + the held-active span
  unwind.py      # RealizationLog
```

This is the EVENTUAL shape, not the tracer's starting set. The package is created LAZILY and grows
as migrated commands force it (FRD: helpers emerge, no up-front framework). The tracer
(`vm add-git-credential`) touches an EXISTING VM (it wraps its work in `keep_active` today) with a
FATAL runup and no pending nodes, so it needs `node.py`, `walk.py`, `secrets.py` (union, prediction,
scoped reader), `readiness.py`'s preflight SWEEP, and `activation.py` (the gate), but NOT
`unwind.py` (`RealizationLog`, first forced by `vm create`'s pending nodes) or `readiness.py`'s
skip-and-degrade POLICY helper (first forced by `vm create` / `vm reinit` init, whose credential
rejection degrades to partial rather than aborting). The plan states each command's starting file
set (reviewer carry, 2026-07-17). NODE implementations (the consuming and live resources) live in
their domains, exactly as kinds do (`vms/nodes.py`, `agents/nodes.py`, `sessions/nodes.py`, ...); a
`git-credential` or `vm-site` node HOLDS its capability instance and composes its readiness.
Capability instances themselves stay `Readiness`-only on `capabilities/base.py` (R9): they gain
nothing node-shaped (no `key`, no `deps`), and the spike's `CapabilityInstanceNode` adapter, which
treated an instance AS a node, does not survive, its job (a git-credential's readiness in the graph)
moves onto the `git-credential` node that holds the provider.

## Readiness and Node: two complementary contracts

The lifecycle splits into two contracts, and keeping them distinct is what keeps the graph's
identity clean (maintainer ruling, 2026-07-17; this supersedes an earlier "capability instances are
nodes" framing, see the design decision below):

```python
@runtime_checkable
class Readiness(Protocol):
    """A thing that can be checked for readiness. Both nodes and the
    capability instances a node holds satisfy it. NOT a graph participant."""
    def preflight(self, ctx: RunContext) -> None: ...
    def runup(self, ctx: RunContext) -> None: ...


@runtime_checkable
class Node(Readiness, Protocol):
    """A readiness-bearing thing WITH graph identity: the orchestrator
    walks these. Readiness plus a key and declared dependencies."""
    @property
    def key(self) -> str: ...
    def deps(self) -> tuple[Node, ...]: ...
    def secret_refs(self) -> tuple[str, ...]: ...
```

- **`Node`** (consuming resources and live resources only): has graph identity (`key`, `deps`) and
  is what the orchestrator walks. Creatable node kinds additionally expose `teardown()` (their half
  of unwind).
- **`Readiness`** (capability instances): has the readiness verbs and nothing else, no `key`, no
  `deps`, so it is STRUCTURALLY not a `Node` and cannot be walked. A capability instance is HELD by
  a node and its readiness is COMPOSED by that node's `preflight`/`runup`, never invoked by the
  orchestrator. (It also keeps `validate_config` and its ops; its declared secrets surface through
  its holding node's `secret_refs`.)

The verbs are shared deliberately: the readiness SEMANTICS are identical (a pre-resolve check, a
post-resolve check). What differs between an instance and a node is walked-vs-composed, a
graph-participation difference that lives correctly in the TYPE (the presence of `key`/`deps`), not
in a renamed verb. So a reader who sees `GitHubCredentialProvider.preflight` sees a `Readiness`, not
a `Node`, and the question "why is this not walked?" answers itself: its holder composes it.

A node's `preflight`/`runup` COMPOSES its held instances': a thin wrapper like `git-credential/gh`
fans into its one instance (a one-line `self._instance.preflight(ctx)`); a rich node iterates its
held instances (an agent template over its feature map) and adds its own checks. This is per-node-
kind code, not a magic framework default: neither `Readiness` nor `Node` exposes a held-instances
accessor, so "the thin wrapper composes automatically" would need a held-instances convention the
protocols do not currently declare. Whether to introduce such a hook (so the second and third thin
wrappers do not each re-implement the fan-in slightly differently) or keep composition as trivial
per-kind boilerplate is an explicit LLD decision (reviewer carry, 2026-07-17; the same question
governs aggregating `secret_refs` across a map of held instances). Either readiness stage may be a
no-op. Ops stay domain-specific and un-unified, on the instances and the node kinds, never on
`Readiness`.

**Key convention** (spike finding 3; fixes the spike's own `vm/s1` session collision): `Node`s key
as `<node-kind>/<name>`, plain, matching the registry's `(kind, name)` exactly. Because only
consuming and live resources are nodes, every key is a NATURAL, globally-unique name, no
owner-qualification anywhere (the reason capability instances are off the graph: their inline
occurrences, an agent template's feature map, have no globally-unique name of their own).

| node                   | key shape                | example                           |
| ---------------------- | ------------------------ | --------------------------------- |
| live/pending VM        | `vm/<name>`              | `vm/box`                          |
| live/pending workspace | `workspace/<name>`       | `workspace/ws1`                   |
| live/pending agent     | `agent/<name>`           | `agent/dev`                       |
| live/pending session   | `session/<name>`         | `session/s1`                      |
| consuming resource     | `<kind>/<name>`          | `vm-site/px`, `git-credential/gh` |
| resolved template      | `<template-kind>/<name>` | `vm-template/default`             |

Held-not-keyed (NOT nodes): the platform instance a `vm-site` holds, the provider instance a
`git-credential` holds, the harness and feature instances a `session`/`agent-template` holds. They
have no graph key; their holder's key covers them.

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

1. **Registry references translate by what they point AT.** A node backed by a declared resource
   reads that resource's references (`referenced_resources()` / `validate_config` implied refs) and
   maps each by its referent:
   - `secret`-kind references -> `secret_refs()` entries (secrets are NOT nodes; they are inputs the
     orchestrator resolves);
   - a reference to another DECLARED RESOURCE (one with its own registry identity: a
     `git-credential` decl, another template) -> a dependency EDGE to that resource's node, memoized
     per command by key (one `git-credential/gh` node no matter how many consumers: the spike's
     `bind_platforms` dedup);
   - a reference to a CAPABILITY with config at the reference site (the vm-platform behind a
     `vm-site`, the harness behind a `session`, an agent-feature in a template's map) -> the
     referencing node CONSTRUCTS and HOLDS the instance; no node, no edge. The held instance's
     readiness is composed by the holder's `preflight`/`runup`, and its declared secrets fold into
     the holder's `secret_refs`.
2. **Row fields translate to live edges.** A live node derives edges from its DB row: a live VM's
   `site` field -> an edge to the `vm-site` node (which HOLDS the platform instance); a live agent's
   row -> its VM node. Live nodes have no registry resource of their own; the row IS the backing
   data.
3. **Pending nodes are constructed with their edges** by the orchestrator, from the resolved
   templates and rows it planned with: a pending agent depends on its agent-template node (whose
   git-credential references become edges to the `git-credential` nodes that each hold a provider
   instance) and its VM node.

Nodes declare only their OWN edges; assembly is the walk's job. The tracer bullet's defining
obligation (FRD R8) is to run this rule end to end for one real command, deriving the graph from
declared references and rows with zero hand-wired edges, and reproduce the imperative behavior.

## Driving readiness

**Preflight.** The sweep is a helper over the walk's output: every participating node, against the
command-start context, before any prompt or mutation. Dependency-blindness stays structural (pending
targets are pending; command-start contexts carry only existing targets). The readiness that floats
(the harness's required-commands) reads both the operation scope's LEVEL and its target node's
pending-ness: out of scope for the level (a system-scoped doctor scan) skips, in scope but pending
defers to runup, in scope and realized probes now, in scope and unexpectedly absent is a loud error.

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
through domain ops turns awkward as commands migrate (for example, a second node kind grows a real
"make yourself available" step, or the gate helper starts special-casing node types), that is the
signal to fold it into the protocol. The tracer bullet and `vm create` will exercise it first;
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

## The context: identity, operation scope, and gated access

Identity in this model is TWO layers, and keeping them separate is what makes the whole thing hold
(maintainer discussion, 2026-07-17; this section supersedes an earlier "scope-free context, identity
by construction" framing that did not survive the diamond case).

**Layer 1, intrinsic self-identity.** A node's `kind/name` and, for a live node, the ancestor names
its DB row carries. Path-independent and always present: `git-credential/gh` knows it is `gh` from
itself, a live agent knows its VM from its row. This is what stays well-defined when a node is
reached by several paths (a shared git-credential under two consumers): its own identity is
self-determined, never handed down by a parent, so the diamond has no "which parent's identity"
problem. Layer 1 is NOT a subset of the operation scope below (`gh` appears nowhere in a
session-create's chain); it is the node's own name.

**Layer 2, the operation scope.** WHY the command is running: its static identity chain, fixed at
command entry (names are chosen up front) and IDENTICAL for every node in the graph. It is the
answer to "why is this node being bothered," so handing all of it to every node is exactly right;
`node scope <= operation scope`, and a node reads the part it cares about and ignores the rest. The
DIVISION OF LABOR between the layers is sharp (first-consumer clarity nit, 2026-07-17): a node
ADDRESSES and acts through its own layer-1 identity, never through layer-2 names, so the harness
takes `claude --name <session>` from its OWN `session_name` (given at construction), not from
`operation_scope.session`. From layer 2 a node reads the LEVEL (the skip/defer/probe/error fork) and
treats the scope's name fields as DESCRIPTIVE only, for error and log framing ("while provisioning
session `s1`..."). The two coincide for a session command (no divergence risk), but keeping layer 2
a pure scope signal rather than a redundant name source keeps each node's construction contract
crisp. Because it is one static object, uniform across the graph, it lives on the context and is
passed AS-IS, no per-node re-scoping (that per-invocation rescoping, not the concept of scope, was
the harness SDD's `level` misstep). It is DESCRIPTIVE, not power-granting, so it is handed over
ungated: knowing "this is a session-create for `s1`" confers no capability.

```python
# capabilities/base.py (this SDD)

class ScopeLevel(Enum):
    SYSTEM = "system"        # the whole installation, no VM
    VM = "vm"                # a VM
    WORKSPACE = "workspace"  # a workspace on a VM
    AGENT = "agent"          # an agent user in a workspace on a VM
    SESSION = "session"      # a harness as agent-or-admin, in a workspace, on a VM


@dataclass(frozen=True)
class OperationScope:
    """Why an operation is running: its static identity chain, keyed by
    LEVEL. __post_init__ ENFORCES that exactly the level's fields are set
    and the rest are absent, so a scope inconsistent with its level cannot
    be constructed. This is a promised invariant, not a convention (the
    same discipline the harness SDD's OperationIdentity used). Built once
    per command; identical on every context; names only (strings)."""

    level: ScopeLevel
    system_slug: str | None = None  # the anchor; may be unset on a first-ever create
    vm: str | None = None
    workspace: str | None = None
    agent: str | None = None
    session: str | None = None
    admin: bool = False
```

The level-to-fields invariant the object enforces (`system_slug` is the anchor, allowed at every
level):

| level     | required beyond slug                                  | forbidden                     | used by                              |
| --------- | ----------------------------------------------------- | ----------------------------- | ------------------------------------ |
| SYSTEM    | (none)                                                | vm, workspace, agent, session | doctor; git-credential readiness     |
| VM        | vm                                                    | workspace, agent, session     | `vm create` / start / stop / gitcred |
| WORKSPACE | vm, workspace                                         | agent, session                | `workspace create`                   |
| AGENT     | vm, workspace, agent                                  | session                       | `agent create` / reinit              |
| SESSION   | vm, workspace, session; exactly one of (agent, admin) | (none)                        | `session create` / restart           |

The level is the operation's, one per command, NOT per node (my repeated mistake; corrected). Its
DEPTH varies by command, so a batch `keep_actives` over N VMs is only SYSTEM level with each VM's
identity coming from layer 1, and it is what a node reads to distinguish absences that a bare `None`
target cannot: the harness's required-commands check reads the level to tell "no agent because this
is a system-scoped doctor scan" (skip, legitimately) from "no agent yet, it is pending" (defer) from
"agent in scope but absent for another reason" (loud bug). Three outcomes, one explicit signal, the
same explicit-beats-inferred argument as `to_create`.

**Access to the power-granting world, shaped for a future gate but NOT gated in v1.** The runtime
handles that grant power (the execution targets, the resolved secrets) are reached through ACCESSOR
METHODS on the context, `ctx.agent_target()`, `ctx.admin_target()`, `ctx.secret(name)`, rather than
bare fields. In v1 they are plain pass-through: no per-node requester binding, no grant check, no
new machinery, they return what today's fields return. The ONLY thing the method shape buys now is
that the harness (consumer number one, in THIS effort) reads `ctx.agent_target()` from day one, so
when a permission model later gates by the requesting node it changes the orchestrator's plumbing,
not the node-facing signature. Everything about the gate itself, the per-node requester binding, the
grant check, the raise on an ungranted target, is DEFERRED to the plugin/trust SDD that needs it and
is explicitly NOT built here (a scope call the reviewer pushed on, 2026-07-17: it would be inert
machinery ahead of its consumer, and this effort's non-goals fence the plugin system). The context
stays merely SHAPED to admit it: methods, not fields. Scoped secret DELIVERY (R5, a node reads only
the secrets it declared) is the one piece with real v1 value, and it rides `ctx.secret(name)`
naturally; whether it ships in v1 or falls back to the whole-cache reader is the tracer's call (see
Secrets and the plan obligations below).

So `RunContext` becomes: `config` and the `OperationScope` as plain fields (world-independent,
ungated, uniform, doing real v1 work), plus plain accessor methods for targets and secrets
(pass-through in v1, the seam a later permission model gates behind). It is frozen and re-assembled
per stage as before. One carry for the harness re-scope, recorded so it is not lost: the real
harness target is agent-OR-admin, admin is always realized, so a `None` target still means a bug,
never a silent skip, and admin-mode sessions are covered by the SESSION level's `admin` flag rather
than by a missing agent.

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
  the materials-write ops of the provider instances held by its git-credential nodes, sequenced by
  whichever orchestrator is running; the session node's own realizing slice is just tmux plus its
  row. The reusable unit is therefore the PHASE-FREE realization choreography per creatable kind,
  factored as domain code and called by any orchestrator that creates that kind: `agent create`
  wraps it in its own phases, `session create --new-agent` calls the same body inside its phases.
  This is what dissolves today's nesting hack, where the nested `create_agent` is a full command
  root that must be handed `git_tokens` and phase suppression to stop it re-running resolve and
  banners: a body never resolves and never frames phases, by construction. The flag-flip is NAMED
  `mark_realized` (settled, maintainer ruling 2026-07-17; the spike's `realize()` spelling dies with
  the spike) precisely so it cannot be read as doing the work.

Two walkthroughs make it concrete.

**Use: `session restart`** (everything exists; no realization record at all):

1. BUILD: the session row names its agent, workspace, and VM; the domain factories construct live
   nodes from those rows (the VM's `site` field -> an edge to the `vm-site` node, which HOLDS the
   platform instance, per the derivation rule); session-template re-resolution by the stored name
   yields `(harness, harness_config)`, and the session NODE holds a harness INSTANCE (not a node),
   constructed with the session name and the live agent node as its target.
2. OPEN THE ACTIVATION GATE (a span held through the command): converge the VM's power state
   (maintenance; refuse if operator-stopped; just-in-time gate secrets if a start or rejoin is
   needed; hold active for platforms that require it) so the probes that follow query a live target.
3. PREFLIGHT-ALL over the walk rooted at the live session node: one command-start context at SESSION
   scope; the harness's target is realized, so required-commands probes NOW, pre-resolve and
   pre-kill; any failure aborts with the old session still running.
4. Command-shaped middle, exactly today's proven order: the BROKEN/confirm gates, then the resolve
   (restart's env-chain pass sits after its gates), then env composition.
5. OPS: kill (a session-node domain op), `harness.restart(ctx)` returns the pane command, tmux
   re-create. No `RealizationLog` exists because nothing is pending; there is nothing to unwind, and
   the post-kill non-rollbackable window stays pinned per command exactly as today.

**Create: `session create --new-agent`** (target nodes pending; realization drives the middle):

1. BUILD: live VM node from its row; PENDING workspace node; PENDING agent node (deps: its
   agent-template node, whose git-credential references become edges to the `git-credential` nodes
   that each HOLD a provider instance, plus the VM node); PENDING session node holding its harness
   INSTANCE (constructed with the CHOSEN session name and the pending agent node as target), with
   deps on agent, workspace, VM, and its resolved session-template node (exactly as the vm-template
   hangs off a pending VM). Names are chosen up front, so every node's identity is complete while it
   is still pending. The walk roots at the pending session node, the command's one target.
2. OPEN THE ACTIVATION GATE (span, held through the command): converge the existing VM's power state
   (maintenance; refuse if operator-stopped; just-in-time gate secrets if needed; hold active for
   platforms that require it), so preflight probes a live target.
3. PREFLIGHT-ALL, same one SESSION-scope context: predictions run for every declared secret; the
   harness's target is in scope (SESSION level) but pending, so it defers. Dependency-blindness is
   structural, not special-cased.
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

Doctor is a SYSTEM-scoped operation: a scan over ALL declared resources, not a command with a plan.
The operation scope makes it fit the model without special-casing. It builds no node graph and
shares the central prediction computation (step 2 above) over the full declared reference graph for
its resolvability rows; where it calls a node's `preflight` for a health row, it hands a
SYSTEM-level context, and each node shapes itself to that level. A harness's required-commands check
sees SYSTEM scope, notes it has no agent or workspace in scope, and SKIPS, exactly the "out of
scope, not a bug" outcome the level exists to express, and cleaner than a doctor-specific "which
checks apply" branch. So doctor's per-resource rows and a command's readiness call the SAME
preflight code; the level is what tells that code how much of the world to expect. No behavior
changes versus today (R2/R5 parity); `secrets.py`'s prediction helper takes declarations, not a
walk, so both a command's union and doctor's all-resources sweep are callers of the same function.

## Migration map

**Tracer bullet: `agw vm add-git-credential`.** Chosen because it is the smallest command with a
real graph and it exercises the two remaining unknowns: end-to-end edge derivation (live VM row ->
edge to the `vm-site` node holding the platform instance; credential name -> edge to the
`git-credential` node holding the provider instance; all from rows and declared references, zero
hand-wired edges) and a real runup with the FATAL policy, plus a real op (materials write) reading
scoped secrets. It has no pending nodes, which is fine: unwind landed in the spike and gets its
production proof in the next step.

Order after the tracer (FRD R8's double duty: de-risk AND crystallize helpers):

1. `vm add-git-credential` (tracer: derivation, fatal runup policy, scoped delivery)
2. `vm create` / `vm reinit` (pending nodes, unwind, skip-and-degrade policy, multi-capability
   graph)
3. `session create` including `--new-agent` (EARLY, the harness SDD's landing pad: the nested
   fan-out, the session node's held harness, restart's command-shaped ordering follows with
   `session restart`)
4. `agent create` / `agent reinit`
5. remaining commands opportunistically (`vm delete`, `vm start/stop`, shell/exec roots), each a
   green, shippable unit

**Proof points the plan must assign, because the spike could not** (reviewer carry, 2026-07-17). The
spike validated the graph walk, pending-node deferral, and unwind ORDER, but several
newest-and-un-spiked mechanisms land only across these steps, and the plan must name where each is
first proven against HEAD rather than letting it ride implicitly:

- **The level-driven SKIP branch** (out-of-scope-for-level): the spike has no level object, so this
  branch is unproven. First real exercise is a doctor scan reaching a harness at SYSTEM level (that
  path exists: doctor calls node preflight) or `session create`; the plan pins which and asserts the
  harness no-ops rather than erroring.
- **`OperationScope` on the context and the plain accessors**: un-spiked (the spike used bare
  fields). The tracer introduces the scope field and the accessor methods; the plan states the
  minimal introduction and an assertion that the operation scope reaches a node's readiness.
- **Scoped secret delivery** (a node receives only its declared names): the tracer's single `github`
  materials write may not stress multi-declaration scoping, so the plan carries an explicit
  acceptance test that a node is handed only its declared secrets, guarding against the whole-cache
  fallback quietly becoming the permanent shape.

Each migrated command adds the R7 parity assertion ("no prompt after the resolve boundary"), plus a
GATE-PROMPT parity assertion. The gate's timing legitimately SHIFTS (it moves from post-resolve to
pre-preflight, e.g. `add-git-credential` today opens `keep_active` only after its resolve and around
the write), so the assertion is not literal timing parity but the true invariant: exactly ONE prompt
session, entirely pre-walk-away, nothing resolved or prompted twice. Both keep the full suite green.
Interim seams (an orchestrated command calling not-yet-migrated machinery) are documented in
`plan.md` per FRD R8.

## Design decisions (recap of rulings, with the HLA's additions)

- **Protocol, not hierarchy; composition as edges** (FRD R1).
- **Readiness vs Node; capability instances are NOT nodes** (maintainer ruling, 2026-07-17,
  correcting an earlier HLA drift). Only consuming and live resources are nodes, so every graph key
  is a natural globally-unique name; a capability instance is a `Readiness`-only object HELD by a
  node and composed by that node's `preflight`/`runup`, never walked. This was forced by identity:
  an inline capability instance (an agent template's feature map) has no unique name of its own, so
  putting it on the graph would demand owner-qualified keys, the exact ugliness rejected for live
  resources. The shared `preflight`/`runup` verbs are kept (the readiness semantics are identical);
  the walked-vs-composed difference lives in the type (`Node` = `Readiness` + `key`/`deps`), which
  is what makes "why isn't this instance walked?" self-answering.
- **Bespoke orchestrators; helpers emerge** (FRD, 2026-07-17 ruling). The HLA adds: the
  `orchestration/` package is created lazily, tracer-first.
- **Secrets central; nodes declare and receive** (FRD R5). The HLA adds: the retirement sequencing
  and the scoped-delivery fallback.
- **Unwind and runup-failure policy are the orchestrator's; mechanism is the node's** (FRD R4). The
  HLA adds: the parity policy table and the skip-and-degrade helper as its first shared form.
- **Two-layer identity: intrinsic self plus operation scope** (FRD R3, revised 2026-07-17). Layer 1
  (a node's own `kind/name` + row-carried ancestors) is intrinsic and path-independent, which is
  what makes shared nodes well-defined under the diamond; layer 2 (the static, level-enforced
  `OperationScope`) rides on the context, uniform across the graph, read-what-you-need. This
  supersedes the earlier "scope-free context / identity by construction" framing. The key convention
  makes layer-1 identity operational (memoization, cycles, unwind); the level makes layer-2 scope
  actionable (the required-commands skip/defer/probe/error fork, doctor as SYSTEM scope).
- **Power-granting world reached through plain accessors; descriptive scope is a field** (maintainer
  ruling 2026-07-17, scoped by reviewer 2026-07-17). Targets and secrets are `ctx.agent_target()` /
  `ctx.admin_target()` / `ctx.secret()`, PASS-THROUGH in v1 (no requester binding, no gating); the
  method shape exists only so the harness (consumer one) is not rewritten when a later permission
  model gates behind it. The gate, the per-node binding, and enforcement are the plugin SDD's and
  are not built here. The operation scope is a plain ungated field because reading it grants no
  capability and it does real v1 work (the level fork, error labels).
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

- Final key spellings for the template rows of the key table (the live-resource and
  consuming-resource keys are settled: plain `kind/name` over globally unique names; the harness has
  no key, it is held by the session node).
- Held-instance COMPOSITION mechanism: whether a node fans into its held instances via one-line
  per-kind boilerplate or a shared held-instances hook/convention on the node interface (neither
  `Readiness` nor `Node` exposes a held-instances accessor today). The same decision governs
  `secret_refs` aggregation over a map of held instances. Pin at LLD before the second and third
  consuming-resource kinds land, so they do not each re-implement the fan-in differently.
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
- `OperationScope` exact per-level field rules and `__post_init__` enforcement (the table pins the
  contract; the LLD pins the asserts), and where each command builds its scope (one site per
  orchestrator, at entry). The full five-level enum is defined once up front (a cheap contract), but
  the tracer and step 2 construct only SYSTEM and VM levels, so WORKSPACE / AGENT / SESSION are
  exercised as their commands migrate; the LLD confirms nothing dead ships early.
- The `RunContext` field-to-accessor migration: turning `admin_target` / `agent_target` / `secrets`
  into PLAIN `ctx.agent_target()` / `ctx.admin_target()` / `ctx.secret()` methods (pass-through, no
  requester binding, no gating; those are the plugin/trust SDD's, see the context section) and
  whether v1 flips all three at once. The spike used bare fields, so this and
  operation-scope-on-context are UN-spiked; the tracer is their first exercise.
