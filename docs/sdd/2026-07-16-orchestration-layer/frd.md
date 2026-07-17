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

> **A command is a plan over a graph of nodes, and each command's orchestrator owns its plan**:
> which nodes participate, the dependency edges between them, the readiness phases, the single
> secret resolve, the sequencing of the command's roll-forward, and the roll-back when it fails.
> Every node exposes the same lifecycle through one thin protocol, and receives everything it needs
> through the context. Nodes never resolve secrets, never walk the graph, never order phases, and
> operate only within their own scope.

Orchestrators are PLURAL and BESPOKE (maintainer ruling, 2026-07-17): one per command, at the top of
the current service layer, entered through the current CLI-shaped APIs. Writing a new command IS
writing its orchestrator. What this SDD standardizes is not an engine that runs commands but the
separation of responsibilities and the interface between the orchestrators and the nodes they drive:
orchestrators own traversal, phase order, secret resolution, context assembly, and unwind; nodes own
their OWN readiness, their own ops (including their own teardown), and the declaration of their own
dependencies, nothing else. An orchestrator is just a layer of code this SDD defines and gives a
name: no required shape, no mandated plan artifact, no lifecycle of its own. Constraining or
facilitating that bespoke logic beyond the responsibility split is explicitly out of scope; an
engine general enough to cover all our ops is the rabbit hole this SDD refuses (see Non-goals).
Shared helpers around dependency resolution, the preflight sweep, secret resolution, and unwind
(possibly a slim shared base) are expected to EMERGE and are factored out as they prove themselves
across commands, not designed up front.

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
- **Orchestrator**: the authored, command-specific composition code at the top of the service layer.
  One per command; plural by design; just code. This SDD names the layer and assigns its
  responsibilities; it deliberately does not prescribe the code's structure.
- **Plan**: shorthand for what an orchestrator has decided to do: the nodes it will touch (including
  pending ones), their edges, and what it has realized so far. Vocabulary, not an artifact: this SDD
  mandates no plan object, and whether an orchestrator materializes one is its own business.
- **Context**: the per-invocation object a node's lifecycle stage receives (the evolution of today's
  `RunContext`): global config, execution targets, resolved secrets, and whatever else the stage's
  timing makes available. Assembled by the orchestrator per stage and frozen: advancing reality
  means a new context for the next stage, never mutation of one a node already holds.

## Requirements

### R1: One lifecycle, on every node, through a thin protocol

- Every node exposes the same READINESS surface: `preflight` (pre-resolve, dependency-blind,
  read-only) and `runup` (post-resolve, read-only, deferred to just before the node's first use),
  plus its dependency declaration. The preflight/runup semantics are the capability model's,
  unchanged; what changes is WHO has them (every node, not just capability instances).
- The protocol pins readiness and dependency declaration ONLY. Ops stay domain-specific and are
  deliberately NOT unified (`start() -> str`, `create()/destroy()`, and credential-materials writes
  share nothing); the capability README's "these belong to the subclass, do not try to unify them"
  carries over verbatim. A node kind that a command can create also exposes its own TEARDOWN op
  (delete what I made), the node-scope half of unwind (R4).
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
  constructed from their DB rows. Their runtime machinery enters as graph edges, consistent with R1
  (a live VM node holds its row; its platform instance is a dependency node, not a contained field).
- A live node knows its own identity: its own name and its ancestors' names (a live agent knows its
  VM, its workspace context, its agent name). **Identity is intrinsic to the node**, which is what
  removes the need to thread a separate identity object through every context construction site
  (supersedes the harness SDD's `OperationIdentity` threading; see R10).
- Identity travels by CONSTRUCTION, all the way down (maintainer discussion, 2026-07-17): whoever
  constructs a node hands it what it belongs to, as ordinary constructor arguments, and the
  constructor is always something that already holds that data (the session's node factory builds
  its harness with the session name and target agent node it planned with; `GitCredentialProvider`
  already gets its `owner_name` the same way today). No injection mechanism and no orchestrator
  reach-down into deep nodes exists; construction is compositional, and it is exactly what keeps the
  context SCOPE-FREE (R6). Nodes never walk the graph to discover identity (R4); the spike proves
  the delivery on the stub harness (R11).
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
- An orchestrator walks the declared graph: memoized (a node shared by two consumers, a vm-site
  under two agents, is visited once), deduplicated, cycle-checked, deterministic. The traversal
  MECHANISM is a shared helper implemented once and used by every orchestrator; which nodes to root
  the walk at, and when, is each orchestrator's call.
- A command's entry point names only the resources it uses DIRECTLY (the session template, the
  target workspace and agent, the VM). Everything transitive (the agent template's git credentials,
  the site's platform, their secrets) enters the plan through declared edges, not through the
  command knowing about them. This replaces the hand-rolled fan-outs of observation 2, including
  `create_session`'s bespoke ephemeral-agent fold, with one mechanism.
- The phase INVARIANT every orchestrator upholds is exactly the walk-away boundary: preflight-all
  (before any prompt or mutation) -> the single secret resolve. Everything after the resolve
  (mutation order, runup timing against "just before first use", interleavings like restart's
  env-chain resolution after its confirm gates) is command-shaped, authored in that command's
  orchestrator; the invariant is a discipline the shared helpers make easy, not an engine-enforced
  template. Power-state convergence by the ACTIVATION GATE (an existing VM auto-started after build
  and before the sweep, so readiness probes a live target) is MAINTENANCE, idempotent declared-state
  convergence that is never rollback-tracked, not a plan mutation; it does not bend "before any
  mutation," and its narrow just-in-time secrets are R5's sanctioned exception (maintainer ruling,
  2026-07-17). The gate is the orchestrator driving the live VM node's own power-state ops (`status`
  / `start` / a held-active span), NOT a new protocol stage and NOT a preflight side effect
  (preflight is read-only). It is a SPAN held through the command and closed at the end (some
  platforms, WSL2 today, must be HELD active, not merely started), and the VM node is the authority
  on whether it may auto-start: auto-start applies only to an auto-stopped VM, and a manually
  stopped one (`operator_stopped`, set by `vm stop`, cleared by `vm start`) refuses with a typed
  error at the gate.
- Roll-BACK is the orchestrator's too (maintainer ruling, 2026-07-17): the orchestrator knows what
  it has realized, and unwind is that record read backwards (tear down the realized nodes, reverse
  realization order), invoked with today's discipline (best-effort, a failed teardown warns and
  never masks the original error, `UserAbort` re-raised, never swallowed). Nodes contribute only
  their own teardown op (R1). Non-rollbackable windows (restart past its kill) remain explicitly
  pinned per command, exactly as the harness SDD pins its one.
- Runup FAILURE POLICY (continue vs abort) is likewise the orchestrator's (spike-review carry,
  2026-07-17): runup's own contract stays narrow (raise typed on definitive rejection; warn inside
  the node on network indeterminacy), and each command's orchestrator decides skip-and-degrade vs
  fatal, exactly as the capability README already assigns to "the caller." Today's per-caller
  policies (a rejected credential skips and degrades provisioning to partial but is fatal in
  `vm add-git-credential`; `[defaults] runup_git_credentials = false` skips the stage) are the
  parity targets.

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
  pre-resolve, non-prompting property with the same operator-facing behavior. It is computed
  centrally from the DECLARED REFERENCE GRAPH, not by each instance; a command's plan and doctor's
  all-resources scan are two views over the same computation, which is how doctor keeps its
  per-resource rows without needing a command plan.
- Delivery may be scoped: a node receives the secrets it declared, not the command-wide mapping.
  (Exact filtering semantics are an HLA decision; the requirement is that nothing about delivery
  reintroduces node-side resolution.)
- The ACTIVATION GATE is the one sanctioned resolution outside the boundary pass (maintainer ruling,
  2026-07-17): converging an existing VM's power state before preflight may need the platform's API
  credential (the common case: observing and starting a stopped VM) or the Tailscale auth key (the
  repair case: a lapsed tailnet registration must rejoin before the VM is reachable). Both resolve
  just-in-time through the same backend chain, orchestrator-owned, before the boundary;
  gate-resolved values SEED the boundary pass so no secret resolves or prompts twice in one command;
  the interactivity precedes the walk-away point (R7's assertion is untouched); and the rejoin path
  carries messaging encouraging REUSABLE auth keys, since a non-reusable key turns every restart
  into a rejoin problem.

### R6: The context is rich, orchestrator-assembled, and stage-accurate

- The context (evolving today's `RunContext`) is assembled by the orchestrator per node invocation
  and reflects current reality: at preflight, command-start reality (existing targets only, no
  secrets); at runup and ops, op-start reality (realized targets, resolved secrets). Contexts are
  FROZEN, like today's `RunContext`: advancing reality means assembling a new context for the next
  stage, never mutating one a node already holds.
- Because identity is intrinsic to nodes (R3) and pending-ness lives in the plan (R3), the context
  does not carry a threaded identity object or a `to_create` set. What it carries is the runtime
  world: config, execution targets, secrets, exactly the fields that are timing-dependent. The
  context is therefore SCOPE-FREE, which is what makes it passable AS-IS: one frozen command-start
  context serves the entire preflight sweep, because nothing in it is specific to any node
  (maintainer discussion, 2026-07-17; contexts still vary by TIMING, never by scope).
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
- The walk-away invariant is oracle-guarded EXPLICITLY, not structurally: because orchestrators are
  bespoke, nothing in the code's shape prevents a future orchestrator from prompting late, so for
  every migrated command the parity suite carries a "no prompt after the resolve boundary" assertion
  (first-consumer review note, 2026-07-17). Discipline plus helpers plus tests is the chosen trade;
  the tests are the part that makes it durable.
- The ops and their internal choreography (each resource's mutation sequence, its idempotency
  contract, its own teardown) are reused as-is. Unwind SEQUENCING moves to the orchestrator (R4)
  with identical operator-visible semantics; the per-node teardown code it invokes is today's,
  relocated onto the node. Where today's code interleaves phases for good reason (restart's
  env-chain resolution after its confirm gates), that command's orchestrator expresses its proven
  order rather than flattening it into a single template.

### R8: Migration is incremental, command by command, always green

- The old (imperative roots) and new (orchestrated) composition coexist during migration. Commands
  are migrated one at a time; each migration is a complete, green, shippable unit.
- No big-bang cutover exists anywhere in the plan. The effort is pausable and resumable at every
  command boundary, deliberately, so it survives interruption by higher-priority work.
- The migration order does DOUBLE DUTY (first-consumer review note, 2026-07-17): it de-risks, and it
  is where the shared helpers crystallize, because helpers emerge from repetition rather than
  up-front design, the early commands are the ones that force their shapes. The first migrated
  command is a thin vertical slice (the tracer bullet), chosen at HLA time, and it must include
  END-TO-END EDGE DERIVATION: its node graph derives from real declared references and DB rows, not
  hand-wired edges, closing the one gap the spike deliberately left (spike-review carry,
  2026-07-17). `vm create` and `session create --new-agent` (the spike scenarios, R11) are the
  reference targets that prove generality, and the latter's fan-out is precisely what forces the
  walker, the secret-union sweep, and the unwind skeleton to earn their shapes. `session create` is
  an EARLY migrated command, not a late one: the re-scoped harness SDD (R10) lands on it, and this
  SDD owes its first consumer a landing pad rather than a long wait behind other commands.
- Node kinds are introduced lazily, per migrated command, rather than standing up all five
  live-resource kinds in the tracer bullet.
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
  dissimilar stress cases (a live VM with its platform instance as a dependency edge, a resolved
  vm-template absorbing `preflight_vm_template`, a pending live agent, a stub harness that defers on
  the pending agent and consumes its injected session identity); and a minimal memoized walker over
  declared dependencies.
- The spike's scenarios are `vm create` and `session create --new-agent`, chosen because together
  they exercise every node species: templates with real readiness, capability instances (platform,
  git credentials) entering as ordinary dependency nodes, existing live resources, pending live
  resources, and the nested secret fold. For each scenario the spike asserts, against the current
  imperative code's behavior: the preflight set, the deferral behavior on pending nodes, the union
  of secrets the resolve pass would cover, and the unwind set and order a failure injected at each
  phase would produce (matching today's hand-rolled rollback).
- The spike answers four questions: does one thin protocol fit dissimilar nodes without contortion;
  does identity-intrinsic-to-nodes (plus orchestrator injection for leaf nodes) hold at construction
  time; does a memoized walk over declared edges reproduce the hand-rolled fan-outs; does
  realization-order unwind reproduce today's rollback. Findings land in `spike-findings.md` in this
  feature directory, and the HLA cites them.
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
  the walker and pending-node design), `plan.md`, and the ADR draft. The HLA explicitly pins the
  reference-graph-to-node-graph translation rule (one registry resource to many nodes; nodes with no
  registry resource of their own), the subtlest part of the walker's input. The HLA also pins the
  MINIMAL SHARED STATE the walker and unwind helpers operate on (what is in the walk, what is
  realized so far): unwind-read-backwards needs a record to read, and a shared helper needs an
  agreed shape for it, so "plan is vocabulary, not an artifact" is reconciled there with helpers
  that carry the smallest record that works (first-consumer review note, 2026-07-17). Three further
  HLA obligations from the spike review (2026-07-17): show the orchestrator's runup driving
  reproducing today's per-caller continue-vs-abort policies (R4); pin the node-key namespace across
  the full node-kind set (the spike keyed its stand-in session as `vm/s1`, a real collision hazard);
  and state how scoped secret delivery (R5) gets proven, since the spike did not exercise it.

## Non-goals

- **A declarative provisioning engine.** The executor is not a DSL and the ops are not data. Each
  resource's mutation choreography stays authored, imperative code; the orchestrator sequences WHEN
  nodes act, never HOW a node does its work. Anyone reading this SDD as "Terraform inside
  Agentworks" has misread it.
- **A generic orchestration engine.** Orchestrators are bespoke, one per command; an engine general
  enough to cover all our ops is a rabbit hole this SDD deliberately refuses (maintainer ruling,
  2026-07-17). Shared helpers are factored out opportunistically as repetition proves them, never
  designed up front as a framework commands plug into.
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
