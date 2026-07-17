# Orchestration layer: functional requirements

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

## Background

The capability model (documented in `cli/agentworks/capabilities/README.md`, shipped by the
resource-manifests SDD and proven on `vm-platform` in PR 169 and `git-credential-provider` in
PR 167) gave Agentworks a real runtime lifecycle: `validate_config` (pure, declares references) ->
construct (binds config) -> `preflight` (pre-resolve, dependency-blind, read-only) -> `runup`
(post-resolve, authenticated, read-only) -> ops (the mutation phase). PR #182 then pinned the secret
contract that keeps a capability forward-compatible: declare secrets purely in `validate_config`,
receive resolved values only from the run context.

That model is good, and this SDD does not replace it. What this SDD addresses is everything AROUND
it, the composition. Four observations about the code at HEAD motivate the effort:

1. **The lifecycle lives only on capability instances.** `preflight` / `runup` exist on
   `capabilities.base.Capability` and its two subclasses, and nowhere else. Readiness for everything
   else leaked out as loose code: `preflight_vm_template` is a free function (`vms/templates.py`),
   and the session's required-commands check is a free function inline in the sessions manager
   (`_assert_required_commands`). The one object carrying the formal lifecycle is the one thing the
   model does not call a resource, while the things it does call resources have their readiness
   scattered.

2. **Composition is hand-rolled per command.** Each service-layer root re-authors the same
   orchestration by hand: `bind_platform` / `bind_platforms` order construct -> preflight ->
   resolve; `create_vm` interleaves the vm-template's and the providers' preflights before the one
   resolve; `create_session` hand-rolls the deepest case, constructing the ephemeral agent's
   git-credential providers itself, preflighting them, folding their secrets into the single
   boundary resolve, and threading the resolved values down so the nested `create_agent` skips its
   own resolve. Every one of these is correct and tested. Every one of them is bespoke. Each new
   command, and each new capability, adds another.

3. **Secret machinery is entangled with the resources.** The per-instance bound resolver does three
   jobs (construct-time registration, preflight-time resolvability prediction, and, residually, an
   op-time value source), and the orchestration-shaped parts of those jobs (what is the union of
   secrets this command needs? has the resolve pass run?) are smeared across instance construction
   sites rather than owned in one place.

4. **The next capability had to invent orchestration primitives to ship.** The in-flight
   session-harness SDD (`docs/sdd/2026-07-07-session-harness`, PR #168) needed a per-invocation
   identity (`OperationIdentity` keyed by a scope `level`, threaded as a required field through all
   fourteen `RunContext` construction sites) and an explicit existence signal (`to_create`, the set
   of entities the command will create that do not exist yet) to make one capability's readiness
   correct. Both are, in substance, slices of a command PLAN: what does this invocation concern, and
   what has been realized so far. The harness work discovering them is evidence they are real;
   fixing them one consumer at a time on the context object is the interim model hardening into
   permanence.

The cost of waiting is the maintainer's stated concern (ruling, 2026-07-16): every additional
capability built on the interim model adds hand-rolled composition that a future orchestration layer
must reproduce exactly, and the will to take the refactor decays with each one. This SDD is
scheduled now, ahead of further capabilities, for exactly that reason. The session harness re-scopes
to be this model's first consumer (see R10).

### The model change

Today the model reads: each service-layer command imperatively composes its resources (constructs
instances, orders preflights, runs the one resolve, drives runups and ops, and hand-rolls rollback).
After this SDD it reads:

> **A command is a plan over a graph of nodes.** The orchestration layer owns the plan: which nodes
> participate, the dependency edges between them, the readiness phases, the single secret resolve,
> and the staged execution that follows. Every node exposes the same lifecycle through one thin
> protocol, and receives everything it needs through the context. Nodes never resolve secrets, never
> walk the graph, and never order phases.

The split gives each side a crisp job. The orchestration layer (the top of the current service
layer, entered through the current CLI-shaped APIs) owns traversal, phase order, secret resolution,
and context assembly. Resources own their OWN readiness, their own ops, and the declaration of their
own dependencies, nothing else.

What deliberately does NOT change: the ops and their choreography. The per-resource mutation
sequences (clone + configure + start for a VM; user + home + store for an agent), the rollback
discipline, and the operator-facing order (prompts up front, then walk away) are proven, tested
behavior. This SDD re-homes the composition around them; it does not redesign them (R7).

### Terminology

- **Node**: a runtime, lifecycle-bearing object the orchestrator constructs and walks. Nodes are not
  operator-facing and are not registry rows. Capability instances, live resources, and
  readiness-bearing resolved templates are all nodes.
- **Registry resource**: unchanged. The operator-facing, named, `agw resource list`-visible recipe:
  templates, decls, capability rows. Registry resources are data; the orchestrator constructs nodes
  from them.
- **Live resource**: a node representing an instance that exists in the installation, distinct from
  the template it was made from: a VM, a workspace, an agent, a session, a console. Today these
  exist only as DB rows plus scattered manager logic; this SDD makes them first-class nodes.
- **Pending node**: a live resource the current command will create. It participates in the plan
  (identity, dependencies, readiness contributions) before it exists in the world, and is marked
  realized when its mutation completes. Pending-ness is a property of the node in the plan, not a
  field threaded through contexts.
- **Plan**: the orchestrator's product for one command: the participating nodes, their edges, and
  the phase order over them.
- **Context**: the per-invocation object a node's lifecycle stage receives (the evolution of today's
  `RunContext`): global config, execution targets, resolved secrets, and whatever else the stage's
  timing makes available. Assembled by the orchestrator, updated as the plan advances.

## Requirements

### R1: One lifecycle, on every node, through a thin protocol

- Every node exposes the same lifecycle surface: `preflight` (pre-resolve, dependency-blind,
  read-only), `runup` (post-resolve, read-only, deferred to just before the node's first use), and
  its domain ops. The preflight/runup semantics are the capability model's, unchanged; what changes
  is WHO has them (every node, not just capability instances).
- The shared surface is a **protocol (structural typing), not an inheritance hierarchy** (maintainer
  ruling, 2026-07-16). A thin `Node` protocol pins the lifecycle shape; node kinds implement it
  directly. There is no deep class tree, and composition (a session is made of a harness, an agent,
  a workspace, a VM) is expressed as graph edges, never as inheritance.
- Either readiness stage may be a no-op for a given node kind, exactly as the capability model
  already allows. A vm-template's preflight predicts its Tailscale key; a live, already-running VM's
  preflight may be empty.
- The readiness code that currently lives outside the lifecycle moves onto it: the free function
  `preflight_vm_template` becomes the vm-template node's preflight; the sessions manager's
  `_assert_required_commands` becomes harness readiness (per the harness SDD, which already
  relocates it); any future readiness check has exactly one home.

### R2: Nodes and registry resources are distinct, and the operator surface does not change

- The registry (kinds, categories, origins, references, `agw resource list` / `describe` / `kinds`,
  doctor's per-resource rows) is untouched as an operator surface. Registry resources remain the
  declared recipes.
- Nodes are internal runtime objects. No new registry category is added for them, and nothing about
  them is operator-visible. (The alternative, promoting capability instances to registry resources,
  was considered and rejected: an instance needs a lifecycle, not a name, a list row, or operator
  visibility. Ruling, 2026-07-16.)
- The existing relationship stands: a registry resource is data the orchestrator constructs a node
  from. One registry resource may give rise to one node per command (a vm-site's platform instance),
  and one node may exist with no registry resource of its own (a live VM).

### R3: Live resources are first-class nodes, including pending ones

- VM, workspace, agent, session, and console instances are nodes, distinct from their templates,
  constructed from their DB rows plus their bound runtime machinery (a live VM node wraps its row
  and its bound platform instance).
- A live node knows its own identity: its own name and its ancestors' names (a live agent knows its
  VM, its workspace context, its agent name). **Identity is intrinsic to the node**, which is what
  removes the need to thread a separate identity object through every context construction site
  (supersedes the harness SDD's `OperationIdentity` threading; see R10).
- A command that creates an instance places a **pending node** for it in the plan up front, with its
  names chosen and its dependencies attached. Existence is a queryable property of the node (pending
  vs realized), updated by the orchestrator when the realizing mutation completes.
- Pending-ness answers the deferral question explicitly and safely: a readiness check that needs a
  target probes it when the target's node is realized and defers when it is pending. A target that
  is missing for any OTHER reason (a selection bug, a permission gap) is a loud error, never a
  silent skip. This preserves the harness SDD's anti-silent-skip property while dissolving its
  `to_create` context field: the plan itself carries the information.

### R4: The orchestrator owns traversal; nodes declare, never walk

- Nodes **declare** their dependencies (the existing reference machinery: `validate_config` implied
  references, `referenced_resources()`, plus the live-resource edges the plan adds). Nodes never
  call other nodes' lifecycle stages.
- The orchestrator walks the declared graph: memoized (a node shared by two consumers, a vm-site
  under two agents, is visited once), deduplicated, cycle-checked, deterministic. Traversal logic
  exists in exactly one place.
- A command's entry point names only the resources it uses DIRECTLY (the session template, the
  target workspace and agent, the VM). Everything transitive (the agent template's git credentials,
  the site's platform, their secrets) enters the plan through declared edges, not through the
  command knowing about them. This replaces the hand-rolled fan-outs of observation 2, including
  `create_session`'s bespoke ephemeral-agent fold, with one mechanism.
- The canonical phase order is preserved and becomes the orchestrator's to enforce, once, instead of
  each root's to re-author: preflight-all (before any prompt or mutation) -> the single secret
  resolve at the preflight boundary -> staged execution, with each node's runup deferred to just
  before its first use, and context updated as realization proceeds.

### R5: The orchestrator owns secrets end to end

- Secret RESOLUTION is exclusively the orchestrator's: it derives the union of declared secrets
  across the plan's nodes, runs the single resolve pass at the preflight boundary (one prompt
  session, walk-away point unchanged), and delivers values to nodes through the context.
- Nodes follow the declare/receive contract already documented in `capabilities/README.md`,
  completed: declare in `validate_config` (pure), receive from the context in runup AND ops. The
  residual op-time bridge (`proxmox`'s op client reading through its bound resolver) is retired as
  part of this SDD, along with the per-instance bound resolver itself: construct-time registration
  and preflight-time resolvability prediction move to the orchestrator, which owns the graph and can
  predict over it centrally.
- Resolvability prediction (doctor's and preflight's "is this secret mapped at all?") remains a
  pre-resolve, non-prompting property with the same operator-facing behavior; it is computed from
  the plan rather than by each instance.
- Delivery may be scoped: a node receives the secrets it declared, not the command-wide mapping.
  (Exact filtering semantics are an HLA decision; the requirement is that nothing about delivery
  reintroduces node-side resolution.)

### R6: The context is rich, orchestrator-assembled, and stage-accurate

- The context (evolving today's `RunContext`) is assembled by the orchestrator per node invocation
  and reflects the plan's current reality: at preflight, command-start reality (existing targets
  only, no secrets); at runup and ops, current reality (realized targets, resolved secrets).
- Because identity is intrinsic to nodes (R3) and pending-ness lives in the plan (R3), the context
  does not carry a threaded identity object or a `to_create` set. What it carries is the runtime
  world: config, execution targets, secrets, exactly the fields that are timing-dependent.
- The context's contract with capability authors (the declare/receive rules, the self-vs-context
  split) is preserved; existing capability implementations keep working against it with at most
  mechanical adjustment (R7, R9).

### R7: Behavior parity, with the existing suite as the oracle

- This effort preserves current functionality. The full existing test suite is the oracle: it
  passes, unmodified in intent, at every merge point. Tests may be mechanically adjusted where they
  reach into internals that move; behavioral assertions do not change.
- Operator-visible behavior is pinned: command output (the phase banners: Preflight, Resolving
  Secrets, Provisioning, the per-resource `Checking <kind>/<name>...` lines), prompt timing
  (prompt-once at the preflight boundary, walk-away after), error types and shapes, rollback
  semantics (best-effort unwind, `UserAbort` never swallowed), and partial-failure degradation (a
  rejected credential skips, the command degrades to partial) all behave as they do at HEAD.
- The ops and their internal choreography (each resource's mutation sequence, its idempotency
  contract, its rollback) are reused as-is. Where today's code interleaves phases for good reason
  (restart's env-chain resolution after its confirm gates), the plan expresses that command's proven
  order rather than flattening it into a single template.

### R8: Migration is incremental, command by command, always green

- The old (imperative roots) and new (orchestrated) composition coexist during migration. Commands
  are migrated one at a time; each migration is a complete, green, shippable unit.
- No big-bang cutover exists anywhere in the plan. The effort is pausable and resumable at every
  command boundary, deliberately, so it survives interruption by higher-priority work.
- The migration order is chosen to de-risk: the first migrated command is a thin vertical slice (the
  tracer bullet), chosen at HLA time; `vm create` and `session create --new-agent` (the spike
  scenarios, R11) are the reference targets that prove generality.
- The interim seams are explicit: while both models coexist, the boundary between an orchestrated
  command and the not-yet-migrated machinery it calls into is documented in the plan, and no
  migrated command regresses to hand-rolled composition to work around a seam.

### R9: The capability model is realigned, not replaced

- Capability instances implement the node protocol. Their lifecycle semantics (the preflight/runup
  boundary, read-only readiness, ops as the mutation phase, idempotency flags) are unchanged; their
  construction loses the bound resolver (R5) and their ops converge on reading the context,
  completing the direction PR #182 documented.
- `capabilities/README.md` is rewritten to teach the realigned model (the node protocol, the
  orchestrator's ownership of traversal and secrets, the completed declare/receive contract), with
  each doc change riding the commit that makes it true.
- Capability AUTHORS get a strictly simpler contract out of this: declare config and references
  purely, implement your own readiness and ops against the context, and never touch a resolver,
  another node's lifecycle, or phase order. That contract is the stable API future capabilities (and
  plugin-registered ones) are written against.

### R10: The session harness is this model's first consumer

- The session-harness SDD (PR #168) re-scopes to build the harness as the first capability on the
  orchestrated model, and its dev is a key reviewer of this SDD (maintainer decision, 2026-07-16).
- Specifically superseded by this model, with the harness SDD updated accordingly rather than
  shipping both designs: the required `OperationIdentity` / `level` object and its threading through
  the fourteen `RunContext` construction sites (identity becomes intrinsic to nodes, R3); the
  `to_create` context field (pending-ness lives in the plan, R3). The harness SDD's requirements
  they served (session-name addressing, probe deferral for ephemerals, the anti-silent-skip
  property) are requirements on THIS model and are called out in R3.
- The harness SDD's domain content (the harness contract, `shell` and `claude-code`, the template
  surface, inheritance semantics, the tmux invariant) is untouched by this SDD and remains that
  effort's to deliver.

### R11: A spike gates the HLA

- Before the HLA hardens, a throwaway code spike validates the load-bearing bets on real types
  (maintainer ruling on scope, 2026-07-16). The spike implements: the `Node` protocol; nodes for the
  dissimilar stress cases (a live VM wrapping its row and platform, a resolved vm-template absorbing
  `preflight_vm_template`, a pending live agent, a stub harness that defers on the pending agent);
  and a minimal memoized walker over declared dependencies.
- The spike's scenarios are `vm create` and `session create --new-agent`, chosen because together
  they exercise every node species: templates with real readiness, capability instances (platform,
  git credentials) entering as ordinary dependency nodes, existing live resources, pending live
  resources, and the nested secret fold. For each scenario the spike asserts, against the current
  imperative code's behavior: the preflight set, the deferral behavior on pending nodes, and the
  union of secrets the resolve pass would cover.
- The spike answers three questions: does one thin protocol fit dissimilar nodes without contortion;
  does identity-intrinsic-to-nodes hold at construction time; does a memoized walk over declared
  edges reproduce the hand-rolled fan-outs. Findings land in `spike-findings.md` in this feature
  directory, and the HLA cites them.
- The spike touches nothing under `agentworks/` (no production code, no wiring into commands) and is
  discarded or kept as reference in this feature directory. It is explicitly NOT the tracer bullet:
  migrating a real command is the plan's first implementation phase, after FRD/HLA review.

### R12: Documentation and decision record

- The permanent docs change in lockstep with the code that makes them true (the SDD lifecycle rule):
  `capabilities/README.md` (R9), the architecture narrative wherever the composition story is told,
  and the capability-author guidance.
- The decision to split orchestration from resources, with the node/registry-resource distinction
  and the protocol-not-hierarchy ruling, is recorded as an ADR, drafted unnumbered in this feature
  directory and promoted into `docs/adrs/` at the end of the effort.
- This SDD's artifacts follow the standard set: this FRD, `hla.md` (after the spike),
  `spike-findings.md`, `prior-art-research.md` (graph-walking orchestrators in adjacent tools:
  Terraform's resource graph, Kubernetes reconciliation, systemd ordering; scoped to what informs
  the walker and pending-node design), `plan.md`, and the ADR draft.

## Non-goals

- **A declarative provisioning engine.** The executor is not a DSL and the ops are not data. Each
  resource's mutation choreography stays authored, imperative code; the orchestrator sequences WHEN
  nodes act, never HOW a node does its work. Anyone reading this SDD as "Terraform inside
  Agentworks" has misread it.
- **Operator surface changes.** No new commands, flags, config keys, or resource kinds; no changes
  to `agw resource` output or doctor rows beyond parity. An operator upgrading across this effort
  should notice nothing.
- **The harness domain itself.** R10 bounds the interaction; the harness's contract and built-ins
  are the harness SDD's.
- **The plugin system.** This model is deliberately the substrate plugins will target (a plugin
  capability is just another node kind), but plugin loading, trust, and registration remain the
  plugin SDD's.
- **Doctor enhancements** (e.g. `doctor --runup`) beyond keeping today's behavior working.
- **Performance work.** The walker must not make commands meaningfully slower, but no latency
  improvements are promised or pursued here.
- **Retiring the TOML dual path** or any other resource-manifests Phase 6 work.

## Migration notes

Nothing operator-visible. Config files, manifests, CLI invocations, output, and prompts are
unchanged. The effort is internal re-composition under a behavior-parity requirement (R7), landed
command by command (R8).
