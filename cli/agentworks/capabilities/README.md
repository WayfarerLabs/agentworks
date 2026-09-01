# The Capability Model

The capability model is the conceptual and practical framework for extending Agentworks to new
backends and providers without modifying its core logic.

## Role

Agentic harnesses, VM-provisioning mechanisms, credential providers, secret backends, and related
integrations vary independently. The capability model keeps those implementations outside core
orchestration behind shared extension contracts. Operators can enable the implementations they need
without installing or configuring unrelated integrations, and new implementations reuse an existing
kind's lifecycle and conformance checks.

The system-plugin framework packages bundled, first-party capability implementations behind the same
registration and conformance boundary as built-ins.

## Conceptual Model

The capability model slots into the existing resource model in Agentworks as a new type of resource.
As with all resources, there is a notion of a _resource kind_, effectively the type of the resource,
and the _resource_ itself, which is a concrete instance of that kind.

In the capability world, the **capability resource kind** is the interface defining a particular
type of capability (e.g. point of extension) such as a VM platform, a git credential provider, or a
secret backend. The **capability resource** is a concrete implementation of that kind, such as
`vm-platform/lima`, `git-credential-provider/github`, or `secret-backend/onepassword`. Each kind is
its own interface, tuned for the specific type of capability it represents. Each resource is then a
concrete implementation of that interface, providing the actual functionality.

Capability kinds are fixed by the core because each integrates with core orchestration. Capability
resources may be built in or supplied by opt-in **system plugins** that ship with Agentworks. There
is no separately distributed plugin API today.

## Currently Implemented Capabilities

Four capability kinds ship today, and between them they cover most of what it takes to stand an
agent up on a machine and let it work: where it runs, what it runs, how it gets its secrets, and how
it authenticates to git hosts. Each kind is an independent extension point with its own shipped
options, allowing an operator to enable only those needed in a particular environment.

`agw resource explain <capability-kind>` lists the implementations available in an install; naming
one documents its config. The per-kind READMEs below cover implementation details.

### VM Platform

The `vm-platform` capability decides where agent VMs live and how they are brought up, torn down,
and kept healthy. The same Agentworks commands apply across local, cloud, and datacenter
infrastructure: `lima` and `wsl2` provide local VMs on macOS and Windows, while `azure-vm`,
`proxmox`, `aws-ec2`, and `gcp-gce` target cloud or datacenter capacity. Whatever the backend, each
delivers the same foundation: a Debian VM with a passwordless-sudo admin login reachable over
Tailscale, whose whole lifecycle (create, start, a cost-saving stop that resumes with state intact,
delete) Agentworks drives through that one admin foothold. See
[`vm_platform/README.md`](vm_platform/README.md) for what a platform must provide and the specifics
of each.

### Harness Integration

The `harness-integration` capability decides what an agent session actually runs and how that
workload is configured, launched, and managed. This is where a session becomes a plain `shell`, an
interactive `claude-code` or `codex` session, or another agentic harness entirely, without
Agentworks needing to know the details of any one of them. An integration produces a launch command,
continues its harness conversation where possible, and checks that the harness's binaries are
present, while Agentworks owns start, restart, stop, the tmux server, the user, and the workspace.
See [`harness_integration/README.md`](harness_integration/README.md) for the integration contract
and the shipped options.

### Secret Backend and Source

The `secret-backend` capability implements how a configured `secret-source` can obtain a value,
avoiding any need to hand-carry credentials onto a VM. A secret can be read from an `env-var`,
requested interactively at a `prompt`, pulled from `onepassword`, or sourced from another backend,
and any secret can map to the named source that matches its storage policy. This lets a single
resource definition travel between an operator who keeps tokens in a vault and one who supplies them
by environment variable. A source selects one backend implementation plus its configuration and
precedence position. Its operation-scoped client resolves a mapping to its value (or reports it
absent so the next source gets a turn), while pure inspection describes lookup applicability without
exposing the value, and never logs it; Agentworks handles where each secret applies and injects it
there. `secret-backend` is a nominal `Capability` subclass registered and published by exact class
identity. Its ordinary `preflight` and `runup` hooks are fixed no-ops because source resolution runs
before that lifecycle; operation-scoped clients own bounded provider work instead. See
[`secret_backend/README.md`](secret_backend/README.md) for the author contract and
[`../secrets/README.md`](../secrets/README.md) for resolution behavior and the shipped options.

### Git Credential Provider

The `git-credential-provider` capability turns provider-owned inputs into final HTTPS credential
material. `github` and `azdo` (Azure DevOps) ship today. Each owns its complete source schema,
declared secret inputs, side-effect-free input validation, optional authenticated runup, final Git
username/password construction, and translation of forge scope into generic HTTPS path prefixes. A
provider declares static scopes and may later return a stored credential or a first-party runtime
helper. Core remains acquisition-agnostic: before creation it validates scopes and invokes input
validation; later it validates and routes payloads and fully reconciles each admin or agent user's
Agentworks-owned Git state during initialization. See
[`git_credential/README.md`](git_credential/README.md) for what a provider must provide and the
shipped providers.

## Technical Overview

The preceding sections describe the operator-facing model. The remaining sections cover the
implementation vocabulary, lifecycle contract, base class, and code layout for engineers who
implement or extend capabilities.

One orientation note before the details. For consuming-side capabilities, a capability instance is
the unit this lifecycle governs, but it is not the thing the framework walks: the consuming resource
that holds an instance is a graph node, and the framework drives those nodes, resolves their
secrets, and orders their readiness. Secret backends use the provider-side specialization described
below: the graph carries the registered class and an attempted source turn owns a bounded client
rather than a `Capability` instance. The graph machinery is the orchestration layer
([ADR 0019](../../../docs/adrs/0019-orchestration-layer-command-plans-over-node-graphs.md)),
documented in [`../orchestration/README.md`](../orchestration/README.md). This guide stays on the
implementation side of that boundary and points across it wherever the framework's behavior makes a
stage's contract make sense.

### Terminology

Because a capability is a resource, its vocabulary parallels the resource model exactly, a four-rung
ladder from type to running object:

- A **capability kind** is a resource kind of the capability category (`vm-platform`,
  `git-credential-provider`, `secret-backend`, `harness-integration`). It defines the interface
  every capability of that kind implements. Fixed by the core; neither the app nor a plugin adds
  one.
- A **capability**, precisely a _capability resource_, shortened to "capability" throughout, is a
  concrete implementation of a capability kind, registered as a read-only resource:
  `vm-platform/lima`, `git-credential-provider/github`. It is what `agw resource list` shows under
  the capability category. The app registers the built-ins; plugins register more.
- A **capability instance** is a capability bound to config, the runtime object that actually runs,
  carrying the lifecycle below. It is _not_ a registry resource. A consuming resource holds one
  instance per capability it uses, and may hold many (see multiplicity below).
- A **consuming resource** is a declarable resource that references a capability, supplies its
  config, and owns the instances built from it. It lives in the registry as data.

The load-bearing distinction is between the **capability instance** (runtime) and the **consuming
resource** (data): it governs how Agentworks handles config, secrets, and lifecycle, so the rest of
this doc turns on it.

These are always two things, even when they look like one. Today a `GitCredentialConfig` /
`VMSiteDecl` (the consuming resource, data) is already distinct from the `GitHubCredentialProvider`
/ `VMPlatform` (the capability instance, runtime) constructed from it. Thin-vs-rich describes the
_consuming resource's_ own behavior, not the capability's ops:

- **Thin wrapper** (`vm-site` over `vm-platform`, `git-credential` over `git-credential-provider`):
  the consuming resource names one capability plus a config blob and has no behavior of its own. Its
  runtime _is_ a single capability instance. The capability behind it may still do real work; the
  consuming resource does not.
- **Rich** (`session` over `harness-integration`): the consuming resource has substantial behavior
  of its own _and_ holds one or more capability instances. A session manages panes, env, and
  lifecycle, and holds a private harness-integration instance. It has its own readiness concerns
  _and_ composes its instances'.

The rule this produces for consuming-side implementations, and the one to hold onto: **a constructed
capability object is instance-scoped, not resource-scoped.** Capability implementations extend the
base; the consuming resources, decls and sessions alike, do not. Their readiness verbs and ops live
on the instance. `SecretBackend` derives from the same nominal base but fixes those instance hooks
to no-ops and puts provider work on its source-bound client.

A consuming-side capability instance satisfies the readiness verbs (`preflight` and `runup`) but is
not itself a graph node: the consuming resource that holds it is the node, and that node composes
its held instances' readiness (a thin wrapper forwards the instance's directly, a rich node also
adds its own checks) and folds their declared secrets into its own. The full node-graph and driver
model, why an instance is held-and-composed rather than walked, how the fan-in and the secret
folding work, is the orchestration layer; see
[`../orchestration/README.md`](../orchestration/README.md).

#### Multiplicity

A consuming resource holds one instance _per capability usage_. The thin wrappers implemented today
hold exactly one: a `vm-site` holds one `vm-platform`, and a `git-credential` holds one
`git-credential-provider`. The lifecycle can also compose several capability instances without new
machinery: the consuming resource composes their preflights, and the one secret-resolution pass
batches all their declared secrets together. No current resource needs that richer multiplicity.

### The Lifecycle

A consuming-side capability instance moves through five stages. Each has a sharply different
contract; the value of the whole model is in keeping them from bleeding into each other. The _order_
is part of the contract: invalid config dies at construct, cheap fatal readiness dies at preflight,
secret prompting waits for preflight to pass (so the operator is never asked for a secret to feed an
op that a bad mapping or a missing tool was going to sink anyway), and authenticated readiness
(runup) runs only once the secrets it needs are in hand.

The two readiness stages take their names from flight: **preflight** is the walk-around inspection
at the ramp (early, cheap, before any commitment); **runup** is the engine run-up at the hold-short
line (everything aboard, throttle up and watch the gauges before committing to the takeoff roll).
They are split by the secret-resolve boundary. **The boundary is the only hard rule; what each stage
checks is the capability author's judgment**, driven by two goals:

- **preflight** (pre-resolve): catch every issue possible _before_ burdening the operator with
  secret prompts. It runs before resolution, so it works without secret values.
- **runup** (post-resolve): cleanly catch and identify errors _before_ any mutating op, both to
  avoid unnecessary mutations and to protect against hard-to-diagnose failures partway through the
  real work. It runs after resolution, so it has the resolved secrets in hand.

Beyond respecting that boundary (and staying read-only), the author decides what belongs in each to
give the operator good UX. **Either stage may be empty:** a capability with nothing worth checking
before the prompt has a trivial preflight; one with nothing to authenticate has a no-op runup.
Neither is a failure to fill in a template. What the boundary forbids is the cross-over that
reintroduces asymmetry: an authenticated check in preflight could only use secrets available without
a prompt, which forks readiness on where a secret happens to come from (an env-var token verified, a
prompted one not). Moving it _after_ resolution dissolves that: by the time runup runs, every secret
is resolved the same way, so every credential is checked the same way. Both readiness stages are
read-only and re-runnable; ops are the only mutation.

**The two stages sit differently in time, and that difference has teeth.** Preflight runs for
_every_ resource before anything is touched, and it runs before any secret is resolved (the
orchestration layer pins that ordering; see
[`../orchestration/README.md`](../orchestration/README.md)). That forces preflight to be
**dependency-blind**: it may only assume what is true at command entry, and must never check state
that a later step in the same command creates. The canonical antipattern is a git-credential
preflight failing `vm create` because git is not installed, the admin user does not exist, or the VM
does not exist yet, all created later in that same command; a preflight that checked any of them
would fail every first-time create. Runup carries no such obligation: it is **deferred to right
before the ops it gates**, reading the already-resolved secrets from the cache, so it runs with full
current context and may test anything, including dependencies an earlier phase has since satisfied.
Hoisting runup to the front would only re-impose preflight's blindness for no gain; deferring it is
strictly more capable.

That "current context" is a concrete object: **`RunContext`** (`capabilities/base.py`), the resolved
runtime world the service-layer operation assembles and hands to `preflight`, `runup`, and (as op
shapes converge) ops (how and when the orchestrator assembles and populates it is the orchestration
layer's concern, see [`../orchestration/README.md`](../orchestration/README.md)). It carries the
operation's config and operation scope as plain fields, and the power-granting world behind plain
accessor methods: the execution targets (`ctx.admin_target()` / `ctx.agent_target()`: transports to
run as those users on a VM) and resolved secrets (`ctx.secret(name)`). Everything is optional, and
the timing is what populates it: **preflight gets it as of command start** (targets that _already_
exist, no resolved secrets), **runup gets it as of op start** (current targets, resolved secrets).
It is the same object minus the secrets, differing only by when it is built. A `vm create` preflight
is handed a context with no VM target, so it cannot reach the thing the command has not created yet.
Pre-resolve concerns read `self` (config bound at construct); runup and ops read the context.

#### Stage 1: Declare

```python
config_model: ClassVar[type[BaseModel]]          # what this capability's config IS
config_for() -> type[BaseModel]                  # which config the core reads, the override point
```

A capability DECLARES the shape of its config as a model, and the core does everything else with it:
shape validation, reference extraction, defaulting, schema emission, and rendering are all derived
views of that one declaration. **No capability code is invoked for any of them**, which is what
keeps a misbehaving plugin out of the finalize pass, and what makes it impossible for the validator
and the documentation to disagree.

The model is an `AgwModel` (`agentworks.schema`): strict, frozen, closed-world. A field that names
another resource carries a `SecretRef` / `ResourceRef` marker, optionally with an owner-templated
default (`git-token-{owner_name}`), and that marker is the single authored place the reference
semantics live: the boundary fill (`filled_defaults`) renders the default into the blob before
validation and extraction read it, and emitted JSON Schema carries it as `x-agw-ref`.

The derived validation and extraction views have these contracts:

- **Extraction is total.** It emits every edge it can derive best-effort, omitting only an edge
  whose _identity_ depends on a field that is itself malformed, and it never raises for any input.
  That is what lets the registry build its dependency graph without validating: graph construction
  extracts, and validation is a distinct, later pass.
- **Validation throws**, and only in the finalize pass over the READY and ENABLED set (plus at
  construct). It never runs at decode, so graph construction never depends on a blob being valid.
- **Both read the EFFECTIVE blob on a host that inherits.** A `session-template` merges its
  `inherits` chain and the merged result is what its edges and its shape check are computed from, so
  a required field is a claim about the whole lineage rather than about one declaration (see
  `harness_integration/README.md`). Every other host is a chain of one, so this is a uniform rule
  rather than a special case, and it is why the inheritance edge itself is excluded from
  runtime-need traversal: the child already carries what it inherited.
- **Both are pure**, and structurally so: they read `model_fields` and a raw blob, and invoke no
  user code at all.
- **Host-agnostic.** The owner is a `(kind, name)` pair used for error framing, reference
  attribution, and rendering an owner-templated default. It is never dispatched on, so the same
  model serves config hosted in a dedicated kind, inline in a consumer, or in a keyed map (see
  hosting shapes under Related).

**How the config reaches the model, on a manifest surface.** A host kind's spec selects a capability
with ONE tagged table on its naming field (`platform: {name: lima, placement: {mode: local}}`),
which the row carries as a `CapabilityBlock`. That table has two owners, and the split is the
contract: **`name` belongs to the HOST kind** (it is the selector, and the host's own model
validates it), and **every other key belongs to the capability** the tag names. Decode deliberately
does NOT check the extras, even though it could see them: they are checked closed-world at finalize
against the capability's own declared model. Validating them twice, against two models, is how a
host kind would end up encoding what its capabilities accept, which is the coupling this whole layer
exists to avoid.

`config_for()` is how the core asks which config a capability offers, and every read of a
capability's config goes through the selected result rather than off `config_model` directly. The
framework keeps that selection stable while the implementation's declared `config_model` identity is
unchanged, so registration and later consumers see the same model. Every capability today offers ONE
config shared by all of its operations, so it declares `config_model` and the base hook answers with
it.

Config is offered per FACET by contract: a facet is the level a capability is driven at (`vm`,
`user`, `workspace`, `session`), pairing that level's methods with that level's config. CONSUMERS
choose which facet they drive, so a producer never has to know who is asking, and facets are
deliberately **not** scopes, with core owning the mapping between them (admin and agent both drive
the `user` level; session launches share `session`). Nothing under `capabilities/` spells a scope.
The parameter that names a facet is not on the signature yet, because nothing offers more than one
config; it arrives additively with the first capability whose methods run at several levels, which
is the same change that brings the consumers able to pass it. Offering a config at a facet is
**not** a claim to support that level, and offering none is not a claim to lack it: support is
carried by the implementation.

The references the core extracts are sourceless. The consuming resource attaches itself as the
source when it emits them, in its `dependencies()` at finalize ("whoever hosts the config that names
the secret emits the reference"). The framework consumes those references two ways: statically, they
feed the registry's reference graph; during preflight and doctor, core runs value-free, no-impact
provider preview; at runtime, their _values_ are fetched only by the framework's one batched resolve
pass as soon as preflight passes (described under ops), and delivered through the context.
References are never value-resolved at command entry.

#### Merge Policy Is Declared Too

When a capability config participates in template inheritance or an instance layer, the model that
capability offers also owns the merge policy. A capability normally offers its `config_model`
through the inherited `config_for()` hook; an override must return the model the other derived
surfaces use too. The framework does not call capability code to combine two config blobs. This
keeps built-in and system-plugin models on the same recursive contract and keeps merge callbacks out
of registry finalization.

The defaults follow the schema shape: objects and mappings merge recursively by key, lists
append-deduplicate atomic values in stable order, and scalars replace. A non-default field policy is
typed annotation metadata:

```python
from typing import Annotated, ClassVar

from agentworks.schema import AgwModel, MergeStrategy


class Credentials(AgwModel):
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.REPLACE


class ExampleConfig(AgwModel):
    credentials: Credentials
    extra_args: Annotated[list[str], MergeStrategy.REPLACE]
```

The containing field wins over a mapping-shaped model's `merge_strategy`; the schema default is
last. `REPLACE` discards the complete prior object or list, so an explicit empty value clears it.
Mapping value annotations are supported, but list elements and mapping keys do not have independent
merge-policy slots. List items remain atomic rather than recursively merged.

A capability kind whose config participates in layered merging declares that fact in its core-owned
`ConfigContract`. Registration then calls `merge_contract_error()` on the exact model `config_for()`
offers before the plugin registry is mutated. That check rejects invalid strategy placement,
append-deduplicated item types outside the closed structural JSON comparison carrier, and merged
mappings without exact `str` keys. Mark the complete list or mapping `REPLACE` when its model
intentionally needs a broader input domain. The check also refuses `validation_alias` on every
reachable participating model, including mapping values and models below replacement boundaries. Use
field names for validation; `serialization_alias` remains available for output. Capability kinds
without a layered config surface do not opt in and retain their existing model contract.

The raw merger preserves malformed values for final typed validation rather than invoking model
validators, applying defaults, filtering values, or coercing them. For union fields, whole-field
replacement wins before arm selection; otherwise only values selecting the same arm may recurse. See
[`../schema/README.md`](../schema/README.md#schema-directed-layer-merging) for the complete
strategy, equality, union, and malformed-input contract.

#### Stage 2: Construct

The instance is constructed bound to its `(owner_name, config)`: its config, _not_ resolved secret
values, and no secret machinery at all (the operation's boundary union comes from the plan's
declared references; values arrive later through the context). Construction validates the blob into
the declared model and **fails on an invalid config shape**: an instance is not built around an
invalid blob, so a shape error dies here, at construction, never later in preflight. What binds is
the validated, fully-defaulted model INSTANCE, reachable as `self.config`, so an operation reads a
typed field rather than a dict key with a fallback beside it. (Errors that need the world to detect,
an unreachable API, a missing tool, are preflight's job, not this.) Construction is otherwise cheap:
no network, no secret resolution, no prompt.

This is uniform across hosting shapes. Whether a consuming resource is dedicated to one capability
(`vm-site`) or holds it as one field among many (a `session` with an integration), the instance is
constructed and held the same way, bound to its config. What preflight and ops take per call is
_runtime_ execution context (an integration's command channel, a platform's provision target), which
every capability needs as it runs; that is not a hosting difference. Config binds at construction
for all of them; runtime context passes per call for all of them.

#### Stage 3: Preflight

Preflight answers "will the real work probably succeed?" on an already-constructed,
already-config-valid instance (config validity is construct's job, not preflight's), without
returning secret values. Its aim is to spend operator effort only on ops that can actually run. What
it checks toward that is the author's call; the list below is the common toolkit, not a checklist,
and a capability with nothing cheap to catch has a trivial preflight. Its defining property is that
it is **read-only and side-effect-free**:

- **Secret availability is previewed with no operator impact** at this stage, centrally rather than
  by the instance or its holding node. The operation's sweep (`preflight_all`) previews each node's
  declared references through configured sources and memoizes the value-free result for the command.
  A backend may read and safely discard a provider value if it can do so with no operator action. It
  cannot prompt or return a value. Definitively missing, blocked, or failed previews stop preflight;
  an indeterminate result defers the answer to actual resolution.
- It checks the rest of the world that needs **no credentials**: required tools present on the
  target, an unauthenticated endpoint reachable.
- It does **not** create or mutate anything.
- It is **best-effort, not an oracle.** It catches the common failures cleanly; anything that can
  only be confirmed by mutating is allowed to fail later in the op, with its own clear error. The
  line: _if verifying it requires a side effect, it is not preflight's job._

**The ceiling is structural.** Preflight never holds a resolved secret value and never authorizes
operator impact. Secret backends own the safest probe they can perform under that ceiling and may
fetch and discard a value internally. An indeterminate preview is expected when a backend cannot
establish presence without operator action; actual resolution remains a separate, value-bearing pass
with no preview-impact input.

The read-only property is load-bearing, not stylistic. It is exactly what lets `doctor` reuse
`preflight` for its per-resource health rows (doctor could never call a method that mutates), and
what makes preflight safely re-runnable (at doctor time, at command entry, on retry) without burning
resources or starting expiry clocks. It is also what lets preflight run _before_ any secret prompt:
the cheap fatal checks (a missing mapping, a missing tool, an unreachable API) are caught without
spending the operator's time on a prompt for an op that was never going to run.

**Doctor runs preflight, not runup.** Doctor requests the same no-impact secret preview and never
prompts or returns a secret value. A backend may perform an authenticated provider read and discard
the result if its configured classification says that requires no operator action. Each secret has
an `available`, `missing`, `indeterminate`, `blocked`, or `failed` preview row alongside source and
backend status groups.

When does it run? Every command runs preflight on all the resources it will use before doing
anything real: before any mutation, and before any secret prompt. That is what lets the cheap fatal
checks (a missing mapping, a missing tool, an unreachable API) fail before the operator is spent on
a prompt. How that sweep is ordered across resources and how it predicts each declared secret's
resolvability is the orchestration layer's job (see
[`../orchestration/README.md`](../orchestration/README.md)). For the author the consequences are
just two: preflight is invoked once per instance per command (not once per op), and it must stay
read-only so running it that eagerly is safe and so doctor can reuse it for its per-resource health
rows.

#### Stage 4: Runup

Runup is preflight's post-resolve twin, the engine run-up right before takeoff. It runs _after_ the
operation's single resolve pass, so it holds the resolved secret values preflight could not, and
does the authenticated checks preflight was barred from: a git provider's `GET /user`, a platform's
API connection check, a secret backend's reachability with the real credential. It answers the
question preflight structurally cannot: "with the credentials actually in hand, does the real work
look like it will succeed?"

Its purpose is to catch and identify errors cleanly before any op mutates, for two reasons: to avoid
unnecessary mutations, and to spare the operator hard-to-diagnose failures partway through the real
work (a 401 on a fresh token is far clearer surfaced here than as a git clone failing three steps
into provisioning). What it checks is the author's call, same as preflight; a capability with
nothing to authenticate leaves runup a no-op.

Its contract mirrors preflight where it matters and differs where it must:

- **Read-only and side-effect-free**, exactly like preflight. It never creates or mutates.
- **Authenticated.** Reading resolved secrets and probing with them is the whole point; it is the
  half of readiness that only makes sense once resolution has happened.
- **Best-effort, not an oracle**, again like preflight. It raises a typed, actionable error on a
  _definitive_ rejection (for example, a 401 on a secret-backed credential), but network
  indeterminacy **warns and continues, never raises**: a transient outage must not block work that
  an unverified-but-valid credential would have completed. Anything only a mutation can confirm
  remains the op's job.

When does it run? **Deferred to right before the ops it gates**, not hoisted to the front with
preflight. It reads the secrets the one up-front resolve pass already cached, but fires at the op
boundary, so in a multi-phase command it sees the world as of that phase, with whatever earlier
phases have since put in place (the orchestrator's preflight-all, resolve-once, then per-phase
runup-then-ops sequence is spelled out in
[`../orchestration/README.md`](../orchestration/README.md)). It is skippable by operator policy
where the round-trip is unwanted (the git stack exposes `[defaults] runup_git_credentials = false`,
and airgapped setups want exactly that); preflight is not skippable, because predicting
resolvability costs nothing. Doctor's passive pass does _not_ run runup (see the preflight section).

**What a runup failure means is the caller's call, not runup's.** Runup's own contract is narrow:
raise a typed error on definitive rejection. Whether that _aborts_ the command or is caught, logged,
and stepped around is decided by the service-layer operation running it, per its own stakes. The
general recommendation turns on whether the failed resource is idempotently retryable:

- **Retryable -> continue.** If the resource can be re-attempted later (initialization is repeatable
  via `reinit`; a rejected git credential is fixed and re-run), skip that one resource with clear
  messaging, degrade the command to partial, and let the retry recover it. Do not sink the whole
  command over one recoverable resource.
- **Ultimately fatal -> stop, and roll back.** If the command cannot meaningfully proceed without
  the resource, there is no point continuing: abort, and best-effort **roll back any mutations
  already made** (the same discipline delete uses on a half-built VM), so the failure does not leave
  a stranded half-state.

`vm create` / `vm reinit` and agent provisioning are the retryable case: each git credential's runup
runs right before its materialization op and, on rejection, that one credential is skipped and the
rest of initialization continues to partial (fix the configured source, `reinit`). Same stage, same
raise; different, deliberate handling by the caller.

#### Stage 5: Ops

The domain methods: `create` / `destroy` for a platform, credential-materials for a provider,
`start` / `probe` for an integration. These belong to the subclass, not the base; do not try to
unify them.

Secret resolution rides the same seam: ops draw their resolved values from a cache the framework
populated once through the context. The framework resolves the command's whole secret union after
preflight, runup checks the resolved values, and every op reads from the same cache. The
interactivity guarantees are spelled out in
[`../orchestration/README.md`](../orchestration/README.md).

#### Idempotency

Provisioning re-runs: `reinit` re-applies everything, and a failed command is retried. So the
lifecycle has to be safe to re-run, and the five stages divide cleanly on how they get there:

- declaration (pure data), `construct` (cheap, side-effect-free), and both readiness stages,
  `preflight` and `runup` (read-only), are idempotent _by their existing contracts_. Their stated
  re-runnability is idempotency by another name; nothing extra is required.
- **ops** are the mutation phase, so idempotency there is an _explicit_ contract, not a free
  consequence. Each kind's ABC **flags the ops that must be idempotent** (a marker plus the standing
  docstring note), and implementations must conform: a flagged op, run twice, lands in the same
  place as run once. Flagging is per-op, so a genuinely one-shot op can be left unflagged, but most
  provisioning ops carry it because `reinit` exists.

Many ops satisfy this because they are pure functions or full reconciliation. Git credential
materialization, for example, produces one complete desired state from a scoped context with no
target; its preceding read-only runup receives a separate fresh context with exactly the current
user target. The core then atomically activates that state and removes Agentworks-owned files and
registrations that are no longer desired.

### Host Readiness and the Fold

Separate from the lifecycle above is host readiness ("can this resource run on this host at all?"),
a cheap dependency-ordered fold computed at registry finalize and stored on the graph, plus the
enabled/disabled operator opt-in axis alongside it. That is orchestration-layer machinery, not the
capability author's contract, so it lives in
[`../orchestration/README.md`](../orchestration/README.md).

### The Base Class

The shared surface is real (it is a lifecycle, not a boilerplate default), so it earns a base class
(`capabilities/base.py`). The base owns the contract above and nothing domain-specific:

- the `config_model` declaration and the `config_for()` hook whose default answers with it;
- the construct, `preflight`, and `runup` instance contract (both readiness stages no-op by default:
  resolvability prediction belongs to the operation's preflight sweep, not to the instance or its
  node, and the capabilities with nothing to check or authenticate get the right behavior for free);
- the capability's identity (`name`, `description`) as the registry sees it, and the
  `contract_version` it is written against.

Subclasses add their ops. `GitHubCredentialProvider`, `VMPlatform`, and `HarnessIntegration` extend
it. Consuming resources do not.

**Four class-level declarations are required and none is defaulted:** `name`, `description`,
`contract_version`, and `config_model`. Registration names and refuses an implementation missing any
of them.

The base lives at the top of the `capabilities/` subtree, not in `resources/`: it is capability
machinery, not framework machinery.

### The Kind Descriptor: One Table, Core-Owned

A capability KIND is declared data too, and the same rule holds one rung up: the framework READS
what it needs to know about a kind rather than asking code. `CapabilityKindDescriptor`
(`capabilities/descriptor.py`) is one frozen record per kind, and `capability_descriptors()` is the
single enumeration of the four. Each capability package contributes its own record beside its kind
strategy (`vm_platform/kinds.py`, `harness_integration/kinds.py`, `git_credential/kinds.py`,
`capabilities/secret_backend/kinds.py`), so nothing central knows a kind's internals.

The descriptor table is the single source for registration, publication, readiness, and manifest
hosting. Adding a kind means adding one descriptor instead of updating parallel switchboards.

What a record carries, and why each field is a fact about the kind rather than about one
implementation of it:

- **`contract_version`**, the single implementation contract version this build supports. Every
  implementation declares its own and registration requires an EXACT match, so a contract change is
  a hard cutover: bumping the number refuses every implementation still on the old one until each is
  migrated. Supporting two at once would need a supported-range field and a compatibility rule,
  which is a decision to make when a real migration needs it, not before.
- **`config_schema`** (a `ConfigContract`), what a config model offered for this kind must BE: its
  base, discriminator, input domain, forbidden reference kinds, and whether its model participates
  in schema-directed layers. A descriptor may separately declare **`mapping_schema`**, with the same
  contract facts for its mapping model, and **`mapping_host`** for a map-key-selected consuming
  surface. Secret backends use tagged `AgwModel` source config plus an untagged, JSON-native
  `AgwRootModel` per-secret mapping. **The kind states the contracts; implementations declare the
  models.**
- **`implementation_contract`, `required_operations`, `required_attributes`**, what an
  implementation must satisfy. All four kinds declare nominal bases, and every registered
  implementation must derive from its kind's contract.
- **`registry`, `entry_factory`, `readiness`, `publisher_source`**, how the kind's implementations
  are stored, published as read-only rows, and asked whether this host supports them.
- **`manifest_section`** (a `HostSurface`), which declarable kind's spec selects this capability and
  under which field. Required, because a capability kind no declarable spec selects is a capability
  nothing can ask for. `secret-backend` is selected by `secret-source.backend`; its separate
  per-secret `backend_mappings` surface is described by `mapping_host`.

Every registry stores the implementation CLASS under each name: adapters, graph nodes, and published
rows preserve the exact registered class and registration never constructs it. The kind list is
fixed by the core and the descriptors are frozen: a plugin contributes IMPLEMENTATIONS of existing
kinds, never a kind. Domain operations stay domain-owned, too. Nothing here touches
`VMPlatform.create` or `SecretBackend.create_client`; the descriptor wires a kind into the framework
without absorbing what makes the kind itself.

#### Registration-Time Conformance

Because the descriptor states the contract, the contract is checkable, and it is checked before
anything is seated. Every implementation is run against its kind's record
(`capabilities/conformance.py`) at registration: the base it derives from, its metadata, the
non-operation members the framework reads off it, that nothing would stop it being constructed, the
domain operations, the config model against `config_schema`, the mapping model against
`mapping_schema` when present, each model's opted-in merge contract, and the contract version.
[`../plugins/README.md`](../plugins/README.md#contract-conformance) enumerates the checks; this is
the one place they are listed.

The check is **nominal and never constructs the implementation**, so it says the same thing for
every class registry. It replaced an `isinstance(impl, type)` gate and a `cast`, under which a class
that merely looked plausible seated fine and failed later, far from the mistake. `register_plugin`
runs it in its validation pass before any registry is mutated, so a non-conforming implementation is
a typed error naming the plugin and seating stays all-or-nothing. Built-in implementations seat by
plain assignment at import and are held to the same contract at a different moment, by the
descriptor table's own self-test, which runs the whole chain over every seated implementation of
every kind. That split is deliberate: a plugin's mistake is refused at its own boundary, while a
built-in's is our bug to fail the build on rather than a class to refuse at startup.

### Modeling a Config That Has Variants

Apply these tiers in order. A lower tier cannot excuse a violation of a higher one.

1. **Different shapes are never implicit.** When an operator-written choice selects a genuine
   mechanism or mode with arm-specific required fields, model it as a discriminated union: one
   closed arm per required shape and a string `Literal` discriminator. Do not put a mode field next
   to optional fields and recover the shapes in a validator. A choice between distinct required-key
   shapes that has no mechanism selector may instead be an explicitly declared untagged structural
   union, emitted as plain `oneOf`. Do not add a `mode` tag merely to make that structural choice
   declarable.
2. **Anything that changes the resource graph is model-visible.** If a field or combination decides
   whether a secret reference or resource edge exists, that decision must be present in the model
   shape the walkers traverse. It cannot hide in validator logic. Extraction must reach exactly the
   models validation can select, so extraction and validation cannot disagree about whether an edge
   exists.
3. **Plain-config cross-field validity may live in validators.** Mutual exclusions, dependencies,
   and similar combinations that do not touch the resource graph may use cross-field validation,
   provided the load-time error is loud and precise. The emitted schema may under-constrain that
   combination. This follows the soundness rule: schema must never reject something the loader
   accepts, while sanctioned under-reporting can let an editor catch the same error one step later.

Before encoding a constraint or restructuring a model, apply three companion tests:

- **Dissolve the constraint first.** Ask whether the forbidden combination deserves useful meaning.
  GitHub credential `repos` and `owner` scopes combine as a union. Multiple install tests combine as
  an all-pass condition: an install is skipped only when at least one test is declared and every
  declared test passes; zero tests means the idempotent command always runs. A constraint that no
  longer exists needs no tier.
- **Protect the common spelling.** A restructure that makes the common case heavier fails the
  ergonomic guardrail. Use defaults, scalar shorthands, and untagged structural unions with distinct
  required shapes to preserve it. Reserve a discriminator tag for genuine mechanism selection.
- **Ask whether it is worth it.** Weigh who pays for the restructure and what it buys. Earlier
  editor feedback rarely justifies a heavier manifest for every operator when the existing loader
  failure is already loud and precise. Tier 3 may be the right result.

For a tagged union, the discriminator selects the shape as well as the mechanism. The normal form is
a nested union with a string `Literal` tag per arm:

```python
auth: Annotated[AmbientAuth | ServicePrincipalAuth, Field(discriminator="mode")]
```

Pydantic emits this as `oneOf` over closed object shapes with `discriminator.propertyName` and a
`const` per arm. One authoritative declaration then serves runtime validation, editor validation,
samples, field documentation, and graph extraction. Adding a real mechanism is additive: add an arm
rather than pre-grouping fields for an implementation that does not exist yet.

For a selector-free structural choice, make the structural intent explicit:

```python
source: Annotated[PlaintextSource | SecretSource, StructuralUnion()]
```

`StructuralUnion` is narrower than an ordinary untagged union. It requires at least two closed model
arms whose required and allowed keys cannot overlap, and it emits them as plain `oneOf`. Structural
unions cannot also declare a discriminator; use a tagged union when a selector exists. Structural
arms use their field names as operator-written keys, so validation aliases are refused. Put a
resource marker on the field inside its arm, never on the union holder or a collection element that
holds the union. A scalar shorthand is allowed only on a marker-free structural arm because raw
graph traversal selects these arms from table keys. Registration checks all of these constraints,
including on unions whose arms currently contain no resource markers, before an implementation is
seated.

A union default may select only the arm that the field's omitted spelling means; adding an arm must
not change that meaning. Scalar shorthands must dispatch through the same union-level declaration
used by validation, filling, extraction, schema emission, and conformance; an arm-local shorthand
alone cannot select an arm.

Name each arm for the mechanism it selects rather than its position (`ssh`, not `remote`; `ambient`,
`service-principal`, `access-key`). Pick the discriminator key by the field's grammar. Action-named
fields normally use `mode`; state nouns normally use `type`. Name the union field for what it
selects, even when that means sibling capabilities use different names, such as `auth` and
`placement`.

### Secrets Are Just Declared References

A capability's config may name secrets (a Proxmox API token, a git PAT, an AWS client secret).
Nothing special happens: the secret is an ordinary `ConfigReference` the core reads off a
`SecretRef`-marked field of the capability's model. The framework owns everything after the
declaration: non-prompting _prediction_ during the operation's preflight sweep (is this resolvable
on this run?, computed centrally over the declarations by `orchestration.readiness.preflight_all`,
never by the instance or the node holding it), _resolution_ at the preflight boundary (everything
the command declared, one batched prompt session, cached), and _delivery_ through the context,
scoped to the declared names. The default secret name is the capability's to choose: a per-consumer
default (`git-token-<name>`, derived from `owner`) where credentials are many, a shared well-known
name (`proxmox-token`) where one is typical. Either way the capability owns the default, as the
marker's `default_template`; the framework only resolves what was declared.

#### Declare, Then Receive: The Contract That Keeps a Capability Forward-Compatible

Everything above reduces, for a capability author, to two obligations at two moments, with the
framework owning everything in between:

1. **Declare, purely.** Mark every field that names a secret (or any other resource) in the
   capability's config model: no resolver, no I/O, no resolution, and no code at all. This is the
   capability's entire config-owned input side. The framework reads those markers to build the
   resolvability prediction the preflight sweep runs and to scope the one batched resolve pass.
2. **Receive, from the context.** Read resolved secret values only via `ctx.secret(name)`, in
   `runup` and in ops (their signatures converged on `RunContext`; a VM platform's power ops take
   the op-start context beside the row). There is no other value source: the instance holds no
   resolver and no reader, and construct only _binds_ (`owner_name` plus config); it never resolves.

The rule that ties the two together is the self-vs-context split stated with `RunContext` above:
pre-resolve concerns read `self`, post-resolve concerns read the context. `ctx.secret(name)` raises
when the context was assembled without a resolve pass (inspection), a typed `ConfigError`, and it
also refuses a name the node never declared; neither is a silent skip: runup runs post-resolve, so a
missing value is a caller bug, not a state to tolerate.

This keeps capabilities independent of the resolution implementation. Each command derives its
secret set from the node graph, resolves once, and exposes only declared values through
`RunContext`. Capability instances never hold a resolver or another value source.

A consuming domain can also own required operation values that every implementation must honor. The
VM domain resolves its template's Tailscale auth-key reference once and selects the concrete Debian
release from core policy. Vm-platform contract version 1 carries both through `ProvisionRequest`,
alongside a value-free progress sink. A platform must not redeclare either value in its config, read
a substitute from ambient state, or infer its own meaning of "current." It translates the requested
release through a platform-owned artifact map. Core probes the returned transport and persists only
that live observation. Platform-config secrets still use `ctx.secret(name)`; domain-owned values
follow the operation that consumes them.

The shipped capabilities illustrate both shapes. A secret-backed `git-credential-provider` reads its
declared inputs through `ctx.secret(name)` during pre-creation input validation, runup, and final
materialization, while a CLI source may declare none. `vm-platform/proxmox` reads its API token
through the same accessor in runup and in its ops (the op client is built on first need from the
delivered value and reused for the operation). A provider gets the accessor's typed error if it asks
for an undeclared or undelivered name. A new capability should never hold a value source of its own.

### Where Capabilities Live

Capabilities form a clean layer: framework (`resources/`), then capabilities, then domains. A
capability depends only on the framework (it returns framework references, constructs from config
and secrets); it never imports a consuming domain. Consuming domains depend on capabilities. Making
that layer physical is the argument for the `capabilities/` subtree, one subdir per capability kind,
rather than folding each capability into its consuming domain, where the layering is obscured and a
capability-imports-domain violation would go unseen. It is also the natural home for the base class
and this guide and, in a plugin world, the canonical answer to "what does the system support."

Each capability kind now lives under `capabilities/`: `vm_platform/`, `git_credential/`,
`harness_integration/`, and `secret_backend/`. Consuming resources and domain assembly remain in
their domains. For example, `GitCredentialConfig` and the materials assembly that writes credentials
to a VM stay in `git_credentials/`; only the provider contract and implementations live in the
capability layer.

### Provider-side capability shape

Secret backends exercise the provider-side variant of the model. The registry stores a backend
class, never a process-global backend object. One source configuration and each per-secret mapping
are separate declared models, while an operation creates a source-bound client context only for an
attempted source turn. The class supplies offline readiness and lookup description; the bounded
client supplies provider work and cleanup. Provider deadlines are backend-owned source configuration
rather than a generic factory concern. A backend validates any deadline in its source model and
applies one shrinking deadline across the complete source turn.

Because secret resolution precedes ordinary capability lifecycle, `SecretBackend.preflight` and
`runup` are final no-ops rather than provider hooks. Authentication, connectivity, and external work
belong to the operation-scoped client under its backend-owned source budget. The 1Password backend
applies its validated timeout to the whole source batch. This is a deliberate specialization of the
shared base, documented in [`secret_backend/README.md`](secret_backend/README.md), not an unresolved
sibling-base question.

### Related

- **Each capability kind has a detailed companion README** with more depth on that specific kind:
  [`vm_platform/README.md`](vm_platform/README.md) (running VMs: exposure, credentials, rollback,
  and the bring-up pitfalls), [`harness_integration/README.md`](harness_integration/README.md)
  (harness integrations: the contract, how the session machinery consumes one, continuation), and
  [`git_credential/README.md`](git_credential/README.md) (sourcing and provisioning git credentials:
  the provider contract, the github and azdo providers, the credential-helper path), and
  [`secret_backend/README.md`](secret_backend/README.md) (source configuration, backend mappings,
  bounded clients, and provider-failure translation). These guides provide the kind-specific details
  needed for implementation and for tracing shipped behavior.
- **The orchestration layer** ([`../orchestration/README.md`](../orchestration/README.md)) is the
  companion on the framework side: how commands walk the node graph that holds capability instances,
  resolve their secrets once, order preflight and runup, and unwind on failure. It supplies the
  framework context whenever a stage's contract depends on _when_ or _how_ that stage is driven.
- **Hosting shapes.** A consuming resource can host capability config as a dedicated kind (such as
  `vm-site`), inline in a richer consumer (such as a session template's harness integration), or in
  a map keyed by implementation (such as a secret's backend mappings). The core frames each with a
  host-agnostic owner, so the capability does not depend on its consumer.
