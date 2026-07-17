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
that a bad mapping or a missing tool was going to sink anyway), and authenticated readiness (runup)
runs only once the secrets it needs are in hand.

The two readiness stages take their names from flight: **preflight** is the walk-around inspection
at the ramp (early, cheap, before you commit anything); **runup** is the engine run-up at the
hold-short line (everything aboard, throttle up and watch the gauges before committing to the
takeoff roll). They are split by the secret-resolve boundary. **The boundary is the only hard rule;
what each stage checks is the capability author's judgment**, driven by two goals:

- **preflight** (pre-resolve): catch every issue you can _before_ burdening the operator with secret
  prompts. It runs before resolution, so it works without secret values.
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

**The two stages sit differently in time, and that difference has teeth.** Preflight has no choice
about when it runs: the single secret-resolve pass runs once at the start of a command, and
preflight must precede it, so preflight runs for _every_ resource before anything is touched. That
forces preflight to be **dependency-blind**: it may only assume what is true at command entry, and
must never check state that a later step in the same command creates. The canonical antipattern is a
git-credential preflight failing `vm create` because git is not installed, the admin user does not
exist, or the VM does not exist yet, all created later in that same command; a preflight that
checked any of them would fail every first-time create. Runup carries no such obligation: it is
**deferred to right before the ops it gates**, reading the already-resolved secrets from the cache,
so it runs with full current context and may test anything, including dependencies an earlier phase
has since satisfied. Hoisting runup to the front would only re-impose preflight's blindness for no
gain; deferring it is strictly more capable.

That "current context" is a concrete object: **`RunContext`** (`capabilities/base.py`), the resolved
runtime world the service-layer operation assembles and hands to `preflight`, `runup`, and (as op
shapes converge) ops. It carries the operation's config, the execution targets (`admin_target` /
`agent_target`: transports to run as those users on a VM), and resolved `secrets`. Every field is
optional, and the timing is what populates it: **preflight gets it as of command start** (targets
that _already_ exist, no resolved secrets), **runup gets it as of op start** (current targets,
resolved secrets). It is the same object minus the secrets, differing only by when it is built --
which is exactly why the dependency-blindness above is structural rather than a rule to remember: a
`vm create` preflight is simply handed a context with no VM target, so it _cannot_ reach the thing
the command has not created yet. (A future permission model omits fields the same way: a capability
not granted a target or a secret just finds it absent.) The rule that pairs with it: pre-resolve
concerns read `self` (config bound at construct, `self.resolver` for prediction); runup and ops read
the context.

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

**Doctor runs preflight, not runup.** Doctor is a passive, non-interactive scan, so it never
prompts; an authenticated check under it could only ever reach the non-interactively-resolvable
secrets, which is the exact source-asymmetry runup exists to avoid. So doctor stays preflight-only
and uniform, and a secret's resolvability (is it mapped at all?) is already its own doctor row via
the secret backends. On-demand authenticated checking is an explicit, interactive escalation of the
same surface (`doctor --runup`: allowed to prompt, therefore allowed to run runup), tracked
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

### 4. `runup` (confirm readiness; post-resolve, authenticated, read-only)

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

- **Read-only and side-effect-free**, exactly like preflight. It never mints, creates, or mutates.
  This is what lets it be re-run and, crucially, lets an explicit interactive surface
  (`doctor --runup`) call it outside an operation.
- **Authenticated.** Reading resolved secrets and probing with them is the whole point; it is the
  half of readiness that only makes sense once resolution has happened.
- **Best-effort, not an oracle**, again like preflight. It raises a typed, actionable error on a
  _definitive_ rejection (a 401 on the token), but network indeterminacy **warns and continues,
  never raises**: a transient outage must not block work that an unverified-but-valid credential
  would have completed. Anything only a mutation can confirm remains the op's job.

When does it run? **Deferred to right before the ops it gates**, not hoisted to the front with
preflight. It reads the secrets the one up-front resolve pass already cached, but fires at the op
boundary, so in a multi-phase command it sees the world as of that phase, with whatever earlier
phases have since put in place. The shape is: preflight-all, then resolve-once, then _per phase_
runup-then-its-ops (not one global runup-all followed by one global ops). It is skippable by
operator policy where the round-trip is unwanted (the git stack exposes
`[defaults] runup_git_credentials = false`, and airgapped setups want exactly that); preflight is
not skippable, because predicting resolvability costs nothing. Doctor's passive pass does _not_ run
runup (see the preflight section); `doctor --runup` is the explicit, prompting escalation that does.

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
runs right before its materials op and, on rejection, that one credential is skipped and the rest of
initialization continues to partial (fix the token, `reinit`). Same stage, same raise; different,
deliberate handling by the caller.

### 5. ops (do the work; the mutation phase)

The domain methods: `create` / `destroy` for a platform, credential-materials for a provider,
`start` / `probe` for a harness. These belong to the subclass, not the base; do not try to unify
them.

Production of a value that requires a mutation lives here, cached and only after the resolve pass,
never in a readiness stage. This is what dissolves the old `acquire_token`-style method entirely:
its verify-half became `runup` (post-resolve, authenticated), its produce-half became a post-resolve
detail of ops. Minting is strictly an op, never runup: minting is a mutation (a new token, a fresh
expiry clock), and runup is read-only, so for a minting provider runup _reads and checks_ the
current token and the op mints when that check says it must.

Secret resolution rides the same seam, and its timing is pinned to the preflight boundary: **resolve
as soon as preflight passes.** Once the operation's preflight checks clear, the resolver resolves
the union of secrets needed across all planned ops across all participating resources (the
template's Tailscale key and the site's API token join the same pass) in one batch, one prompt
session, values cached; runup then runs on those resolved values, and ops draw from the same cache
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
  `preflight` and `runup` (read-only), are idempotent _by their existing contracts_. Their stated
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
it (the same read-only check `runup` runs), and mint only if it is absent or expired. That guard is
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
- the construct, `preflight`, and `runup` instance contract (`preflight` predicting resolvability by
  default, `runup` a no-op by default: the capabilities with nothing to authenticate get the right
  behavior for free);
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
prompt session, cached; ops and runup draw from the cache). The default secret name is the
capability's to choose: a per-consumer default (`git-token-<name>`, derived from `owner`) where
credentials are many, a shared well-known name (`proxmox-token`) where one is typical. Either way
the capability owns the default; the framework only resolves what was declared.

### Declare, then receive: the contract that keeps a capability forward-compatible

Everything above reduces, for a capability author, to two obligations at two moments, with the
framework owning everything in between:

1. **Declare, purely.** Name every secret (and every other resource reference) in `validate_config`:
   no resolver, no I/O, no resolution. This is the capability's _entire_ input side. The framework
   reads those references to build the resolvability prediction preflight uses and to scope the one
   batched resolve pass.
2. **Receive, from the context.** Read resolved secret values only from `ctx.secrets`, in `runup`
   (and in ops as their signatures converge on `RunContext`). Never fetch a value through
   `self.resolver`: the bound resolver is a _prediction_ tool for preflight (`register_name` /
   `predict`, which never returns a value), not a value source. And construct only _binds_ (config
   plus resolver); it never resolves.

The rule that ties the two together is the self-vs-context split stated with `RunContext` above:
pre-resolve concerns read `self`, post-resolve concerns read the context. A secret is absent from
`ctx.secrets` only when the context was assembled without a resolve pass (inspection), and that is a
typed `ConfigError`, not a silent skip: runup runs post-resolve, so a missing value is a caller bug,
not a state to tolerate.

Holding this line is what keeps a capability **forward-compatible with the resolution model moving
under it.** The direction of travel is an orchestration layer that resolves the whole reference
graph once and hands each capability its values through the context, retiring the per-instance bound
resolver. A capability that only ever declares (rule 1) and receives (rule 2) does not change shape
when that lands: the `RunContext` it reads is the stable surface, and only the framework plumbing
behind it moves. One that reaches into `self.resolver` for values, or resolves at construct, has to
be rewritten.

Both shipped capabilities are the reference: `git-credential-provider` (github, azdo) and
`vm-platform/proxmox` read their tokens from `ctx.secrets` in `runup` and raise a typed
`ConfigError` when it is absent. Proxmox's op client is the one remaining bridge (its `_api` still
reads the token through the bound resolver, pending the op-signature convergence noted with
`RunContext`); a new capability should not add a second.

## Where capabilities live

Capabilities form a clean layer: framework (`resources/`), then capabilities, then domains. A
capability depends only on the framework (it returns framework references, constructs from config
and secrets); it never imports a consuming domain. Consuming domains depend on capabilities. Making
that layer physical is the argument for the `capabilities/` subtree, one subdir per capability kind,
rather than folding each capability into its consuming domain, where the layering is obscured and a
capability-imports-domain violation would go unseen. It is also the natural home for the base class
and this guide and, in a plugin world, the canonical answer to "what does the system support."

The tree fills in incrementally, as each capability adopts the base and moves in under its own
change, not in one sweep. `vm-platform` (`capabilities/vm_platform/`) and `git-credential-provider`
(`capabilities/git_credential/`) live here; the `git-credential-provider`'s consuming resource
(`GitCredentialConfig`) and the materials assembly that writes credentials to a VM stay in the
`git_credentials/` domain, exactly the split this layer is for. The already-merged `secret-backend`
capability still moves in under its own change. That is expected, not half-done.

## Open questions

The model is proven on two consuming-side capabilities (`vm-platform`, `git-credential-provider`).
The `secret-backend` capability (already merged, adopting the base under its own change) stresses it
in ways worth recording before that change, because it is a different animal:

- **Shared multiplicity: many consuming resources, one instance.** vm-platform and git-credential
  are per-consuming-resource: one instance per site, per credential. A secret-backend is the
  inverse: one instance built from _global_ backend config, **shared across every secret that maps
  to it**. The consuming resource (a secret) supplies only a per-secret _mapping_ (the env-var name,
  the 1Password item ref), not the backend's config. So readiness deduplicates per backend (check
  1Password once for twenty secrets), and the "consuming resource supplies the config" story flips.
  The Multiplicity section models one-resource-many-instances (feature maps); this
  many-resources-one-instance shape is not yet modeled.

- **Provider-side vs consuming-side base.** The `Capability` base is shaped for the _consuming_
  side: register the secrets your config declares on the resolver, read them back at runup. A
  backend has no declared secrets; it is the thing that _serves_ them. Its contract is different:
  preflight = am I installed/configured, runup = can I reach/authenticate, op = resolve. Adopting it
  will likely reveal that today's base is really the _consuming-capability_ base, and a backend
  needs a sibling base or a deliberately looser one.

- **Where its runup lands.** A backend's op _is_ resolution, so "runup right before its op" puts its
  runup at the resolve-pass boundary: authenticate/reach the vault once, before serving any value,
  upstream of every consuming capability's (post-resolve) runup. That is consistent with the general
  rule, not an exception; it is noted only because a backend is the first capability whose op
  precedes the resolve boundary rather than following it. (Most backends have a trivial runup
  anyway: env-var and prompt are knowable offline, so they are preflight-only; only the network/auth
  ones like 1Password carry a real one.)

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
