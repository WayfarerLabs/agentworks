# Session harness capability: high-level architecture

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

## Overview

The harness is a new capability, built on the capability model documented in
`cli/agentworks/capabilities/README.md` and proven on `vm-platform` and `git-credential-provider`. A
harness is a consuming-side capability: its implementations extend `capabilities.base.Capability`,
live in the `capabilities/harness/` subtree, and move through the model's lifecycle
(`validate_config` -> construct -> `preflight` -> `runup` -> ops). No resources-framework changes
are needed; the new code is the `capabilities/harness/` package (base, kind module, two built-ins,
registry/publisher) plus the consumer changes in the session-template declaration path and the
harness dispatch in the sessions manager.

Two facts about the current model shape this design, both surfaced by reading the service layer on
main:

- **The session is the model's first RICH consuming resource.** `vm-site` and `git-credential` are
  both thin wrappers (one capability instance, no behavior of their own); the composition
  (preflight-all, one resolve, deferred runups) is done imperatively in the manager roots
  (`bind_platform`, `create_vm`, agent init). The session has substantial behavior of its own
  (panes, env, tmux, lifecycle) AND holds a harness instance whose stages it composes. This SDD
  builds that rich case for the first time; it keeps the composition imperative in the sessions
  manager, consistent with the existing roots, rather than inventing a rich-resource framework.
- **The harness is the first capability to run ON the VM as the target user.** Existing runups probe
  from the CLI host (a git provider's `GET /user`, a platform's API check), so no capability
  populates or reads `RunContext.admin_target` / `agent_target` today. The harness's readiness and
  ops execute on the target (the required-commands probe, claude-code's session-state detection), so
  it is the first real consumer of those (already-defined) `RunContext` transport fields.

```text
declaration                              resources framework
+------------------------------+        +------------------------------+
| session-template             |  ref   | Registry                     |
|   spec.harness --------------+------->|   harness/shell              |
|   spec.harness_config (blob) |        |   harness/claude-code        |
|                              |        |   (capability, built-in,     |
| TOML flat command fields     |        |    read-only, error miss)    |
| hoist to shell's blob at the |        +---------------^--------------+
| loader                       |                        | publish_to
+------------------------------+        +---------------+--------------+
                                        | capabilities.harness         |
runtime (sessions.manager)              |   HARNESS_REGISTRY           |
+------------------------------+        |   { shell, claude-code }     |
| resolve_template(registry)   |        +---------------^--------------+
|   -> (harness, harness_config)|                       |
| harness = harness_for(name)( |  preflight/runup/      |
|   owner, merged_config,      |--- start/restart ------+
|   resolver)                  |
| RunContext(config, targets,  |   ctx.agent_target executes on the
|   secrets, identity)         |   launch target AS THE TARGET USER
|   <- pane command string     |
| core substitutes {{vars}},   |
| tmux hosts the pane          |
+------------------------------+
```

The FRD's model change ("a session is a specification to run a specific harness as an agent in a
workspace on a VM") lands architecturally as a narrowing of what the sessions manager knows: it
stops interpreting command strings and starts constructing the harness instance behind the resolved
template's `(harness, harness_config)` pair and driving its lifecycle. Everything else the manager
does (target preparation, env composition, secret resolution, tmux hosting, liveness) is untouched.

## Package layout

```text
cli/agentworks/capabilities/harness/
  __init__.py         # public surface: HARNESS_REGISTRY, harness_for(), publish_to()
  base.py             # Harness(Capability) ABC, require_commands helper, merge_config default
  kinds.py            # capability kind strategy + HarnessEntry row (domains own their kinds)
  shell.py            # built-in 'shell' harness (owns command/restart_command/required_commands)
  claude_code.py      # built-in 'claude-code' harness (launch-vs-resume state logic)
```

Mirrors `capabilities/git_credential/` exactly: a `base.py` with the `Capability` subclass and
shared helpers, per-member modules, a `kinds.py` following `_GitCredentialProviderKind`, and an
`__init__.py` carrying `HARNESS_REGISTRY` (name -> class) plus `publish_to`. Pure Python, no Typer
dependency (typer-isolation rule), and no import of the `sessions/` domain (the capability-layering
rule: a capability depends only on the framework).

`harness/kinds.py` defines `_HarnessKind` (`category = "capability"`, `miss_policy = "error"`,
`builtin_override = "reserved"`, `auto_declare_names = None`, a `synthesize` that raises
`NoUnreferencedDefaultError`) and a frozen `HarnessEntry(name, origin, references)` row, and
registers into `KIND_REGISTRY` at import. The one-line import is added to
`resources/kinds/__init__.py`, the pure index that populates `KIND_REGISTRY` per domain.

`publish_to(registry)` adds one `HarnessEntry` per registered harness with
`Origin.built_in(source="agentworks.capabilities.harness")`; `bootstrap.build_registry` gains the
call in the built-in publisher block alongside `git_credential.publish_to` / `secrets.publish_to`.

## The Harness API

The centerpiece of this design (FRD R7). The contract is the capability model's lifecycle,
specialized to the session domain; the power is in the `RunContext`.

```python
# capabilities/harness/base.py (pseudocode-level; exact types at LLD)

class Harness(Capability):
    """The session-domain capability: how a named session of a tool runs.

    Extends ``capabilities.base.Capability`` (a consuming-side capability
    like ``GitCredentialProvider``). Bound at construct to
    ``(owner_name, harness_config, resolver)``; ``owner_kind =
    "session-template"``. ``validate_config`` MUST be cheap and
    side-effect-free (it runs at config load and again at construct).
    ``preflight``, ``runup``, and the ops MAY execute code on the launch
    target via ``ctx.agent_target`` / ``ctx.admin_target`` but MUST NOT be
    interactive: no prompts, ever, since all operator interaction precedes
    dispatch (the walk-away invariant), and that holds for plugin
    harnesses too.
    """

    owner_kind = "session-template"

    @classmethod
    def validate_config(
        cls, owner: str, config: Mapping[str, object]
    ) -> tuple[ConfigReference, ...]:
        """Validate the ``harness_config`` blob owned by ``owner``
        (e.g. "session-template/claude") and return the references it
        implies. Both built-ins accept only their own vocabulary and imply
        no references, returning (). VOCABULARY AND SHAPE only (per-field
        checks; unknown fields are errors, FRD R4); completeness rules
        (required fields, cross-field constraints) belong to the merged
        blob at resolve (FRD R2/R5). Raises ConfigError naming the
        offending field; blob-boundary callers prefix the declaration's
        location."""

    # preflight(ctx): inherited from Capability. Pre-resolve, read-only,
    #   DEPENDENCY-BLIND: it runs before the resolve pass and before any
    #   mutation, so it may not assume the target user or workspace exists
    #   (they may be ephemeral, created later this command). For the
    #   built-ins the base default is the whole of it (predict the config's
    #   secret references resolvable; both built-ins declare none, so it is
    #   effectively a no-op). The real environment check is runup, below.

    def runup(self, ctx: RunContext) -> None:
        """Post-resolve readiness in the REAL session environment: the
        target-environment check the manager runs before the launch op.
        Today: probe ``required_commands`` on the actual target user
        (``ctx.agent_target`` / ``ctx.admin_target``), in the actual
        workspace, with the fully composed env. Deferred to right before
        the launch op, so the ephemeral target/workspace already exist.
        Raises StateError with actionable detail; returning means "safe to
        proceed". The common case is one call to
        ``base.require_commands(ctx, [...])``, which packages today's
        required-commands probe loop and error shape. Read-only, like every
        runup; the mutation is the tmux launch that follows."""

    def start(self, ctx: RunContext) -> str:
        """Op: the pane command for a fresh ``session create``. Reads its
        blob from ``self.config``. Empty string means login shell only."""

    def restart(self, ctx: RunContext) -> str:
        """Op: the pane command for ``session restart`` (invoked after the
        old session is killed)."""

    # OPTIONAL, getattr-gated (repo precedent: ResourceKind.instances):
    #
    #   def merge_config(self, base: Mapping, child: Mapping) -> dict:
    #
    # Inheritance-time blob merge for same-harness parent/child pairs.
    # Absent-on-class means the default shallow merge ({**base, **child}).
    # `shell` implements it to union required_commands (parity with
    # today's append-dedupe inheritance semantics).
```

Contract points, and why they are shaped this way:

- **The blob binds at construct; the ops and readiness read `self.config`.** This follows the
  capability model (config is bound at `Capability.__init__`, re-validated there) and REVERSES the
  pre-realignment draft, which passed config as an explicit per-call parameter. The resolved, merged
  blob is what the instance is constructed against, so `start`, `restart`, and `runup` read
  `self.config` and take only `ctx`. `RunContext.config` is the GLOBAL `agentworks.config.Config`
  (settings), not the harness blob, exactly as it is for every other capability.
- **The return value is a pane command string, and that is the whole tmux story.** The core applies
  template-variable substitution and `exec` wrapping to the op's return and hands it to
  `tmux.create_session`, exactly as it does for today's template `command`. The harness never sees
  tmux (FRD R7's tmux invariant): it decides WHAT runs in the pane, and returning a string is the
  mechanism that makes "never HOW" structural. A multi-step launch is a compound shell snippet in
  the returned string. (This is also the "payload-only" shape the permission ladder reserves for
  untrusted harnesses: an op that returns a declarative payload the core executes needs no live
  channel at all.)
- **`ctx.agent_target` / `ctx.admin_target` are the arbitrary-code surface, bound to the target
  user.** "Target user" is the established vocabulary (the direct-user-SSH SDD's "operations whose
  target user is the agent"): the user the session runs as, the selected agent, or the admin user in
  admin mode. The harness uses `ctx.agent_target` in normal mode and `ctx.admin_target` in admin
  mode. These are the same user-bound transports the manager already holds for the launch; handing
  them on the context costs no new plumbing and inherits the existing logging and error shaping.
  Harness logic runs CLI-side; only the commands it issues run on the target. Nothing is uploaded or
  installed. The harness is the first capability to actually use these fields, so the composition
  root populates them when it builds the `RunContext` for the harness's runup and ops (existing
  runups leave them `None`).
- **The readiness check is `runup`, not `preflight`, and the model forces that.** Preflight runs
  before the single resolve pass and before any mutation, which makes it dependency-blind: it may
  not check state a later step of the same command creates. The harness's target-environment check
  depends on exactly such state (an ephemeral agent's Linux user, an ephemeral workspace's files),
  so it cannot be preflight without failing every first-time ephemeral launch (the direct analog of
  a git-credential preflight failing `vm create` because git is not installed yet). It is therefore
  `runup`: deferred to the op boundary, run against the real target the provisioning phase created,
  with the composed env the resolved secrets feed. The built-in harnesses declare no secrets, so
  their `preflight` is the base's resolvability prediction and nothing more, near-empty by design
  and a legitimate answer, not an unfinished one.
- **`runup` needs no secret, and that is allowed.** The preflight/runup boundary is the
  secret-resolve boundary, not an auth boundary: runup is simply "post-resolve, read-only, before
  the mutating op". The required-commands probe reads no secret (git-credential's own "is git
  installed on the target" examples are the same non-authenticated runup shape); it wants the
  post-resolve slot because it needs the post-provision target and the composed env (which the
  resolved values feed), not because it authenticates.
- **`start` and `restart` are separate ops** rather than one op with a flag: they are the contract's
  verbs (FRD R3/R7 vocabulary), implementations that differ (shell) read naturally, and
  implementations that coincide (claude-code, where both reduce to resume-or-launch) share a private
  helper. A future restart-specific concern (pre-resume cleanup) has an obvious home.
- **Errors**: `validate_config` raises `ConfigError`; `runup` / `start` / `restart` raise the
  standard typed errors (`StateError`, `ExternalError`) with `entity_kind="session"`, which the CLI
  already renders.

### The identity gap and the proposed `RunContext` enrichment

`RunContext` carries `config`, `admin_target`, `agent_target`, `secrets`, and nothing else. The
harness needs the model's identity chain by NAME: at minimum the SESSION name to address the tool
session (`claude --name <session>`, distinct from the template name, which is the harness's
`owner_name`), and the vm / workspace / agent names for probe context and error labels. None of that
is config, `owner`, or a target's `describe()` string.

This is exactly the "second consumer" the capability model's own open question anticipated:

> `owner` is a host-agnostic string today. If a second consumer (preflight's richer context is the
> likely trigger) needs more than a name, the right evolution is a small host-agnostic context
> value, not passing the consuming resource, designed once, when two real consumers reveal its
> shape.

The harness reveals that shape. The proposal (a capability-model change, flagged for the
maintainer): add a single host-agnostic identity value to `RunContext`, populated by the composition
root for its operation. For a `vm create` it carries just the VM name; for a `session create` it
carries the full chain (vm, workspace, agent-or-None, session). A capability reads what it needs and
ignores the rest, the same way it reads the optional targets. This keeps the addition additive and
shared rather than harness-specific, and it stays NAMES-only for now (see the "Names now" design
decision for why representations are deliberately deferred). If the maintainer prefers not to touch
`RunContext`, the fallback is a harness-specific context the sessions manager builds and passes
alongside the lifecycle calls; that keeps the change out of the shared model at the cost of the
harness not being a clean `Capability`. The recommendation is the shared identity value: it is
small, it matches the model's own predicted evolution, and the harness is a genuine second consumer,
not a special case.

## Layer changes

### Declaration layer (`sessions/template.py` + `config.py`)

`SessionTemplate` lives in `sessions/template.py` (its home since resource-manifests Phase 5.8:
domains own their dataclasses and kinds; `config.py` keeps only settings and the legacy TOML
loaders). The reshape lands there, following the best-representations rule (internal shape = YAML
shape):

- Fields `command`, `restart_command`, `required_commands` are REMOVED; `harness: str | None` and
  `harness_config: dict[str, object] | None` are added (`None` = not declared, which inheritance
  distinguishes from declared-empty).
- `_load_session_templates` accepts the new keys plus the legacy flat fields and applies the FRD R6
  rules: flat fields hoist to `harness="shell"` + the equivalent blob; flat fields + a non-`shell`
  `harness` is a `ConfigError`; flat fields + an explicit `harness_config` table is a `ConfigError`.
  `_SESSION_TEMPLATE_KEYS` grows accordingly (the flat keys stay, so no unknown-key warning
  regression).
- Blob validation runs through the capability config-validation contract at exactly the
  git-credential invocation points: the manifest decoder validates the TRUE blob with `file:line`
  framing, the TOML loader validates its assembled (hoisted) blob, and unknown harness names skip
  invocation so the reference miss policy reports them uniformly at finalize. Validation dispatches
  through `HARNESS_REGISTRY[name].validate_config(owner, blob)` (a classmethod; no instance needed
  at load).
- `SessionTemplate.referenced_resources()` emits one `harness`-kind reference when `harness` is
  declared (usage "the session harness", source `("session-template", <name>)`), plus any
  capability-implied `ConfigReference`s from `validate_config`, emitted as itself per the contract
  (none today for either built-in; the plumbing is uniform so a future harness whose blob names a
  secret Just Works, its token registering on the resolver at construct like a git credential's).

### Manifest decoder (`manifests/decode.py`)

`_decode_session_template` rejects the flat fields before delegating (the clean-spec rule, FRD R2),
with the error pointing at `harness: shell` + `harness_config`; `harness` / `harness_config` pass
through to the shared TOML loader, which owns all further validation. Same pre-check-then-delegate
shape as `_decode_git_credential`'s `type` rejection.

### Template resolver (`sessions/templates.py`)

`ResolvedSessionTemplate` becomes `(name, description, env, harness: str, harness_config: dict)`,
defaults `("shell", {})`. The merge walk keeps its depth-first, left-to-right order and env/
description semantics; the pair merges per FRD R5:

```python
def _merge_pair(acc_name: str | None, acc_config: dict, child: SessionTemplate):
    if child.harness is None:          # child says nothing about the pair
        return acc_name, acc_config    #   (harness_config without harness cannot load)
    base = acc_config if child.harness == acc_name else {}   # fresh blob on switch
    merged = harness_for(child.harness).merge_config(base, child.harness_config or {})
    return child.harness, merged
# after the walk: (None, {}) -> ("shell", {})   # the undeclared default
```

`merge_config` here is the class-level (getattr-gated) hook, invoked without an instance, since the
merge is a pure blob operation; the resolved pair is what a harness INSTANCE is later constructed
against. The resolver validates the MERGED blob through `validate_config` once resolution completes
(FRD R7: the resolved pair is validated at use) -- a merged blob is a new value no single
declaration ever saw, so declared-blob validation alone cannot cover it. This is also where
completeness lives: declared blobs validate vocabulary/shape only (a restating child may be
partial), and any required-field or cross-field rule a harness has is checked here, against the
merged whole.

### Sessions manager (`sessions/manager.py`)

The manager becomes the harness instance's composition root, mirroring how `create_vm` roots the
platform and providers:

- After template resolution it constructs the instance:
  `harness = harness_for(resolved.harness)(resolved.name, resolved.harness_config, resolver)`,
  binding it to the same operation `Resolver` every other instance registers on (a future
  secret-declaring harness's references join the one boundary resolve for free).
- `_build_session_command(...)` becomes a thin dispatcher: assemble the `RunContext` from values the
  call site already holds (the target-user transport as `agent_target` / `admin_target`, the
  resolved secrets, the global config, the identity names), call `harness.start(ctx)` or
  `harness.restart(ctx)`, then apply template-variable substitution to the returned string exactly
  as today.
- `_assert_required_commands`'s probe loop, docstring rationale, and error shape relocate verbatim
  to `capabilities/harness/base.py` as the `require_commands(ctx, commands, ...)` helper; the
  manager's readiness call site becomes `harness.runup(ctx)`.

Sequencing note (pinned so the LLD preserves it): on restart, `runup` runs BEFORE the destructive
kill (as today's required-commands check does), while the `restart` op runs AFTER it, which is
exactly what claude-code needs, since Claude Code's session state is only settled once the old
process is dead. On create, the `start` op runs after `runup`, before `tmux.create_session`. Both
paths already hold the correctly-bound target transport at those points; no new threading beyond
placing it on the `RunContext`.

### Env composition (unchanged; stated so the story is explicit)

Nothing about env moves, and the invariant is simple: **everything gets the appropriately resolved
env.** Secrets resolve exactly once per command (the operation `Resolver` at the preflight boundary,
folding session env-chain secrets in via `register_targets` / `compute_needed_secrets`),
`compose_env` merges the vm / workspace / admin / agent / session scopes with the resolved values,
and the composed env reaches the pane through the existing belt-and-suspenders delivery (SSH
`SetEnv` on the connection plus tmux `-e` session-environment flags). The command a harness returns
therefore executes with the fully resolved env exactly as today's template `command` did; a harness
never composes, reads, or forwards env itself, and `spec.env` stays core template vocabulary, never
harness config.

One deliberate upgrade over today: the target transport handed to the harness on the `RunContext` is
bound with the composed env (the same `SetEnv` delivery the pane's SSH connection uses), so runup
and launch-time probes see the env the pane will see; today's login-env-only probe misses
PATH-affecting values the session itself gets. Probes run in the workspace directory, matching the
pane's working directory (the workspace exists by runup time; ephemerals are provisioned first).

### Registry, bootstrap, inspection

- `capabilities/harness/kinds.py` self-registers into `KIND_REGISTRY` at import (one index line
  added to `resources/kinds/__init__.py`), which makes `agw resource kinds`, `--kind harness`
  filters, the `resource_refs` completer, and the envelope's capability-kind rejection all work with
  zero additional wiring.
- `bootstrap.build_registry` gains `harness.publish_to(registry)` in the built-in block. Publisher
  order within the block is irrelevant (no cross-kind collisions).
- `agw resource list/describe/kinds` need no code changes; the rows and references render through
  the existing framework surfaces (FRD R8).

### Database: unchanged, deliberately

No schema migration. `SessionRow` stores the template NAME (plus placement and tmux lifecycle
fields); the pane command is not persisted today and the harness pair is not persisted either.
Restart re-resolves the template by the stored name, reconstructs the harness instance, and
dispatches fresh, so the harness and its config always come from current declared config, exactly
the semantics template edits already have between create and restart. Nothing else in the design
wants DB state.

### Migration tool (`migrate/planning.py`)

`_emit_document` gains a `session-template` branch mirroring the `git-credential` one: pop the flat
fields; when any were present, emit `harness: shell` plus a `harness_config` blob built from them;
pass declared `harness` / `harness_config` through. The TOML loader's hoist and this emission land
on the identical internal value, so the per-run registry-equivalence verification (and the golden
tests) prove the shape divergence is value-free, exactly as they did for `provider_config`.

### Samples and docs

- `manifests/samples/session-template.yaml`: rewritten to lead with a `claude-code` document (one
  `harness:` line where the old sample restated three command strings) followed by the `shell` +
  `harness_config` form for generic commands; stays fully commented per the sample rules.
- Permanent docs per FRD R10, each riding the commit that makes it true: top-level `README.md` model
  narrative, `cli/README.md` schema/reference, `docs/guides/resources.md` capability story (which
  already carries `secret-backend` and `git-credential-provider`).
- The ADR is drafted as `adr-session-harness.md` in this feature directory and is promoted (and
  numbered) into `docs/adrs/` at the very end of the effort (FRD R10). It references ADR 0016 for
  the capability collapse and `capabilities/README.md` for the lifecycle contract.

## Service layer orchestration

`create_session` (`sessions/manager.py`) is already the single service endpoint this SDD needs: the
ephemeral-session work gave it CLI-flag-shaped parameters, consolidation and validation in the
service, atomic ephemeral workspace/agent provisioning with rollback on any failure, and one secret
resolve. Harness dispatch slots INSIDE that endpoint without changing its shape, and the CLI stays
dead simple per cli-conventions (`agw session create` gains no new flags; the service layer is the
authority).

The orchestration follows the capability model's canonical order, the same one `create_vm` and agent
init use: preflight-all before any prompt or mutation, then the single resolve at the preflight
boundary (one prompt session), then per-phase runup-then-ops. Applied to create:

1. `build_registry(config)` -- once. Harness rows publish; a template's declared `harness` reference
   validates at finalize, so a typo'd name dies here, before any prompt or mutation.
2. Flag-shape validation, DB checks, operator prompting (unchanged).
3. Template resolution -> `(harness, harness_config)`; merged-blob `validate_config`; construct the
   harness instance against the operation `Resolver`. A bad blob fails before any prompt or
   mutation.
4. Ensure the VM is running (unchanged; precedes the resolve today). Interactive by design when a
   stopped VM needs a tailscale auth key, interactivity that rightly precedes the walk-away point.
   Power-state convergence is idempotent declared-state maintenance, not a rollback-tracked
   mutation; called out so "any state change" in the invariant reads precisely.
5. Preflight-all: the harness's `preflight(ctx)` (near-empty for the built-ins) alongside every
   other participating resource's, all against the command-start `RunContext(config=config)` (no
   secrets, no on-VM targets), before any prompt or mutation.
6. The single secret resolve (`Resolver.resolve()`; the union across every instance registered on
   it, one batched prompt session) and env composition. When `--new-agent` is in play, the ephemeral
   agent's git-credential tokens are already folded into this resolve on main (constructed against
   the same resolver; their values thread through `create_agent`, which skips its own resolve). The
   harness adds no resolve calls and no prompts. **This is the walk-away point.**
7. Ephemeral workspace/agent provisioning (unchanged; rollback-protected from here down), then
   target preparation. Nothing in this block prompts.
8. `harness.runup(ctx)` against the op-start `RunContext` (config, resolved `secrets`, and the
   target transport as `agent_target` / `admin_target`, plus the identity value), in the real
   environment: actual target user, actual workspace, composed env. A failure rolls back the
   ephemerals and reports; when nothing ephemeral was requested, nothing has mutated yet and the
   abort is free.
9. `harness.start(ctx)` -> pane command; template-var substitution; `tmux.create_session` with the
   composed env.

`restart_session` runs against the existing session (template re-resolved by the stored name,
instance reconstructed). It differs from create in one intentional way already on main: it binds the
platform via `_prepare_vm` (site secrets resolve there) and resolves the session ENV chain via the
legacy `resolve_for_command` AFTER its BROKEN/confirm gates, so a declined restart never prompts for
secrets it would discard. The harness `runup` slots after that resolve and before the kill: resolve
-> compose -> `harness.runup(ctx)` -> kill -> `harness.restart(ctx)` (after the kill, the
claude-code sequencing requirement). A bad blob, a missing binary, or an unresolvable secret all
abort with the old session still running. Past the kill, the failure contract changes shape (FRD
R7): a `restart` op or tmux failure cannot restore the old session, so the pinned end state is
session row intact, old tmux gone, `agw session restart` cleanly retryable, and an error naming the
failed step, no resurrection attempt.

## Validation responsibilities

| Layer                       | Owns                                                                                     |
| --------------------------- | ---------------------------------------------------------------------------------------- |
| TOML loader                 | section/key shapes, flat-field hoist, flat+non-shell and flat+blob conflict errors       |
| Manifest decoder            | flat-field rejection (clean YAML spec); everything else delegates to the shared loader   |
| Harness (`validate_config`) | blob field vocabulary, per declared blob at load and per merged blob at resolve          |
| Harness (`preflight`)       | pre-resolve, dependency-blind readiness (config-secret resolvability; near-empty today)  |
| Harness (`runup`)           | post-resolve target-environment check in the real session environment (today: commands)  |
| Registry finalize           | unknown `harness` name via the kind's error miss policy (declared references only)       |
| Sessions manager            | orchestration order, instance construction, `RunContext` assembly, dispatch, {{var}} sub |

All errors are the standard typed `AgentworksError` subclasses; the layer determines the framing.

## Design decisions

### The context carries transports, not a new execution abstraction

"Execute arbitrary code as the target user" could have grown a script-upload mechanism or a
harness-side agent. It doesn't need to: the manager already holds a user-bound transport for the
launch at every dispatch point, and placing it on the `RunContext` (`agent_target` / `admin_target`,
the fields the model already defines for exactly this) gives multi-command probe ability with the
existing logging, quoting, and error conventions. The cost model is honest too: each target exec is
one SSH exec, so a harness author sees exactly what a probe costs and can prefer the in-pane runtime
check (below) when one round-trip matters. The harness being the first real consumer of these fields
is a first-use, not a model change: the fields exist, present-or-`None`, precisely so a capability
that runs on the target can be handed them.

### Two channels, one granted by default (falls out of `RunContext`)

The pre-realignment draft reserved a future `run_admin` channel behind an explicit grant. The new
model already provides it: `RunContext` carries BOTH `admin_target` and `agent_target` as optional
fields, present only when the operation supplies them and (in a future permission model) when the
capability is granted them. So "target user by default, admin behind a grant" is just which of the
two fields the composition root populates for a given harness, no new abstraction. The built-ins use
only the target-user field; a future untrusted plugin harness might be denied `admin_target` (and,
per the payload-only tier below, denied live channels altogether).

A further rung, recorded while it is fresh (recorded, not designed): the grant ladder need not stop
at channel selection. The lowest tier shapes the CONTRACT itself, and the harness already sits close
to it: an op that returns a declarative pane-command string, and a runup expressible as a
declarative probe script the core executes, holds no live channel at all. That tier is qualitatively
different for third-party code, because it is the one rung that is actually ENFORCEABLE: a
zero-callback, data-only contract is trivially hostable out of process, and everything such a
harness contributes executes inside the VM's existing isolation as the target user, the same
boundary Agentworks already trusts for agents themselves. The built-ins need none of this; it is the
natural bottom tier for untrusted plugin harnesses if the plugin SDD ever wants confinement stronger
than distribution trust.

### Names now, full representations reserved (and why that is not a security call)

The context should carry the model's whole identity chain (vm, workspace, agent-or-None, session) as
NAMES, which is what a command-lifecycle harness can actually use (tool addressing, probe context,
error labels). Full representations (the row dataclasses or projections of them) are deliberately
absent until a harness has a concrete need: handing out row types would make their shape part of the
capability contract, and, once plugins arrive, a compatibility surface, for zero current payoff.
When the need lands (artifact placement is the likely trigger), the addition is a new field, purely
additive; whether it exposes the row dataclasses or stable read-only projections is decided then,
with plugin API stability arguing for projections. This is why the proposed `RunContext` identity
value (above) is names-only.

Withholding data is NOT a security mechanism, and this design does not pretend it is. A harness is
in-process Python: it can import the DB, the config loader, and the transports regardless of what
the context hands it, and no in-process sandbox exists to preserve. The trust boundary for
third-party harnesses is plugin installation and enablement (distribution trust, the plugin SDD's
story), not runtime confinement. What the context's minimalism (and the ungranted `admin_target`)
actually buys is misuse-resistance and auditability for cooperating code: a harness that only uses
its handed surface is easy to review, and a grant is an explicit, visible declaration. Valuable
properties; neither confines malice, and the docs say so rather than imply a boundary that is not
there.

### claude-code's existence check: prefer runtime logic in the pane command

FRD R4 leaves the mechanism open between a `ctx.agent_target` probe and runtime shell logic in the
returned command. The HLA's preference (final call at LLD, against the Claude Code CLI surface at
implementation time): fold the check into the launch snippet, so check and launch are a single
invocation. This is the FRD's own "easy strengthening" for the check-to-launch race, and it makes
start/restart symmetry trivial (both return the same resume-or-launch snippet). A `ctx.agent_target`
probe remains the right tool if the detection logic proves too awkward for a readable one-liner; per
the FRD's robustness posture, neither variant aims to be race-proof. Either way this is op-time work
(when `start` / `restart` build the command), not a preflight or runup concern.

### claude-code config vocabulary: pinned v1, reserved future

v1 is `permission_mode` / `model` / `extra_args` (FRD R4). The harness is the natural owner of three
further concerns the FRD records as reserved (not built here), and the config surface is where they
would land: a flag governing whether the launched session inherits the operator's user-level MCP
servers (default non-inheritance, the safe posture against silent tool escalation); a
question-timeout field for unattended sessions; and a Claude-subscription OAuth auth mode. The OAuth
mode is the one that would touch the contract rather than just the vocabulary, since any auth
interactivity must precede the walk-away point; it is deferred until its shape is pinned.
`extra_args` is the escape hatch that keeps v1 from needing a field per flag in the meantime.

### Same-harness blobs merge; the capability owns the merge

A child restating the harness merges per-key (child wins) rather than replacing the blob, because
that is what today's field semantics do once the fields become blob keys: a child overriding just
`command` must not silently drop the parent's `required_commands`. The union semantics of
`required_commands` cannot be expressed by a generic shallow merge, and inventing a core-side
per-key merge vocabulary for an opaque blob would leak capability knowledge into the core; an
optional `merge_config` hook keeps blob semantics with the blob's owner at zero cost to harnesses
that don't care.

### Hoist at the TOML loader, one internal shape

The legacy flat fields could have survived as internal dataclass fields with a compat shim at the
consumers. Rejected on the best-representations rule (internal shapes match YAML shapes; TOML is the
lone divergent domain, mapped at its loader): one internal shape means the resolver, the manager,
the migrator, and the equivalence verification all reason about exactly one encoding, and the flat
fields' eventual retirement (with the TOML resource path, resource-manifests Phase 6) deletes loader
lines, not consumer logic.

### Kind module now, framework untouched

The capability kind reuses `category` / `builtin_override` / miss-policy machinery as-is; nothing
about the harness required a framework hook (the one open item, the `RunContext` identity value, is
a context-object addition, not a framework hook). Adding the capability domain is a package, a kind
module, and a publisher line, the resource-manifests and capability models paying off.

## Open questions / for LLD

- **`RunContext` identity value**: the maintainer decision on whether to add a shared host-agnostic
  identity value to `RunContext` (recommended) versus a harness-specific context object. Pin the
  field's shape (a small frozen value carrying the chain of names, most absent for a `vm` operation,
  all present for a `session` operation) and which composition roots populate it.
- **Populating the target transport on `RunContext`**: the harness is the first consumer of
  `admin_target` / `agent_target`; pin where `create_session` / `restart_session` bind the
  composed-env target transport onto the context, and confirm the admin-mode selection
  (`agent_target` None, `admin_target` set) matches the manager's existing mode handling.
- **Claude Code detection and flags**: how a resumable session named `<session>` is detected (CLI
  listing surface vs on-disk session files) and the exact spellings for `permission_mode` / `model`
  / `extra_args` forwarding -- verify against the latest stable Claude Code CLI at implementation
  time (latest-stable rule), and pin the fixture strategy for testing it without a real `claude`
  binary.
- **Template-variable substitution on harness output**: today's `_substitute_template_vars` runs on
  operator-authored strings; with harness-returned strings it must not mangle legitimate literal
  braces in generated snippets. Pin the escaping (or restrict substitution to shell's
  operator-authored config values) at LLD.
- **`ResolvedSessionTemplate.description` default**: "Login shell" is today's field default; decide
  whether the default description should come from the resolved harness instead (cosmetic only).
- **Consumer inventory**: LLD confirms the only readers of the removed template fields are the
  manager call sites (`env/show.py` touches only `env`; the DB stores the template NAME, so restart
  re-resolves) and sweeps tests/docs for field mentions. Note the stale in-code comment near
  `create_session`'s ephemeral path claiming `create_agent` re-resolves its own tokens; it is wrong
  relative to current behavior and is worth a cleanup commit riding this work.
- **At-use validation home**: whether the merged-blob `validate_config` call lives in
  `resolve_template` or at the manager dispatch points (it must run exactly once per resolution
  either way).
- **`require_commands` helper signature**: the exact shape the relocated probe loop exposes
  (target-label wording, aggregation of missing commands, `check=False` probe semantics against the
  bound transport) -- a mechanical extraction, pinned at LLD.
- **Not an LLD question, recorded as deferred**: the `admin_target` grant vocabulary and enforcement
  (who grants, where it is declared) belongs to the plugin SDD's trust story; this SDD only uses the
  target-user field and keeps the context shape extensible for it.
