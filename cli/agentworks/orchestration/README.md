# The Orchestration Layer

This document is the working guide to the Agentworks orchestration layer
(`cli/agentworks/orchestration/`): the code that turns a service-layer command into a walk over a
graph of nodes, resolves that command's secrets exactly once, checks readiness in the right order,
and unwinds cleanly on failure. Every resource involved in an orchestrated operation participates in
this layer, whether or not it holds a capability. This is where the framework's command-driving
behavior lives, distinct from the per-capability author contract described in
[`../capabilities/README.md`](../capabilities/README.md).

The decision record for the layer is
[ADR 0019](../../../docs/adrs/0019-orchestration-layer-command-plans-over-node-graphs.md): why a
command is a plan over a node graph driven by a bespoke orchestrator (one authored composition per
command, no engine and no declarative plan artifact), and what alternatives were weighed. Read the
ADR when you want the rationale and the roads not taken. This guide covers how the pieces work in
practice, and every claim here is grounded in the code under `orchestration/`.

## The Node Graph Model

The layer rests on two complementary contracts, both in `node.py`, and the distinction between them
is the whole game:

- **`Readiness`** is just two verbs: `preflight` (pre-resolve, dependency-blind, read-only) and
  `runup` (post-resolve, authenticated, read-only, deferred to just before the ops it gates). A
  thing that satisfies `Readiness` can be checked, and nothing more.
- **`Node`** is `Readiness` plus graph identity: a `key`, its declared `deps`, and its declared
  `secret_refs`. A node is what the orchestrator walks.

A capability instance satisfies `Readiness` and stops there: it has no key and declares no
dependencies, so it is structurally not a node and the orchestrator never walks or invokes it
directly. The nodes are the consuming resources (a `vm-site`, a `git-credential`), the
readiness-bearing resolved templates (a `vm-template`, whose declared auth-key secret is its
readiness), and the live resources (a VM, a workspace, an agent, a session, whether existing or
pending). A capability instance is instead **held** by a node, and its readiness is **composed** by
that holder. The verbs are shared deliberately: the readiness semantics are identical, so what
separates an instance from a node is walked-versus-composed, and that lives in the type (the
presence of `key` / `deps`), never in a renamed verb.

Composition is a one-line fan-in in the thin case and richer in the rich case. A thin consuming
resource (a `vm-site` over its platform, a `git-credential` over its provider) names one capability
plus a config blob and has no behavior of its own, so its `preflight` / `runup` is exactly the held
instance's, forwarded. A rich node (a session over its harness integration) has substantial behavior
of its own and holds one or more instances, so its `preflight` / `runup` runs its own checks and
fans into the instances' as well. Either way the node folds its held instances' declared secrets
into its own surface: the bare-name union through `secret_refs` (the resolve union: every name this
node's readiness and ops consume, whatever the origin), and the richer references through
`config_secret_refs` (only the references that came from a consuming resource's declared config,
each carrying the `usage` prose that lets an operator-facing error say what the secret is for). The
two surfaces answer different questions and are not redundant; the preflight sweep predicts
resolvability over exactly the `config_secret_refs` set, which is why that type has to survive
alongside the bare names.

Keeping the instance a distinct object even in the thin one-to-one case is what lets thin and rich
share one model instead of forking, and it is what keeps instances off the graph: an inline instance
(one entry of an agent template's feature map) has no globally-unique name to key a node by, while
its holder does. Keys are therefore always plain `<kind>/<name>` over naturally globally-unique
names, with no owner-qualification anywhere.

The graph is **derived, never hand-wired**. A translation rule per node kind turns existing data
into edges: a declared resource's references map by referent (a `secret`-kind reference becomes a
`secret_refs` entry, a reference to another declared resource becomes a dependency edge, a
capability reference with config at the reference site becomes a held instance), and a live node's
DB row fields become edges (a VM row's `site` field is its edge to the `vm-site` node). The walk
(`walk.py`) is the single traversal every orchestrator uses: a memoized, multi-root, post-order walk
that visits dependencies before dependents and each node exactly once, even when a shared node (one
`git-credential` under two consumers) is reached by several paths. It fails loudly rather than
repairing silently, because keys drive memoization, cycle reporting, and the unwind log: a
dependency cycle raises naming the chain, and two distinct objects sharing one key raise as well,
since one object per key is a construction contract (every edge holder must observe the same object
the orchestrator later marks realized, so the pending-to-realized flip is seen everywhere without
rewiring).

## The Command Execution Model

A command's stages run in a fixed order, and that order is the contract: **preflight-all, then
resolve-once at the preflight boundary, then per phase runup-then-ops**. The single resolve pass is
the one hard split in the whole model; everything else about where a check lives is the author's
judgment, but the resolve boundary is fixed.

Preflight runs first, over every participating node, against one command-start context, before any
prompt and before any mutation. `preflight_all` (`readiness.py`) is the sweep: for each node in the
walk's dependency-first order it predicts that node's declared config secrets are resolvable (see
the fold-and-secrets material below) and then runs the node's own `preflight`. Running before
resolution is exactly what forces preflight to be dependency-blind: it may assume only what is true
at command entry and must never check state a later step in the same command creates. The first
failure propagates; nothing has been touched, so there is nothing to unwind.

Resolution is pinned to the preflight boundary: **resolve as soon as preflight passes**, not eager
at command entry and not deferred to first op-need. Once the sweep clears, the framework resolves
the union of secrets the walked plan declared (`secret_union` over the nodes' `secret_refs`, in
`secrets.py`) in one batched pass, values cached. This placement is deliberate: resolving eagerly
could spend a prompt ahead of a fatal check that would have sunk the op, while deferring to first
op-need would scatter prompts across the run. The invariant it buys is that all plan-wide prompting
happens before the work starts mutating anything (the walk-away point), and nothing is resolved or
prompted twice in one command. Contiguity is not promised: an activation gate may prompt for a
stopped VM's platform credential before the boundary pass does the rest, and both sit before the
walk-away point. Conditional Tailscale repair is the deliberate lazy exception: a stopped VM may
start before late repair-key delivery, then validation precedes every rejoin-specific action.

Runup then runs post-resolve, but **deferred to right before the ops it gates**, not hoisted to the
front with preflight. It reads the already-cached secrets and fires at the op boundary, so in a
multi-phase command it sees the world as of that phase with whatever earlier phases put in place.
The shape is preflight-all, then resolve-once, then per phase runup-then-its-ops, never one global
runup-all followed by one global ops. Runup's own contract stays narrow (raise a typed error on
definitive rejection, warn and continue on network indeterminacy); what a raise _means_ is the
orchestrator's policy, not the node's. `readiness.py` carries the shared policies: a plain uncaught
raise is the fatal case, and `runup_skip_and_degrade` is the generalized skip-and-degrade case (a
definitive rejection skips that one item's materials op, hands it to a domain callback for
messaging, and lets the command continue to a partial result that a later `reinit` recovers).

Prompting now happens inside the command at the boundary rather than at bind, so the operator's
abort point moves with it, and so does the error discipline. A Ctrl-C at a secret prompt raises
`UserAbort`, and any catch-all that wraps a best-effort span (a warn-and-continue cleanup block,
whether around the resolve pass or an op) **must re-raise `UserAbort`, never downgrade it to a
warning**. The cautionary case is deleting a VM: its backend cleanup is deliberately best-effort,
but a swallowed abort at the token prompt would warn, fall through, and delete the DB row anyway,
orphaning the backend VM the operator just declined to authenticate against. The same re-raise rule
holds in unwind (`unwind.py`): a failed `teardown` warns and never masks the original error, but a
`UserAbort` mid-unwind is re-raised.

Two further pieces round out how a command runs, and are covered in the ADR and their own modules
rather than re-derived here: the **activation gate** (`activation.py`), which converges an existing
VM's power state before the preflight sweep and holds it active as a span for the command's
duration, resolving its narrow gate secrets just-in-time and seeding them into the boundary pass so
nothing resolves twice; and **unwind** (`unwind.py`), the command-local `RealizationLog` that
records each pending node as its realizing mutation completes and, on failure, tears them down in
reverse order, best-effort.

## The Readiness Fold and Enablement

Distinct from the per-command lifecycle above, and far cheaper than it, is a second question: **can
this resource run on this host at all?** That is answered once, at registry `finalize`, by a
dependency-ordered fold, and the verdict is stored on the retained `DependencyGraph`
(`resources/graph.py`); consumers read `graph.readiness_of(kind, name)` and never recompute. The
contract is cheap, offline, host-introspection only (OS, tool presence, config shape) and never
touches network, secrets, or prompting; readiness that needs the secret machinery or a remote read
is preflight's job at the op boundary instead. The fold is non-constructing: it never builds an
instance, so it stays total over unvalidated config and a malformed block never becomes a permanent
readiness reason.

Two config-shaped inputs feed it. A capability's config-**independent** host support is the
capability node's own readiness: `VMPlatform.unsupported_reason()` answers "could any configuration
ever work here" (wsl2 off Windows), and the fold reads it off the graph-carried impl. A capability's
config-**dependent** check is keyed on the consuming resource's config (a local-Lima site without
`limactl`; an ssh-placed site without local `ssh`). A consuming resource then decides its own
verdict from its dependencies' states: the fold hands each node a `DependencyState` per dependency
(that dep's enablement, its readiness when enabled, its carried disabled reason, and its impl), and
the resource-level `not_ready(deps)` hook folds them however it likes. The fold imposes no
propagation rule of its own; it only distributes the states. Two consumers propagate from a single
dependency today, `vm-site` from its platform and `git-credential` from its provider (a disabled
dependency propagates the carried remediation reason, e.g. "enable plugin `<name>`", falling back to
"enable its unit"; a not-ready dependency propagates its readiness reason; otherwise the resource
re-asks with its own config). A resource that implements no `not_ready` hook simply opts out and is
always ready: a `secret` and a `session-template` both do this, and a `session-template`'s harness
integration is gated at use instead (`ensure_harness_integration_enabled`, the secret model) rather
than folded here.

Enablement (`enabled` / `disabled`) is a **separate axis** from readiness, for operator opt-in
rather than host capability, and `finalize` now **produces** it: it composes injected enablement
sources (`compose_enablement`) into a disabled-node mark map and projects the binary axis from it,
so a node is `disabled` iff some source marks it. The first such producer is the plugin opt-in
source (a not-opted-in `system-plugin` row is `disabled` with an "enable plugin `<name>`" reason); a
build with no sources yields all-`enabled`, exactly as before. A disabled dependency's mark is what
supplies the remediation clause a propagating consumer's hint reads.

The rules on the resulting verdict are uniform:

- A not-ready resource **still registers**: `agw resource list` marks it, `agw doctor` reports the
  reason, and the resource holds references. Existence and availability are separate axes.
- **Using** a not-ready resource is a typed error naming the reason.
- **References to** a not-ready resource are doctor warnings, never command failures: a resources
  dir shared across hosts degrades gracefully on the host that lacks a requirement.
- A reference to an **absent** resource (a typo, or an uninstalled plugin) is a hard error, not a
  self-disable: absence is the registry's structural miss.

The secret path threads through all of this and is worth stating end to end, because it is the
layer's sharpest security property. Capabilities and nodes **declare** secret references and never
resolve them. The command's union is computed from the walked plan's `secret_refs`, never from a
construction side effect. Resolvability is **predicted centrally** over the declarations
(`predict_resolution` in `secrets.py`), by the operation rather than the resource that named the
secret, because whether a name is available is a property of this run's active sources and operation
inputs, not of the resource. Prediction is a provider-aware, value-free preview: core passes exact
TTY access, the backend alone decides whether it is limiting, and the backend may perform work
allowed by the preview intent, including a safe fetch-and-discard. No value crosses the backend
boundary. That placement is what makes doctor come out right for free: doctor invokes each node's
`preflight` per row without running the sweep, so a prompt-only secret leaves a site row healthy
while the Secrets group reports on the secret once, where it belongs. The union then resolves in
**one boundary pass**, and values are **delivered scoped**: `ScopedSecrets` (the `ctx.secret(name)`
view) hands a node only the names it declared and refuses anything else with a typed error. An
instance therefore cannot read a secret it did not declare and cannot hold a value source of its
own; scoped delivery over the boundary pass is the only way it ever sees a value.

## Relationship to the Capability Model

This layer is the framework side: it drives every node, resolves secrets, and orders readiness. The
[capability model](../capabilities/README.md) is the author side: the contract a single capability
implementation writes to, its five lifecycle stages (`dependencies` / `validate`, `construct`,
`preflight`, `runup`, `ops`), and the self-versus-context rule for reading config and secrets. The
two docs form a clean pair with no overlap: when you are extending or debugging how commands
compose, resolve, and unwind, you are in this document; when you are writing a new `vm-platform`,
`harness-integration`, `git-credential-provider`, or `secret-backend`, you are in the capability
doc, which points back here for how the framework drives what you write.
