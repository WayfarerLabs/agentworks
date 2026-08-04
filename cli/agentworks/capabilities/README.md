# The Capability Model

The capability model is the conceptual and practical framework for extending Agentworks to new
backends and providers without modifying its core logic.

## Motivation

The surrounding ecosystem provides many potentially useful agentic harnesses, VM-provisioning
mechanisms, credential providers, secret backends, and related integrations. The naive path of just
building all that into the core app presents several problems:

- First and foremost, Agentworks must remain secure and reliable. Integrating all this stuff
  directly into the core would make it harder to maintain, audit, and generally reason about.
- Every operator has different needs across this universe of possibility. The system must let
  operators select what they need without forcing them to install, configure, or even just see
  everything.
- Overly-specific integrations into the core might not scale to similar integrations, resulting in
  multiple, slightly different implementations of the same concept, each needing to be tested and
  maintained for all core development.

The capability model is the solution to these problems. By identifying and designing flexible and
powerful extension abstractions, it lets Agentworks integrate new functionality without bloating or
destabilizing the core, and significantly reduces the risk of operators getting negatively impacted
by functionality they do not need or want.

Additionally, the capability model is the foundation for a plugin system that will let third parties
implement and distribute their own capabilities without needing to touch the Agentworks repository.

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

The current plan is that the set of capability kinds is fixed by the core as each one needs to be
carefully integrated into the core logic. The set of capability resources that implement the various
kinds is open-ended. Some are bundled in the core itself, some are included as opt-in **system
plugins**, and some are (will be as of 0.13.0) provided by third-party **plugins**, defined and
distributed entirely outside of the Agentworks repository.

## Currently Implemented Capabilities

Four capability kinds ship today, and between them they cover most of what it takes to stand an
agent up on a machine and let it work: where it runs, what it runs, how it gets its secrets, and how
it authenticates to git hosts. Each kind is an independent extension point with its own shipped
options, allowing an operator to enable only those needed in a particular environment.

### VM Platform

The `vm-platform` capability decides where agent VMs live and how they are brought up, torn down,
and kept healthy. The same Agentworks commands apply across local, cloud, and datacenter
infrastructure: `lima` and `wsl2` provide local VMs on macOS and Windows, while `azure-vm`,
`proxmox`, and `aws-ec2` target cloud or datacenter capacity. Whatever the backend, each delivers
the same foundation: a Debian VM with a passwordless-sudo admin login reachable over Tailscale,
whose whole lifecycle (create, start, a cost-saving stop that resumes with state intact, delete)
Agentworks drives through that one admin foothold. See
[`vm_platform/README.md`](vm_platform/README.md) for what a platform must provide and the specifics
of each.

### Harness Integration

The `harness-integration` capability decides what an agent session actually runs and how that
workload is configured, launched, and managed. This is where a session becomes a plain `shell`, an
interactive `claude-code` or `codex` session, or another agentic harness entirely, without
Agentworks needing to know the details of any one of them. An integration launches its harness,
brings it back on resume (resuming where the harness allows), and checks the harness's binaries are
present, while Agentworks owns the tmux session, the user, and the workspace around it. See
[`harness_integration/README.md`](harness_integration/README.md) for the integration contract and
the shipped options.

### Secret Backend

The `secret-backend` capability is the source of a secret's value when Agentworks needs one,
avoiding any need to hand-carry credentials onto a VM. A secret can be read from an `env-var`,
requested interactively at a `prompt`, pulled from `onepassword`, or sourced from another backend,
and any secret can map to the backend that matches its storage policy. This lets a single resource
definition travel between an operator who keeps tokens in a vault and one who supplies them by
environment variable. A backend resolves a mapping to its value (or reports it absent so the next
backend in the chain gets a turn), describes the lookup for inspection without ever exposing the
value, and never logs it; Agentworks handles where each secret applies and injects it there. Unlike
the other kinds, `secret-backend` is a capability in spirit today: it is a real capability kind but
has not yet moved into `capabilities/` or adopted the shared capability base (tracked in
[#374](https://github.com/WayfarerLabs/agentworks/issues/374)). See
[`../secrets/README.md`](../secrets/README.md) for what a backend must provide and the shipped
options.

### Git Credential Provider

The `git-credential-provider` capability obtains and provisions the git credentials an agent needs
to clone and push against git hosts over plain https without baking tokens into images. `github` and
`azdo` (Azure DevOps) ship today, each knowing how to source a token for its host and get it onto
the VM in the form git expects. A provider obtains its token without a pasted value, sourcing it by
secret name, verifies it before it is relied on, and produces exactly what git needs to authenticate
on the VM, with per-repo scoping so several credentials can serve one host. A future extension may
allow providers to mint tokens through a host API. See
[`git_credential/README.md`](git_credential/README.md) for what a provider must provide and the
shipped providers.

## Planned Future Capabilities

As of 0.13.0, the following capabilities are being considered for future implementation:

- Expanding `harness-integration` across machine, user, workspace, and session scopes. The current
  capability owns only the session workload; the follow-up will let the same integration provision
  harness authentication, plugins, configuration, and permissions at the scopes where those effects
  belong.
- `agent-feature`, `vm-feature`, and `session-feature` capabilities that enable optional, composable
  behaviors at each level: `agent-feature/az-cli` installs and configures the Azure CLI from
  provided secrets; `vm-feature/ca` exposes a certificate authority for cryptographic verification;
  `agent-feature/passport` issues a signed passport attesting to an agent's purpose, verifiable
  against the VM's CA. The value is composability: an operator turns on exactly the features a given
  agent, VM, or session needs, and features can build on each other (passport depends on the CA the
  VM feature exposes) without any of it being wired into the core.
- A `console-provider` that generalizes our current named console feature, allowing for different
  frontends to provide access to the underlying sessions and companion shells. herdr.dev is one
  likely candidate. This would let the same running sessions be reached through whatever console an
  operator prefers, rather than the single built-in one.

While the need to modify the core will mean a higher level of concern, ideas for new capability
kinds are absolutely welcome.

## Technical Overview

The preceding sections describe the operator-facing model. The remaining sections cover the
implementation vocabulary, lifecycle contract, base class, and code layout for engineers who
implement or extend capabilities.

One orientation note before the details. A capability instance is the unit this contract governs,
but it is not the thing the framework walks: the consuming resource that holds an instance is a
graph node, and the framework drives those nodes, resolves their secrets, and orders their
readiness. That machinery is the orchestration layer
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
_consuming resource's_ own behavior, not the capability's ops (a thin consuming resource can wrap a
capability with rich ops, like a credential provider that mints tokens):

- **Thin wrapper** (`vm-site` over `vm-platform`, `git-credential` over `git-credential-provider`):
  the consuming resource names one capability plus a config blob and has no behavior of its own. Its
  runtime _is_ a single capability instance. The capability behind it may still do real work; the
  consuming resource does not.
- **Rich** (`session` over `harness-integration`): the consuming resource has substantial behavior
  of its own _and_ holds one or more capability instances. A session manages panes, env, and
  lifecycle, and holds a private harness-integration instance. It has its own readiness concerns
  _and_ composes its instances'.

The rule this produces, and the one to hold onto: **the base capability class is instance-scoped,
not resource-scoped.** Capability implementations extend it; the consuming resources, decls and
sessions alike, do not. The readiness verbs and ops live on the instance.

A capability instance satisfies the readiness verbs (`preflight` and `runup`) but is not itself a
graph node: the consuming resource that holds it is the node, and that node composes its held
instances' readiness (a thin wrapper forwards the instance's directly, a rich node also adds its own
checks) and folds their declared secrets into its own. The full node-graph and driver model, why an
instance is held-and-composed rather than walked, how the fan-in and the secret folding work, is the
orchestration layer; see [`../orchestration/README.md`](../orchestration/README.md).

#### Multiplicity

A consuming resource holds one instance _per capability usage_. The thin wrappers implemented today
hold exactly one: a `vm-site` holds one `vm-platform`, and a `git-credential` holds one
`git-credential-provider`. The lifecycle can also compose several capability instances without new
machinery: the consuming resource composes their preflights, and the one secret-resolution pass
batches all their declared secrets together. No current resource needs that richer multiplicity.

### The Lifecycle

A capability instance moves through five stages. Each has a sharply different contract; the value of
the whole model is in keeping them from bleeding into each other. The _order_ is part of the
contract: invalid config dies at construct, cheap fatal readiness dies at preflight, secret
prompting waits for preflight to pass (so the operator is never asked for a secret to feed an op
that a bad mapping or a missing tool was going to sink anyway), and authenticated readiness (runup)
runs only once the secrets it needs are in hand.

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
It is the same object minus the secrets, differing only by when it is built, which is exactly why
the dependency-blindness above is structural rather than a rule to remember: a `vm create` preflight
is simply handed a context with no VM target, so it _cannot_ reach the thing the command has not
created yet. (A future permission model omits fields the same way: a capability not granted a target
or a secret just finds it absent.) The rule that pairs with it: pre-resolve concerns read `self`
(config bound at construct); runup and ops read the context.

#### Stage 1: Declare and Validate

```python
dependencies(owner: str, config: Mapping[str, object]) -> tuple[ConfigReference, ...]
validate(owner: str, config: Mapping[str, object]) -> None
```

The config-declaration contract is split in two. `dependencies` extracts the resource references the
config blob implies (secrets it names, other resources it points at); `validate` is the throwing
_shape_ check. This separation is what lets the registry build its dependency graph without
validating: graph construction runs `dependencies` alone, and validation is a distinct pass. They
are:

- **Total vs throwing.** `dependencies` **never raises**: it emits every edge it can derive
  best-effort, omitting only an edge whose _identity_ depends on a field that is itself malformed.
  All the raising lives in `validate`. Together, for a valid blob, they reproduce what the earlier
  fused method did: the same refs from `dependencies`, the same errors from `validate`.
- **Pure.** No I/O, no secret resolution, no network. They are called repeatedly and in varied
  contexts (registry finalize, construct), so they have to be cheap and side-effect-free everywhere;
  finalize in particular is a pure graph-building pass where I/O has no place. (`validate` used to
  also run at manifest decode / TOML load; it moved into a dedicated finalize pass so graph
  construction never depends on a block being valid.)
- **Classmethods.** They have no instance; they read a blob to declare refs (`dependencies`) or
  check its shape (`validate`).
- **Host-agnostic.** `owner` is a label used only for error framing and reference attribution, never
  dispatched on, so the same methods serve config hosted in a dedicated kind, inline in a consumer,
  or in a keyed map (see hosting shapes under Related). They are _not_ the consuming resource; if
  they were, they could serve only one host. Examples: `git-credential/ado`, or a session template's
  `harness_integration_config` site.

The references `dependencies` returns are sourceless. The consuming resource attaches itself as the
source when it emits them, in its `dependencies()` at finalize ("whoever hosts the config that names
the secret emits the reference"). The framework consumes those references two ways: statically, they
feed the registry's reference graph and doctor's resolvability prediction; at runtime, their
_values_ are fetched only by the framework's one batched resolve pass as soon as preflight passes
(described under ops), and delivered through the context. References are never value-resolved at
command entry.

#### Stage 2: Construct

The instance is constructed bound to its `(owner_name, config)`: its config, _not_ resolved secret
values, and no secret machinery at all (the operation's boundary union comes from the plan's
declared references; values arrive later through the context). Construction re-runs `validate` and
**fails on an invalid config shape**: an instance is not built around an invalid blob, so a shape
error dies here, at construction, never later in preflight. (Errors that need the world to detect,
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
already-config-valid instance (config validity is construct's job, not preflight's), using only what
is knowable _before_ any secret is resolved. Its aim is to spend the operator's prompt only on ops
that can actually run. What it checks toward that is the author's call; the list below is the common
toolkit, not a checklist, and a capability with nothing cheap to catch before the prompt has a
trivial (empty) preflight. Its defining property is that it is **read-only and side-effect-free**:

- **Secret resolvability is predicted without prompting** at this stage, but centrally, not by the
  instance or its holding node: the operation's preflight sweep (`preflight_all`) predicts over each
  node's declared references (`orchestration.secrets`), so a declared secret with no mapping at all
  is fatal and knowable here, without prompting for the others, and neither the instance nor the
  node touches the secret machinery. Value checks defer to the op, uniformly. (An earlier draft let
  preflight read-and-verify "non-interactively resolvable" values; that was ruled out: it forks
  readiness on where a secret happens to come from.)
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

- **Read-only and side-effect-free**, exactly like preflight. It never mints, creates, or mutates.
  This is what lets it be re-run and, crucially, would let an explicit interactive surface (a
  planned `doctor --runup`) call it outside an operation.
- **Authenticated.** Reading resolved secrets and probing with them is the whole point; it is the
  half of readiness that only makes sense once resolution has happened.
- **Best-effort, not an oracle**, again like preflight. It raises a typed, actionable error on a
  _definitive_ rejection (a 401 on the token), but network indeterminacy **warns and continues,
  never raises**: a transient outage must not block work that an unverified-but-valid credential
  would have completed. Anything only a mutation can confirm remains the op's job.

When does it run? **Deferred to right before the ops it gates**, not hoisted to the front with
preflight. It reads the secrets the one up-front resolve pass already cached, but fires at the op
boundary, so in a multi-phase command it sees the world as of that phase, with whatever earlier
phases have since put in place (the orchestrator's preflight-all, resolve-once, then per-phase
runup-then-ops sequence is spelled out in
[`../orchestration/README.md`](../orchestration/README.md)). It is skippable by operator policy
where the round-trip is unwanted (the git stack exposes `[defaults] runup_git_credentials = false`,
and airgapped setups want exactly that); preflight is not skippable, because predicting
resolvability costs nothing. Doctor's passive pass does _not_ run runup (see the preflight section);
a planned `doctor --runup` would be the explicit, prompting escalation that does.

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

#### Stage 5: Ops

The domain methods: `create` / `destroy` for a platform, credential-materials for a provider,
`start` / `probe` for an integration. These belong to the subclass, not the base; do not try to
unify them.

Production of a value that requires a mutation lives here, cached and only after the resolve pass,
never in a readiness stage. This is what dissolves the old `acquire_token`-style method entirely:
its verify-half became `runup` (post-resolve, authenticated), its produce-half became a post-resolve
detail of ops. Minting is strictly an op, never runup: minting is a mutation (a new token, a fresh
expiry clock), and runup is read-only, so for a minting provider runup _reads and checks_ the
current token and the op mints when that check says it must.

Secret resolution rides the same seam: ops draw their resolved values from a cache the framework
populated once, through the context, and a minting provider produces its token here guarded by
check-then-mint. How and when that cache is populated is the orchestration layer's concern, not the
ops author's: the framework resolves the command's whole secret union in one batched pass as soon as
preflight passes (neither eager at command entry nor deferred to first op-need), runup then runs on
the resolved values, and every op reads from the same cache. The interactivity guarantees that come
with it (all prompting happens before the work starts mutating anything, the walk-away point;
nothing is resolved or prompted twice in one command; and the `UserAbort` re-raise discipline that
keeps a Ctrl-C at a secret prompt from being downgraded into a swallowed warning, so a declined
authentication never falls through into a mutation) are spelled out in
[`../orchestration/README.md`](../orchestration/README.md).

#### Idempotency

Provisioning re-runs: `reinit` re-applies everything, and a failed command is retried. So the
lifecycle has to be safe to re-run, and the five stages divide cleanly on how they get there:

- `dependencies` / `validate` (pure), `construct` (cheap, side-effect-free), and both readiness
  stages, `preflight` and `runup` (read-only), are idempotent _by their existing contracts_. Their
  stated re-runnability is idempotency by another name; nothing extra is required.
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

### Host Readiness and the Fold

Separate from the lifecycle above is host readiness ("can this resource run on this host at all?"),
a cheap dependency-ordered fold computed at registry finalize and stored on the graph, plus the
enabled/disabled operator opt-in axis alongside it. That is orchestration-layer machinery, not the
capability author's contract, so it lives in
[`../orchestration/README.md`](../orchestration/README.md).

### The Base Class

The shared surface is real (it is a lifecycle, not a boilerplate default), so it earns a base class
(`capabilities/base.py`). The base owns the contract above and nothing domain-specific:

- the `dependencies` / `validate` classmethods, with sensible defaults (no implied references,
  accepts no config) and the standing NOTE that this invoked-validation API may later be superseded
  by capabilities declaring their config schema at registration time;
- the construct, `preflight`, and `runup` instance contract (both readiness stages no-op by default:
  resolvability prediction belongs to the operation's preflight sweep, not to the instance or its
  node, and the capabilities with nothing to check or authenticate get the right behavior for free);
- the capability's identity (`name`, `description`) as the registry sees it.

Subclasses add their ops. `GitHubCredentialProvider`, `VMPlatform`, and `HarnessIntegration` extend
it. Consuming resources do not.

The base lives at the top of the `capabilities/` subtree, not in `resources/`: it is capability
machinery, not framework machinery.

### Secrets Are Just Declared References

A capability's config may name secrets (a Proxmox API token, a git PAT, an AWS client secret).
Nothing special happens: the secret is an ordinary `ConfigReference` returned by `dependencies`. The
framework owns everything after the declaration: non-prompting _prediction_ during the operation's
preflight sweep (is this resolvable on this run?, computed centrally over the declarations by
`orchestration.readiness.preflight_all`, never by the instance or the node holding it), _resolution_
at the preflight boundary (everything the command declared, one batched prompt session, cached), and
_delivery_ through the context, scoped to the declared names. The default secret name is the
capability's to choose: a per-consumer default (`git-token-<name>`, derived from `owner`) where
credentials are many, a shared well-known name (`proxmox-token`) where one is typical. Either way
the capability owns the default; the framework only resolves what was declared.

#### Declare, Then Receive: The Contract That Keeps a Capability Forward-Compatible

Everything above reduces, for a capability author, to two obligations at two moments, with the
framework owning everything in between:

1. **Declare, purely.** Name every secret (and every other resource reference) in `dependencies`: no
   resolver, no I/O, no resolution. This is the capability's _entire_ input side. The framework
   reads those references to build the resolvability prediction the preflight sweep runs and to
   scope the one batched resolve pass.
2. **Receive, from the context.** Read resolved secret values only via `ctx.secret(name)`, in
   `runup` and in ops (their signatures converged on `RunContext`; a VM platform's power ops take
   the op-start context beside the row). There is no other value source: the instance holds no
   resolver and no reader, and construct only _binds_ (`owner_name` plus config); it never resolves.

The rule that ties the two together is the self-vs-context split stated with `RunContext` above:
pre-resolve concerns read `self`, post-resolve concerns read the context. `ctx.secret(name)` raises
when the context was assembled without a resolve pass (inspection), a typed `ConfigError`, and it
also refuses a name the node never declared; neither is a silent skip: runup runs post-resolve, so a
missing value is a caller bug, not a state to tolerate.

Holding this line is what keeps a capability **forward-compatible with the resolution model moving
under it.** That model is the orchestration layer, and it is LANDED: every command's orchestrator
derives the union of secrets from its node graph, resolves once, and hands each node's held
instances their values through the context, scoped to the names they declared. The per-instance
bound resolver is retired; construction takes none, and `ctx.secret(name)` scoped delivery is the
only way an instance ever sees a secret value. A capability that only ever declares (rule 1) and
receives (rule 2) does not change shape as the framework evolves: the `RunContext` it reads is the
stable surface, and only the framework plumbing behind it moves.

Both shipped capabilities are the reference: `git-credential-provider` (github, azdo) reads its
token via `ctx.secret(name)` in `runup`, and `vm-platform/proxmox` reads its API token the same way
in `runup` and in its ops (the op client is built on first need from the delivered value and reused
for the operation); both get the accessor's typed `ConfigError` when the context carries none. A new
capability should never hold a value source of its own.

### Where Capabilities Live

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

### Open Questions

The model is proven on two consuming-side capabilities (`vm-platform`, `git-credential-provider`).
The `secret-backend` capability (already merged, adopting the base under its own change) stresses it
in ways worth recording before that change, because it is a different animal:

- **Shared multiplicity: many consuming resources, one instance.** vm-platform and git-credential
  are per-consuming-resource: one instance per site, per credential. A secret-backend is the
  inverse: one instance built from _global_ backend config, **shared across every secret that maps
  to it**. The consuming resource (a secret) supplies only a per-secret _mapping_ (the env-var name,
  the 1Password item ref), not the backend's config. So readiness deduplicates per backend (check
  1Password once for twenty secrets), and the "consuming resource supplies the config" story flips.
  The Multiplicity section covers the one-resource-many-instances case; this
  many-resources-one-instance shape is not yet modeled.

- **Provider-side vs consuming-side base.** The `Capability` base is shaped for the _consuming_
  side: declare the secrets its config names, then read them back from the context at runup. A
  backend has no declared secrets; it is the thing that _serves_ them. Its contract is different:
  preflight checks installation and configuration, runup checks reachability and authentication, and
  the op resolves a value. Adopting it will likely reveal that today's base is really the
  _consuming-capability_ base, and a backend needs a sibling base or a deliberately looser one.

- **Where its runup lands.** A backend's op _is_ resolution, so "runup right before its op" puts its
  runup at the resolve-pass boundary: authenticate/reach the vault once, before serving any value,
  upstream of every consuming capability's (post-resolve) runup. That is consistent with the general
  rule, not an exception; it is noted only because a backend is the first capability whose op
  precedes the resolve boundary rather than following it. (Most backends have a trivial runup
  anyway: env-var and prompt are knowable offline, so they are preflight-only; only the network/auth
  ones like 1Password carry a real one.)

### Related

- **Each capability kind has a detailed companion README** with more depth on that specific kind:
  [`vm_platform/README.md`](vm_platform/README.md) (running VMs: exposure, credentials, rollback,
  and the bring-up pitfalls), [`harness_integration/README.md`](harness_integration/README.md)
  (harness integrations: the contract, how the session machinery consumes one, session resume), and
  [`git_credential/README.md`](git_credential/README.md) (sourcing and provisioning git credentials:
  the provider contract, the github and azdo providers, the credential-helper path). These guides
  provide the kind-specific details needed for implementation and for tracing shipped behavior.
- **The orchestration layer** ([`../orchestration/README.md`](../orchestration/README.md)) is the
  companion on the framework side: how commands walk the node graph that holds capability instances,
  resolve their secrets once, order preflight and runup, and unwind on failure. It supplies the
  framework context whenever a stage's contract depends on _when_ or _how_ that stage is driven.
- **Hosting shapes.** A consuming resource can host a capability's config three ways: as a dedicated
  kind (reference + a config blob, like `vm-site`), inline in a richer consumer (like a
  session-template's inline harness-integration block), or in a map keyed by name (the planned shape
  for an agent template's feature map, once `agent-feature` ships). `dependencies` / `validate`'s
  host-agnostic `owner` is exactly what lets one capability serve any of these without knowing which
  consumer hosts it.
- `owner` is a host-agnostic string today. If a second consumer (preflight's richer context is the
  likely trigger) needs more than a name, the right evolution is a small host-agnostic context
  value, not passing the consuming resource, designed once, when two real consumers reveal its
  shape.
