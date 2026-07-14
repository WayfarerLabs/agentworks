# The capability model

> This is the contract every Agentworks capability implements, so capabilities behave uniformly for
> the framework, for `doctor`, and for the next author. It describes the contract, not a recipe.
> `vm-platform` was the first capability to implement the full shape; `git-credential-provider` the
> second.

Agentworks has a small number of **capabilities**: code that abstracts different backends and
providers behind a set of uniform interfaces, so agentworks can be extended without modifying its
core logic.

The currently considered/planned capabilities are:

- a `vm-platform` that provisions and manages VMs different ways (`lima`, `azure-vm`, `proxmox`)
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
is a further extension, not needed yet.)

## The lifecycle

A capability instance moves through five stages. Each has a sharply different contract; the value of
the whole model is in keeping them from bleeding into each other. The _order_ is part of the
contract: invalid config dies at construct, cheap fatal readiness dies at preflight, secret
prompting waits for preflight to pass (so the operator is never asked for a secret to feed an op
that a bad mapping or a missing tool was going to sink anyway), and authenticated readiness (verify)
runs only once the secrets it needs are in hand.

Readiness is deliberately two stages, `preflight` and `verify`, split by the secret-resolve
boundary. **The boundary is the only hard rule; what each stage checks is the capability author's
judgment**, driven by two goals:

- **preflight** (pre-resolve): catch every issue you can _before_ burdening the operator with secret
  prompts. It runs before resolution, so it works without secret values.
- **verify** (post-resolve): cleanly catch and identify errors _before_ any mutating op, both to
  avoid unnecessary mutations and to protect against hard-to-diagnose failures partway through the
  real work. It runs after resolution, so it has the resolved secrets in hand.

Beyond respecting that boundary (and staying read-only), the author decides what belongs in each to
give the operator good UX. **Either stage may be empty:** a capability with nothing worth checking
before the prompt has a trivial preflight; one with nothing to authenticate has a no-op verify.
Neither is a failure to fill in a template. What the boundary forbids is the cross-over that
reintroduces asymmetry: an authenticated check in preflight could only use secrets available without
a prompt, which forks readiness on where a secret happens to come from (an env-var token verified, a
prompted one not). Moving it _after_ resolution dissolves that: by the time verify runs, every
secret is resolved the same way, so every credential is checked the same way. Both readiness stages
are read-only and re-runnable; ops are the only mutation.

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
  or in a keyed map (see hosting shapes under Related). It is _not_ the consuming resource; if it
  were, the method could serve only one host. Examples: `git-credential/ado`, or a session
  template's `harness_config` site.

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

### 3. `preflight` (predict readiness; pre-resolve, read-only)

Preflight answers "will the real work probably succeed?" on an already-constructed,
already-config-valid instance (config validity is construct's job, not preflight's), using only what
is knowable _before_ any secret is resolved. Its aim is to spend the operator's prompt only on ops
that can actually run. What it checks toward that is the author's call; the list below is the common
toolkit, not a checklist, and a capability with nothing cheap to catch before the prompt has a
trivial preflight beyond the base's resolvability prediction. Its defining property is that it is
**read-only and side-effect-free**:

- It **predicts secret resolvability without prompting.** A declared secret with no mapping at all
  is fatal and knowable here, without prompting for the others. Value checks defer to the op,
  uniformly. (An earlier draft let preflight read-and-verify "non-interactively resolvable" values;
  that was ruled out: it forks readiness on where a secret happens to come from.)
- It checks the rest of the world that needs **no credentials**: required tools present on the
  target, an unauthenticated endpoint reachable.
- It does **not** mutate. In particular it does **not** mint or create anything.
- It is **best-effort, not an oracle.** It catches the common failures cleanly; anything that can
  only be confirmed by mutating is allowed to fail later in the op, with its own clear error. The
  line: _if verifying it requires a side effect, it is not preflight's job._

**The ceiling is structural, and low; that is fine.** Preflight runs before the resolve pass, so it
never holds resolved secret values, and any check that needs one (an authenticated API read, a
credential probe) is out of its reach by design. Do not bend it past that ceiling: partial
workarounds (resolving "just the env-var-backed" secrets, probing one credential source but not the
interactive one) make readiness depend on where a secret happens to come from, which is complexity
without a principled line. Preflight does what unresolved-secret, read-only checks can do;
everything past the ceiling fails at the op, and the op's own typed, actionable error handling is
the other half of the contract: invest there, not in stretching preflight.

The read-only property is load-bearing, not stylistic. It is exactly what lets `doctor` reuse
`preflight` for its per-resource health rows (doctor could never call a method that mutates), and
what makes preflight safely re-runnable (at doctor time, at command entry, on retry) without burning
resources or starting expiry clocks. It is also what lets preflight run _before_ any secret prompt:
the cheap fatal checks (a missing mapping, a missing tool, an unreachable API) are caught without
spending the operator's time on a prompt for an op that was never going to run.

**Doctor runs preflight, not verify.** Doctor is a passive, non-interactive scan, so it never
prompts; an authenticated check under it could only ever reach the non-interactively-resolvable
secrets, which is the exact source-asymmetry verify exists to avoid. So doctor stays preflight-only
and uniform, and a secret's resolvability (is it mapped at all?) is already its own doctor row via
the secret backends. On-demand authenticated checking is an explicit, interactive escalation of the
same surface (`doctor --verify`: allowed to prompt, therefore allowed to run verify), tracked
separately; it is not something doctor's passive pass does.

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

### 4. `verify` (confirm readiness; post-resolve, authenticated, read-only)

Verify is preflight's post-resolve twin. It runs _after_ the operation's single resolve pass, so it
holds the resolved secret values preflight could not, and does the authenticated checks preflight
was barred from: a git provider's `GET /user`, a platform's API connection check, a secret backend's
reachability with the real credential. It answers the question preflight structurally cannot: "with
the credentials actually in hand, does the real work look like it will succeed?"

Its purpose is to catch and identify errors cleanly before any op mutates, for two reasons: to avoid
unnecessary mutations, and to spare the operator hard-to-diagnose failures partway through the real
work (a 401 on a fresh token is far clearer surfaced here than as a git clone failing three steps
into provisioning). What it checks is the author's call, same as preflight; a capability with
nothing to authenticate leaves verify a no-op.

Its contract mirrors preflight where it matters and differs where it must:

- **Read-only and side-effect-free**, exactly like preflight. It never mints, creates, or mutates.
  This is what lets it be re-run and, crucially, lets an explicit interactive surface
  (`doctor --verify`) call it outside an operation.
- **Authenticated.** Reading resolved secrets and probing with them is the whole point; it is the
  half of readiness that only makes sense once resolution has happened.
- **Best-effort, not an oracle**, again like preflight. It raises a typed, actionable error on a
  _definitive_ rejection (a 401 on the token), but network indeterminacy **warns and continues,
  never raises**: a transient outage must not block work that an unverified-but-valid credential
  would have completed. Anything only a mutation can confirm remains the op's job.

When does it run? After the resolve pass, on every participating resource whose readiness has an
authenticated component, before any op mutates, once per instance. The starting policy pairs it with
preflight: preflight-all, then resolve, then verify-all, then ops. It is skippable by operator
policy where the round-trip is unwanted (the git stack exposes
`[defaults] verify_git_tokens = false`, and airgapped setups want exactly that); preflight is not
skippable, because predicting resolvability costs nothing. Doctor's passive pass does _not_ run
verify (see the preflight section); `doctor --verify` is the explicit, prompting escalation that
does.

### 5. ops (do the work; the mutation phase)

The domain methods: `create` / `destroy` for a platform, credential-materials for a provider,
`start` / `probe` for a harness. These belong to the subclass, not the base; do not try to unify
them.

Production of a value that requires a mutation lives here, cached and only after the resolve pass,
never in a readiness stage. This is what dissolves the old `acquire_token`-style method entirely:
its verify-half became `verify` (post-resolve, authenticated), its produce-half became a
post-resolve detail of ops. Minting is strictly an op, never verify: minting is a mutation (a new
token, a fresh expiry clock), and verify is read-only, so for a minting provider verify _reads and
checks_ the current token and the op mints when that check says it must.

Secret resolution rides the same seam, and its timing is pinned to the preflight boundary: **resolve
as soon as preflight passes.** Once the operation's preflight checks clear, the resolver resolves
the union of secrets needed across all planned ops across all participating resources (the
template's Tailscale key and the site's API token join the same pass) in one batch, one prompt
session, values cached; verify then runs on those resolved values, and ops draw from the same cache
(a minting provider produces its token here, guarded by check-then-mint). Resolution is deliberately
neither of the two extremes: not eager at command entry (a prompt could precede a fatal check that
would have sunk the op), and not deferred to first op-need (prompts would land mid-operation,
scattered across the run). The operator is prompted exactly once, at a predictable moment, after the
work is confirmed able to proceed and before it starts. In one line: preflight passing is the
trigger, the command's declared set is the scope. Wait for preflight, then do it all.

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
lifecycle has to be safe to re-run, and the five stages divide cleanly on how they get there:

- `validate_config` (pure), `construct` (cheap, side-effect-free), and both readiness stages,
  `preflight` and `verify` (read-only), are idempotent _by their existing contracts_. Their stated
  re-runnability is idempotency by another name; nothing extra is required.
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
it (the same read-only check `verify` runs), and mint only if it is absent or expired. That guard is
real work the implementer is on the hook for, and the flag is what tells them so.

## Disabled resources (`disabled_reason`)

Distinct from the lifecycle and cheaper than all of it: any resource (capability instance or
declared resource) may answer **"do you have what you need to run on this host?"** via a generic
`disabled_reason() -> str | None` (`None` = enabled). The contract is _cheap, offline,
host-introspection only_: OS, tool presence, the shape of the bound config; never network, secrets,
or prompting. Readiness that needs a resolver or a remote read is preflight's job at the op
boundary; `disabled_reason` runs on inspection and selection surfaces (doctor, `resource list`, site
selection) where preflight would be too heavy.

For most declared resources the answer is a no-op (a vm-template always has what it needs); the
resource layer treats absent-on-kind as "never disabled" (the same structural-hook pattern as
`instances`). Where it is real, the rules are uniform:

- A disabled resource **still registers**: it lists (marked), describes (with the reason), and holds
  references. Existence and availability are separate axes.
- **Using** a disabled resource is a typed error naming the reason chain.
- **References to** a disabled resource are doctor warnings, never command failures: a resources dir
  shared across hosts degrades gracefully on the host that lacks a requirement.

The vm stack is the first adopter: a platform's class-level `unsupported_reason` gates its
capability row ("could any configuration ever work here": wsl2 off Windows), and every vm-site
registers unconditionally, deriving its own `disabled_reason` from the chain: platform missing (an
uninstalled plugin and a typo are indistinguishable by design), platform host-disabled, or the bound
platform instance's own answer (a local-Lima site without `limactl`; remote sites run `limactl` on
the `vm_host` and need nothing locally).

## The base class

The shared surface is real (it is a lifecycle, not a boilerplate default), so it earns a base class
(`capabilities/base.py`). The base owns the contract above and nothing domain-specific:

- the `validate_config` classmethod, with a sensible default (accepts no config) and the standing
  NOTE that this invoked-validation API may later be superseded by capabilities declaring their
  config schema at registration time;
- the construct, `preflight`, and `verify` instance contract (`preflight` predicting resolvability
  by default, `verify` a no-op by default: the capabilities with nothing to authenticate get the
  right behavior for free);
- the capability's identity (`name`, `description`) as the registry sees it.

Subclasses add their ops. `GitHubCredentialProvider`, `VMPlatform`, `Harness` extend it. Consuming
resources do not.

The base lives at the top of the `capabilities/` subtree, not in `resources/`: it is capability
machinery, not framework machinery.

## Secrets are just declared references

A capability's config may name secrets (a Proxmox API token, a git PAT, an AWS client secret).
Nothing special happens: the secret is an ordinary `ConfigReference` returned by `validate_config`.
The framework owns resolution; the instance never implements it. The instance holds a framework
_resolver_ and uses it two ways: non-prompting _prediction_ in preflight (is this resolvable at
all?), and _resolution_ at the preflight boundary (everything the command declared, one batched
prompt session, cached; ops and verify draw from the cache). The default secret name is the
capability's to choose: a per-consumer default (`git-token-<name>`, derived from `owner`) where
credentials are many, a shared well-known name (`proxmox-token`) where one is typical. Either way
the capability owns the default; the framework only resolves what was declared.

## Where capabilities live

Capabilities form a clean layer: framework (`resources/`), then capabilities, then domains. A
capability depends only on the framework (it returns framework references, constructs from config
and secrets); it never imports a consuming domain. Consuming domains depend on capabilities. Making
that layer physical is the argument for the `capabilities/` subtree, one subdir per capability kind,
rather than folding each capability into its consuming domain, where the layering is obscured and a
capability-imports-domain violation would go unseen. It is also the natural home for the base class
and this guide and, in a plugin world, the canonical answer to "what does the system support."

The tree fills in incrementally, as each capability adopts the base and moves in under its own
change, not in one sweep. `vm-platform` lives here (`capabilities/vm_platform/`);
`git-credential-provider` has adopted the base but still lives in its historical `git_credentials/`
home, and the already-merged `secret-backend` capability likewise moves in under its own change.
That is expected, not half-done.

## Related

- **Hosting shapes.** A consuming resource can host a capability's config three ways: as a dedicated
  kind (reference + a config blob, like `vm-site`), inline in a richer consumer (like a session's
  `harness_config`), or in a map keyed by name (like an agent template's feature map).
  `validate_config`'s host-agnostic `owner` is exactly what lets one capability serve all three
  without knowing which consumer hosts it.
- `owner` is a host-agnostic string today. If a second consumer (preflight's richer context is the
  likely trigger) needs more than a name, the right evolution is a small host-agnostic context
  value, not passing the consuming resource, designed once, when two real consumers reveal its
  shape.
