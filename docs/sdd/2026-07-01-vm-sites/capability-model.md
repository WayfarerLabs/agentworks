# The capability model

> Status: design doc, drafted in the vm-sites SDD (this is the first capability pair, `vm-platform`
> / `vm-site`, to implement the full shape). It promotes to a permanent `capabilities/README.md`
> once a second capability (git credentials) validates it. Written for capability authors: it
> describes the contract, not a recipe.

Agentworks has a small number of **capabilities**: code that abstracts different backends and
providers behind a set of uniform interfaces, so agentworks can be extended without modifying its
core logic.

The currently considered/planned capabilities are:

- a `vm-platform` that provisions and manages VMs different ways (`lima`, `azure`, `proxmox`)
- a `git-credential-provider` that sources and provisions git credentials for a git host so
  agentworks can use them (`github`, `azdo`)
- a `secret-backend` that resolves secrets from different sources (`env-var`, `prompt`,
  `onepassword`, ...)
- a `harness` that configures, runs, and manages a specific session workload (`claude-code`,
  `codex`, ...)
- `agent-feature`, `vm-feature`, and `session-feature` capabilities that enable optional, composable
  behaviors at each level: `agent-feature/az-cli` installs and configures the Azure CLI from
  provided secrets; `vm-feature/ca` exposes a certificate authority for cryptographic verification;
  `agent-feature/passport` issues a signed passport attesting to an agent's purpose, verifiable
  against the VM's CA

Capabilities live _inside_ the resource model, not beside it. This is the orienting fact. Every
resource in the registry has a category, **declarable** (operator-declared data) or **capability**
(read-only, code-backed), so a capability is simply a resource of the capability category:
code-defined, registered by the app, listed by `agw resource kinds`, and never declared by an
operator. The capability _kinds_ are fixed by the core; both the app and (future) plugins add
_capabilities_ that conform to them by registering with the core. Consuming resources then use those
capabilities by declaring them and supplying config.

## Terminology

Because a capability is a resource, its vocabulary parallels the resource model exactly, a four-rung
ladder from type to running object:

- A **capability kind** is a resource kind of the capability category (`vm-platform`,
  `git-credential-provider`, `secret-backend`, `harness`). It defines the interface every capability
  of that kind implements. Fixed by the core; neither the app nor a plugin adds one.
- A **capability**, precisely a _capability resource_, shortened to "capability" throughout, is a
  concrete implementation of a capability kind, registered as a read-only resource:
  `vm-platform/lima`, `git-credential-provider/github`. It is what `agw resource list` shows under
  the capability category. The app registers the built-ins; plugins register more.
- A **capability instance** is a capability bound to config and a resolver, the runtime object that
  actually runs, carrying the lifecycle below. It is _not_ a registry resource. A consuming resource
  holds one instance per capability it uses, and may hold many (see multiplicity below).
- A **consuming resource** is a declarable resource that references a capability, supplies its
  config, and owns the instances built from it. It lives in the registry as data.

The load-bearing distinction is between the **capability instance** (runtime) and the **consuming
resource** (data): it governs how agentworks handles config, secrets, and lifecycle, so the rest of
this doc turns on it.

These are always two things, even when they look like one. Today a `GitCredentialConfig` /
`VMSiteDecl` (the consuming resource, data) is already distinct from the `GitHubCredentialProvider`
/ `VMPlatform` (the capability instance, runtime) constructed from it. Thin-vs-rich describes the
_consuming resource's_ own behavior, not the capability's ops (a thin consuming resource can wrap a
capability with rich ops, like a credential provider that mints tokens):

- **Thin wrapper** (`vm-site` over `vm-platform`, `git-credential` over `git-credential-provider`):
  the consuming resource names one capability plus a config blob and has no behavior of its own. Its
  runtime _is_ a single capability instance. The capability behind it may still do real work; the
  consuming resource does not.
- **Rich** (`session` over `harness`): the consuming resource has substantial behavior of its own
  _and_ holds one or more capability instances. A session manages panes, env, and lifecycle, and
  holds a private harness instance. It has its own readiness concerns _and_ composes its instances'.

The rule this produces, and the one to hold onto: **the base capability class is instance-scoped,
not resource-scoped.** Capability implementations extend it; the consuming resources, decls and
sessions alike, do not. Preflight and ops live on the instance. A rich consuming resource has its
_own_ preflight (its own API, not the base's) that _composes_ the preflights of the instances it
holds. Do not grow a `preflight` on a consuming resource; construct the instance and call the
instance's.

Keep the instance a distinct object even in the thin case. It is tempting to collapse a one-to-one
consuming resource and instance into a single class; resisting that is what lets thin and rich cases
share one model instead of forking.

### Multiplicity

A consuming resource holds one instance _per capability usage_, so it may hold many. The thin
wrapper holds exactly one. A rich resource may hold several: an agent template holds a _map_ of
`agent-feature` instances (`az-cli`, `passport`, ...), one per enabled feature. The lifecycle below
scales to that with no new machinery: the consuming resource's preflight composes all its instances'
preflights, and the one secret-resolution pass batches all their declared secrets together. (One
capability appearing more than once in a single consuming resource, with different config each time,
is a further extension the hosting-shapes doc tracks; it is not needed yet.)

## The lifecycle

A capability instance moves through four stages. Each has a sharply different contract; the value of
the whole model is in keeping them from bleeding into each other. The _order_ is part of the
contract: invalid config dies at construct, cheap fatal readiness dies at preflight, and secret
prompting waits for preflight to pass, so the operator is never asked for a secret to feed an op
that a bad mapping or a missing tool was going to sink anyway.

### 1. `validate_config` (declare; pure, classmethod)

```python
validate_config(owner: str, config: Mapping[str, object]) -> tuple[ConfigReference, ...]
```

Validates the config blob's _shape_ and returns the resource references it implies (secrets it
names, other resources it points at). It is:

- **Pure.** No I/O, no secret resolution, no network. It is called repeatedly and in varied contexts
  (decode, registry finalize, construct), so it has to be cheap and side-effect-free everywhere;
  finalize in particular is a pure graph-building pass where I/O has no place.
- **A classmethod.** It has no instance; it validates a blob and declares refs.
- **Host-agnostic.** `owner` is a label used only for error framing and reference attribution, never
  dispatched on, so the same method serves config hosted in a dedicated kind, inline in a consumer,
  or in a keyed map (see the hosting-shapes doc). It is _not_ the consuming resource; if it were,
  the method could serve only one host. Examples: `git-credential/ado`, or a session template's
  `harness_config` site.

The references it returns are sourceless. The consuming resource attaches itself as the source when
it emits them, in its `referenced_resources()` at finalize ("whoever hosts the config that names the
secret emits the reference"). The framework consumes those references two ways: statically, they
feed the registry's reference graph and doctor's resolvability prediction; at runtime, their
_values_ are fetched only through the resolver, in one batched pass as soon as preflight passes
(described under ops). References are never value-resolved at command entry.

### 2. construct (bind; cheap, config-valid by construction)

The instance is constructed bound to its `(name, config, resolver)`: its config plus a
framework-provided _resolver_ it will use to fetch secrets later, _not_ resolved secret values.
Construction re-runs `validate_config` and **fails on an invalid config shape**: you do not build an
instance around an invalid blob, so a shape error dies here, at construction, never later in
preflight. (Errors that need the world to detect, an unreachable API, a missing tool, are
preflight's job, not this.) Construction is otherwise cheap: no network, no secret resolution, no
prompt.

This is uniform across hosting shapes. Whether a consuming resource is dedicated to one capability
(`vm-site`) or holds it as one field among many (a `session` with a harness), the instance is
constructed and held the same way, bound to its config and resolver. What preflight and ops take per
call is _runtime_ execution context (a harness's command channel, a platform's provision target),
which every capability needs as it runs; that is not a hosting difference. Config binds at
construction for all of them; runtime context passes per call for all of them.

### 3. `preflight` (verify readiness; read-only, best-effort)

Preflight answers "will the real work probably succeed?" on an already-constructed,
already-config-valid instance (config validity is construct's job, not preflight's). It reports
problems _clearly_, before any mutation and before any secret prompt. Its defining property is that
it is **read-only and side-effect-free**:

- It **predicts secret resolvability without prompting.** A declared secret with no mapping at all
  is fatal and knowable here, without prompting for the others. A secret that resolves
  non-interactively (an env var that is set) it may read and verify (e.g. a token `GET /user`),
  still without prompting; a prompt-only secret's value-check defers to the op.
- It checks the rest of the world: required tools present on the target, an API reachable and
  authorizing (a _read_).
- It does **not** mutate. In particular it does **not** mint or create anything.
- It is **best-effort, not an oracle.** It catches the common failures cleanly; anything that can
  only be confirmed by mutating is allowed to fail later in the op, with its own clear error. The
  line: _if verifying it requires a side effect, it is not preflight's job._

The read-only property is load-bearing, not stylistic. It is exactly what lets `doctor` reuse
`preflight` for its per-resource health rows (doctor could never call a method that mutates), and
what makes preflight safely re-runnable (at doctor time, at command entry, on retry) without burning
resources or starting expiry clocks. It is also what lets preflight run _before_ any secret prompt:
the cheap fatal checks (a missing mapping, a missing tool, an unreachable API) are caught without
spending the operator's time on a prompt for an op that was never going to run.

When does it run? The starting policy: **every service-layer operation runs preflight on all the
resources it will use, before doing anything real** (before any mutation, and before any secret
prompt). That means the capability instances, and also the declarable resources with readiness
concerns of their own: basically everything has a preflight. `vm create` preflights both the
vm-template (which predicts that its Tailscale auth key can resolve; that key is the template's
responsibility, not the site's) and the site's platform instance, in either order, before the
resolve pass. Within one service-layer operation, multiple ops on the same instance incur preflight
once, not once per op. This is a real latency tax on routine commands, and it is accepted: failing
clearly before work starts is worth more than the round-trip it costs, and there is room to refine
(caching, per-op opt-outs) once real usage shows where it hurts. Doctor calls the same preflights
for its per-resource health rows.

### 4. ops (do the work; the mutation phase)

The domain methods: `create` / `destroy` for a platform, credential-materials for a provider,
`start` / `probe` for a harness. These belong to the subclass, not the base; do not try to unify
them.

Production of a value that requires a mutation lives here, cached and only after preflight, never in
preflight. This is what dissolves the old `acquire_token`-style method entirely: its verify-half
became preflight, its produce-half became a post-preflight detail of ops.

Secret resolution rides the same seam, and its timing is pinned to the preflight boundary: **resolve
as soon as preflight passes.** Once the operation's preflight checks clear, the resolver resolves
the union of secrets needed across all planned ops across all participating resources (the
template's Tailscale key and the site's API token join the same pass) in one batch, one prompt
session, values cached; ops then draw from that cache (a minting provider produces its token through
the same pass, guarded by check-then-mint). Resolution is deliberately neither of the two extremes:
not eager at command entry (a prompt could precede a fatal check that would have sunk the op), and
not deferred to first op-need (prompts would land mid-operation, scattered across the run). The
operator is prompted exactly once, at a predictable moment, after the work is confirmed able to
proceed and before it starts. In one line: preflight passing is the trigger, the command's declared
set is the scope. Wait for preflight, then do it all.

Prompting now happens inside the service-layer operation (at the preflight boundary rather than at
bind), so the operator's abort point moves with it, and the error discipline moves too. A Ctrl-C at
a secret prompt (`UserAbort`) must be handled cleanly, best-effort, at all times: catch-all handling
that wraps a best-effort span (a "warn and continue" cleanup block, whether around the resolve pass
or an op) must re-raise `UserAbort`, never downgrade it to a warning. The cautionary case is
deleting a VM: its backend cleanup is deliberately best-effort (broken backends are what delete
exists to clean up), but a swallowed abort at the token prompt would warn, fall through, and delete
the DB row anyway, orphaning the backend VM the operator just declined to authenticate against.

### Idempotency

Provisioning re-runs: `reinit` re-applies everything, and a failed command is retried. So the
lifecycle has to be safe to re-run, and the four stages divide cleanly on how they get there:

- `validate_config` (pure), `construct` (cheap, side-effect-free), and `preflight` (read-only) are
  idempotent _by their existing contracts_. Preflight's stated re-runnability is idempotency by
  another name; nothing extra is required.
- **ops** are the mutation phase, so idempotency there is an _explicit_ contract, not a free
  consequence. Each kind's ABC **flags the ops that must be idempotent** (a marker plus the standing
  docstring note), and implementations must conform: a flagged op, run twice, lands in the same
  place as run once. Flagging is per-op, so a genuinely one-shot op can be left unflagged, but most
  provisioning ops carry it because `reinit` exists.

Many ops satisfy this for free because they are pure functions or wholesale writes (the
git-credential materials are exactly this: a deterministic build, files overwritten whole, the
helper registered with `--replace-all`, the include added behind a guard). The flag earns its keep
where idempotency stops being free, and minting is the canonical case: a mint creates a new token
and starts a fresh expiry clock, so a naive minting op would mint on _every_ reinit, leaking tokens.
A flagged, idempotent minting op must therefore **check-then-mint**: read the current token, verify
it (the preflight-style read), and mint only if it is absent or expired. That guard is real work the
implementer is on the hook for, and the flag is what tells them so.

## The base class

The shared surface is real (it is a lifecycle, not a boilerplate default), so it earns a base class.
The base owns the contract above and nothing domain-specific:

- the `validate_config` classmethod, with a sensible default (accepts no config) and the standing
  NOTE that this invoked-validation API may later be superseded by capabilities declaring their
  config schema at registration time;
- the construct and `preflight` instance contract;
- the capability's identity (`name`, `description`) as the registry sees it.

Subclasses add their ops. `GitHubCredentialProvider`, `VMPlatform`, `Harness` extend it. Consuming
resources do not.

The base lives at the top of the `capabilities/` subtree (see below), not in `resources/`: it is
capability machinery, not framework machinery.

## Secrets are just declared references

A capability's config may name secrets (a Proxmox API token, a git PAT, an AWS client secret).
Nothing special happens: the secret is an ordinary `ConfigReference` returned by `validate_config`.
The framework owns resolution; the instance never implements it. The instance holds a framework
_resolver_ and uses it two ways: non-prompting _prediction_ in preflight (is this resolvable at
all?), and _resolution_ at the preflight boundary (everything the command declared, one batched
prompt session, cached; ops draw from the cache). The default secret name is the capability's to
choose: a per-consumer default (`git-token-<name>`, derived from `owner`) where credentials are
many, a shared well-known name (`proxmox-token-secret`) where one is typical. Either way the
capability owns the default; the framework only resolves what was declared.

## Where capabilities live

Capabilities form a clean layer: framework (`resources/`), then capabilities, then domains. A
capability depends only on the framework (it returns framework references, constructs from config
and secrets); it never imports a consuming domain. Consuming domains depend on capabilities. Making
that layer physical is the argument for a `capabilities/` subtree, one subdir per capability kind,
rather than folding each capability into its consuming domain, where the layering is obscured and a
capability-imports-domain violation would go unseen. It is also the natural home for the base class
and this guide (`capabilities/README.md`) and, in a plugin world, the canonical answer to "what does
the system support."

The tree fills in incrementally, as each capability adopts the base, not in one sweep, and is not
complete until the already-merged `secret-backend` capability moves in under its own change. That is
expected, not half-done.

## Related

- Hosting shapes (reference + blob as a dedicated kind or inline in a consumer; map keyed by name)
  and how a consuming resource references a capability: see the hosting-shapes doc
  (capability-consumers).
- `owner` is a host-agnostic string today. If a second consumer (preflight's richer context is the
  likely trigger) needs more than a name, the right evolution is a small host-agnostic context
  value, not passing the consuming resource, designed once, when two real consumers reveal its
  shape.
