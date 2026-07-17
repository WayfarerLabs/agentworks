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

    def preflight(self, ctx: RunContext) -> None:
        """Pre-resolve readiness. A general hook: a harness may do any
        target-independent, secret-free check here (config-derived checks,
        a tool-version probe on an existing target), plus ``super()`` for
        the base's secret-resolvability prediction. For the built-ins the
        one check today is ``required_commands``, run via
        ``base.require_commands(ctx, [...])`` (the relocated probe loop),
        which probes ``ctx.agent_target`` / ``ctx.admin_target`` WHEN the
        target user's entity is not in ``ctx.to_create`` (an existing
        agent/workspace, and every restart), so it runs pre-resolve and
        bails before any prompt. When the target user IS in ``ctx.to_create``
        (a ``--new-agent`` create, whose user is made later this command),
        the probe is deferred to runup; preflight stays dependency-blind.
        Raises StateError; returning means "safe to proceed"."""

    def runup(self, ctx: RunContext) -> None:
        """Post-resolve readiness. Also a general hook: a harness with a
        secret or an authenticated post-provision check does it here, with
        ``ctx.secrets`` in hand. For the built-ins its only use today is the
        ephemeral fallback for ``required_commands``: the same
        ``require_commands`` call, which now finds the just-created target
        no longer in ``ctx.to_create`` and probes it (real workspace,
        composed env). For a non-ephemeral session preflight already
        checked, so runup no-ops. Read-only, like every runup; the mutation
        is the tmux launch that follows. The LLD pins the fired-once
        guard."""

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
  blob is what the instance is constructed against, so `preflight`, `runup`, `start`, and `restart`
  read `self.config` and take only `ctx`. `RunContext.config` is the GLOBAL
  `agentworks.config.Config` (settings), not the harness blob, exactly as it is for every other
  capability.
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
- **`preflight` and `runup` are general readiness hooks; `required_commands` is the one check that
  floats between them by target existence.** A harness may do any pre-resolve, secret-free check in
  preflight (config-derived checks, a tool-version probe) and any post-resolve check in runup
  (authenticated probes once a harness has secrets, post-provision tool-state). The built-ins fill
  only the required-commands slice today, but the hooks are not defined by it. That one check wants
  to run as early as it can (fail before spending the operator's prompt), which is preflight,
  pre-resolve. Preflight is dependency-blind (it may not check state a later step of the command
  creates), so it can only probe a target that already exists. The harness learns which is which
  from the context, EXPLICITLY: the shared `require_commands(ctx, [...])` helper probes the target
  user when that user's entity is not in `ctx.to_create`, and defers when it is. So for an existing
  agent/workspace and every restart the probe runs at preflight, pre-resolve, bailing before any
  prompt; for a `--new-agent` create (agent in `ctx.to_create`, made later this command, the direct
  analog of a git-credential preflight not checking a VM `vm create` has not made yet) it defers to
  runup, where the just-created agent is no longer in `to_create` and gets probed post-provision. No
  `--new-agent` special-case leaks into the harness or the orchestration: the harness never asks "am
  I ephemeral", it reads `ctx.to_create`, and the same helper called from both hooks does the right
  thing because the two stages carry different `to_create` snapshots.
- **Gating on `to_create`, not a `None` target, is a safety property.** The harness defers ONLY when
  the context explicitly says the entity is pending. A target that is missing for any OTHER reason
  (an admin-mode selection, a permission gap, a bug) is not silently treated as "defer"; it surfaces
  as a loud error when the harness tries to use it. Inferring deferral from `agent_target is None`
  would turn every such absence into a SKIPPED readiness check, the exact failure the check exists
  to prevent. (An admin-as-proxy preflight that probed the ephemeral target as admin was also
  rejected: it false-aborts on agent-template user-level tooling. The check runs as the real target
  user or is deferred, never faked.)
- **The `runup` re-check is parity, not new machinery.** Today's `create_session` already probes
  `required_commands` post-provision as the agent user (`sessions/manager.py`, after the ephemeral
  `create_agent`), so a `--new-agent` session is checked after its agent exists in current code too.
  Keeping the runup fallback preserves that; dropping it would leave ephemeral sessions unchecked.
  What the preflight-primary shape adds is strictly earlier failure for the paths that can afford it
  (an existing-agent create now fails before the prompt, where today it checks after).
- **The check needs no secret, and that is fine at either stage.** The preflight/runup boundary is
  the secret-resolve pass, not an auth boundary, so a non-authenticated check on either side is
  within contract. That is why required-commands can sit in preflight (pre-resolve) for the common
  path and fall back to runup (post-provision) for the ephemeral one without any secret entering the
  picture.
- **`start` and `restart` are separate ops** rather than one op with a flag: they are the contract's
  verbs (FRD R3/R7 vocabulary), implementations that differ (shell) read naturally, and
  implementations that coincide (claude-code, where both reduce to resume-or-launch) share a private
  helper. A future restart-specific concern (pre-resume cleanup) has an obvious home.
- **Errors**: `validate_config` raises `ConfigError`; `preflight` / `runup` / `start` / `restart`
  raise the standard typed errors (`StateError`, `ExternalError`) with `entity_kind="session"`,
  which the CLI already renders.

### The identity chain on `RunContext`

`RunContext` today carries `config`, `admin_target`, `agent_target`, `secrets`, and nothing else. A
harness needs the model's identity chain by NAME: at minimum the SESSION name to address the tool
session (`claude --name <session>`, distinct from the template name, which is the harness's
`owner_name`), and the vm / workspace / agent names for probe context and error labels. None of that
is config, `owner`, or a target's `describe()` string.

This rhymes with the capability model's own open question about enriching context beyond the
validate-time `owner` string:

> `owner` is a host-agnostic string today. If a second consumer (preflight's richer context is the
> likely trigger) needs more than a name, the right evolution is a small host-agnostic context
> value, not passing the consuming resource, designed once, when two real consumers reveal its
> shape.

That note is about `owner` (read at validate/preflight) and counsels waiting for two consumers; this
is a different field (`RunContext.identity`, read at readiness and ops) and one consumer, so take it
as precedent for the SHAPE (a small host-agnostic value, not the consuming resource itself), not as
a mandate to design now. What settles doing it in this SDD is the maintainer ruling (2026-07-16),
and the ruling is that identity is a real INVARIANT, not an optional extra: a `RunContext` without
the operation's identity is an incomplete object, so identity is REQUIRED and the object enforces
it.

The enforcement turns on a `level`. **A level is the scope of the specific capability invocation:
what entity THAT call concerns**, not the ambient command and not where the consuming resource is
declared. That distinction is what makes it coherent within one command: in a single `vm create` the
vm-platform preflight concerns the VM being made (VM level) while each git-credential readiness call
concerns a system-global credential and its token (SYSTEM level), so the two carry different-level
identities in the same command. Each `RunContext` is already built per capability call, so
per-invocation level matches how the code is shaped.

```python
# capabilities/base.py (added by this SDD)

class ContextLevel(Enum):
    """The scope level a capability invocation runs at; the identity's
    field set follows from it. The full model hierarchy is enumerated;
    only the levels a call site constructs today are implemented (see
    OperationIdentity.__post_init__)."""

    SYSTEM = "system"        # scope: the whole installation, no VM
    VM = "vm"                # a VM
    WORKSPACE = "workspace"  # a workspace on a VM        (reserved)
    ADMIN = "admin"          # the admin user on a VM     (reserved)
    AGENT = "agent"          # an agent user on a VM       (reserved)
    SESSION = "session"      # a harness as agent-or-admin, in a workspace, on a VM


@dataclass(frozen=True)
class OperationIdentity:
    """The invocation's identity, pinned to a LEVEL. __post_init__ enforces
    that exactly the level's fields are set and the rest are None, so an
    identity inconsistent with its level (a SESSION with no workspace, a VM
    op naming an agent) cannot be constructed.

    Only the levels a call site constructs today (SYSTEM, VM, SESSION) have
    their field rules implemented; WORKSPACE / ADMIN / AGENT are kept in
    the enum to document the full hierarchy but raise NotImplementedError
    on construction (their required/forbidden rules are pinned when a real
    call site first needs them). Their name fields still exist and are
    validated WITHIN SESSION.

    The system slug (the installation identifier, added recently; may be
    blank or unset, so str | None) anchors every level. NAMES only,
    deliberately (see the "Names now" design decision); room reserved for
    fuller representations as a future additive field.
    """

    level: ContextLevel
    system_slug: str | None = None
    vm_name: str | None = None
    workspace_name: str | None = None
    agent_name: str | None = None
    session_name: str | None = None
    admin: bool = False
    # __post_init__: SYSTEM/VM/SESSION field rules enforced (per the table
    #   below); WORKSPACE/ADMIN/AGENT raise NotImplementedError for now.
```

The level-to-fields table the object enforces (system_slug is allowed at every level and is the only
field allowed at SYSTEM):

- **SYSTEM**: system_slug only; vm/workspace/agent/session all None, admin False. Scope is the
  entire installation, not any VM: the level for the system-global capabilities, `secret-backend`
  readiness (am I installed, can I reach the vault) and `git-credential-provider` readiness (the
  credential and its token are declared once for the system, and the probe hits the git host, not a
  VM), and for cross-system scans like doctor's vm-site config check. A git-credential readiness
  call is SYSTEM even when it runs inside `vm create` or agent init, because it concerns the
  system-global credential, not the VM. The credential or backend's OWN name is its `owner_name`,
  not identity.
- **VM**: vm_name set; workspace/agent/session None, admin False.
- **SESSION**: vm_name + workspace_name + session_name set, and exactly one of (agent_name set,
  admin True).
- **WORKSPACE / ADMIN / AGENT**: reserved. Kept in the enum to document the full model hierarchy,
  but no operation constructs one today, so constructing an identity at these levels raises
  NotImplementedError; the rules shown by the pre-realignment sketch (WORKSPACE = vm + workspace,
  ADMIN = vm + admin, AGENT = vm + agent) are the expected shapes, pinned when a real call site
  appears.

Three things follow:

- **`identity` is a required field on `RunContext`** (the dataclass becomes `kw_only=True` so the
  required field composes with the existing defaulted ones). There is no `None` default to fall
  through: every construction site passes an identity at the right level. This SDD threads it
  through all fourteen existing construction sites: thirteen live in `vms/manager.py`,
  `agents/manager.py`, `git_credentials/__init__.py`, and `doctor.py`, and the fourteenth already
  exists in `sessions/manager.py` (the ephemeral-agent git-credential preflight this SDD also builds
  on). By the per-invocation rule: vm-create/reinit platform readiness -> VM; every git-credential
  readiness call -> SYSTEM (including the ones inside vm and agent init); doctor's site scan ->
  SYSTEM; and the harness's own calls -> SESSION. All fourteen already build
  `RunContext(config=...)` by keyword, so the edit is one added argument per call, not a call-shape
  break.
- **The level resolves what the earlier coherence sketch left open.** A SESSION always has a
  workspace (so `session_name` implies `workspace_name`), and admin-vs-agent is the SESSION level's
  one either/or, not a free-floating flag; doctor's placement-free scan is simply SYSTEM level, not
  a hand-built "empty" identity. The capability's OWN name stays its `owner_name` (a vm-site's name,
  a credential's name); identity is the invocation's scope, which is why a git-credential readiness
  call is SYSTEM and not "git-credential level".
- **`identity` stays stable; existence is separate.** `config` / `admin_target` / `agent_target` /
  `secrets` stay optional and timing-populated. `OperationIdentity` itself is fixed at command entry
  (stable names and level), which is why it can be required. What is NOT stable, and is added here
  explicitly rather than inferred, is which of its named entities exist yet: `RunContext` gains a
  `to_create` field, the set of entities the command will create that do NOT exist at this stage
  (`{"agent", "workspace"}` for a `--new-agent --new-workspace` create at command start, and empty
  once they are provisioned). This is stage-state, like the targets, so it lives on `RunContext`
  beside them, not on the stable identity. (The one caveat the FRD notes: the system slug can be
  prompted once on a first-ever create, resolved before any `RunContext` is built, so "fixed at
  entry" describes the identity every readiness stage sees, not a literally untouched world.)

Why `to_create` is explicit and not read off a `None` target: a capability must be able to tell
"this entity will exist but does not yet" (defer the check) from "there is no such target here at
all" (an admin-mode selection, a permission gap, or a bug). If deferral were inferred from
`agent_target is None`, any of those other absences would SILENTLY SKIP a readiness check instead of
raising, which is exactly the failure a readiness check exists to prevent. With `to_create`
explicit, a harness defers only when the context says the entity is genuinely pending, and a target
that is missing for any other reason surfaces as a loud error, not a skipped check.

On ephemerals, then: a `session create --new-agent --new-workspace` names resources that do not
exist yet, but this is a NON-ISSUE for the IDENTITY (the orchestration layer chooses the names up
front, so a SESSION-level identity always carries valid names), and the existence question is
answered by `to_create` rather than by any missing name or target. Identity is stable names;
`to_create` is the stage's honest statement of what is not built yet.

The harness runs at SESSION level: it picks `ctx.agent_target` vs `ctx.admin_target` off
`ctx.identity.admin`, addresses the tool session by `ctx.identity.session_name`, and consults
`ctx.to_create` to decide whether its target user/workspace exist yet. Because its composition root
always supplies a full SESSION-level identity, the harness relies on those names being present, not
merely typed as optional.

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
(FRD R7: the resolved pair is validated at use); a merged blob is a new value no single declaration
ever saw, so declared-blob validation alone cannot cover it. This is also where completeness lives:
declared blobs validate vocabulary/shape only (a restating child may be partial), and any
required-field or cross-field rule a harness has is checked here, against the merged whole.

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
- `_assert_required_commands`'s probe loop, docstring rationale, and error shape LEAVE the session
  entirely (today they are inline in `create_session`) and relocate verbatim to
  `capabilities/harness/base.py` as the `require_commands(ctx, commands, ...)` helper, which probes
  the target user when that user's entity is not in `ctx.to_create` and defers when it is. The
  manager then does the STANDARD, UNIFORM two-hook readiness for every session, not branching on
  ephemeral: `harness.preflight(ctx)` in the preflight-all phase and `harness.runup(ctx)` before the
  launch op. Both hooks are thin wrappers over the one helper, and a fired-once guard makes the
  actual probe run exactly once, at whichever hook first sees the target user out of `ctx.to_create`
  (preflight for an existing agent/workspace and every restart; runup for a just-provisioned
  ephemeral). The session owns none of the check's logic; it only builds each stage's `RunContext`
  (including `to_create`) and calls the standard hooks.

Sequencing note (pinned so the LLD preserves it): on restart the target already exists, so
`harness.preflight` (required-commands) runs pre-resolve and pre-kill, and the ephemeral runup
fallback never fires; the `restart` op runs AFTER the kill, which is exactly what claude-code needs,
since Claude Code's session state is only settled once the old process is dead. On create, the
required-commands check is preflight for an existing agent/workspace and the runup fallback for an
ephemeral one; the `start` op runs last, before `tmux.create_session`. The manager holds the
correctly-bound target transport at each of these points; the new work is placing it, and the
identity, on the `RunContext`.

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

Probes get the env appropriate to their stage. The primary required-commands check runs at
preflight, pre-resolve, so it sees the target's login env, exactly as today's
`_assert_required_commands` does (no regression, and adequate for checking a binary on the base
PATH). The ephemeral runup fallback runs post-resolve, so its target transport is bound with the
composed env (the same `SetEnv` delivery the pane's SSH connection uses), which is a small upgrade
for that one path (it sees PATH-affecting values the session itself gets). Probes run in the
workspace directory, matching the pane's working directory (for the ephemeral runup path the
workspace exists by then; ephemerals are provisioned before runup).

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
- `capabilities/README.md`: the lifecycle doc's `RunContext` description says "every field is
  optional"; this SDD updates that line to carve out `identity` as the required exception (known at
  command entry, unlike the timing-populated fields), so the capability model doc and the code
  agree.
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
boundary (one prompt session), then per-phase runup-then-ops. The harness is one participant in that
order, not its author: the prompts-up-front-then-walk-away consistency is a property of the resolve
and isolation machinery around it, and the harness contributes its readiness and its two ops at the
points shown. Applied to create:

1. `build_registry(config)`, once. Harness rows publish; a template's declared `harness` reference
   validates at finalize, so a typo'd name dies here, before any prompt or mutation.
2. Flag-shape validation, DB checks, operator prompting (unchanged).
3. Template resolution -> `(harness, harness_config)`; merged-blob `validate_config`; construct the
   harness instance against the operation `Resolver`. A bad blob fails before any prompt or
   mutation.
4. Ensure the VM is running (unchanged; precedes the resolve today). Interactive by design when a
   stopped VM needs a tailscale auth key, interactivity that rightly precedes the walk-away point.
   Power-state convergence is idempotent declared-state maintenance, not a rollback-tracked
   mutation; called out so "any state change" in the invariant reads precisely.
5. Preflight-all: the harness's `preflight(ctx)` alongside every other participating resource's, all
   against the command-start
   `RunContext(identity=..., config=config, agent_target=..., to_create=...)` (identity always
   present, no secrets). For a session on an EXISTING agent/workspace `to_create` is empty and the
   context carries the target, so the harness preflight probes `required_commands` here,
   pre-resolve, bailing before any prompt. For a `--new-agent` / `--new-workspace` session those
   entities are in `to_create`, so that probe defers to step 8. Everything in this step is before
   any prompt or mutation.
6. The single secret resolve (`Resolver.resolve()`; the union across every instance registered on
   it, one batched prompt session) and env composition. When `--new-agent` is in play, the ephemeral
   agent's git-credential tokens are already folded into this resolve on main (constructed against
   the same resolver; their values thread through `create_agent`, which skips its own resolve). The
   harness adds no resolve calls and no prompts. **This is the walk-away point.**
7. Ephemeral workspace/agent provisioning (unchanged; rollback-protected from here down), then
   target preparation. Nothing in this block prompts.
8. `harness.runup(ctx)` against the op-start `RunContext` (config, resolved `secrets`, the target
   transport, the identity, and now-empty `to_create`), in the real environment. This is the
   EPHEMERAL FALLBACK: the just-created agent/workspace are no longer in `to_create`, so the
   `required_commands` probe that deferred in step 5 runs here; for a non-ephemeral session it
   already ran at preflight, so runup no-ops. A failure rolls back the ephemerals and reports.
9. `harness.start(ctx)` -> pane command; template-var substitution; `tmux.create_session` with the
   composed env.

`restart_session` runs against the existing session (template re-resolved by the stored name,
instance reconstructed). The target always exists on restart, so the harness readiness check is a
plain preflight and runs BEFORE the resolve, which restores today's discipline: a declined or doomed
restart never prompts for secrets it would discard, and a missing binary aborts before any prompt.
Sequence: `harness.preflight(ctx)` (required-commands, pre-resolve) -> resolve the session ENV chain
via the legacy `resolve_for_command` AFTER the BROKEN/confirm gates (as on main) -> compose -> kill
-> `harness.restart(ctx)` (after the kill, the claude-code sequencing requirement); the ephemeral
runup fallback never fires on restart. A bad blob, a missing binary, or an unresolvable secret all
abort with the old session still running. Past the kill, the failure contract changes shape (FRD
R7): a `restart` op or tmux failure cannot restore the old session, so the pinned end state is
session row intact, old tmux gone, `agw session restart` cleanly retryable, and an error naming the
failed step; no resurrection is attempted.

## Validation responsibilities

| Layer                       | Owns                                                                                     |
| --------------------------- | ---------------------------------------------------------------------------------------- |
| TOML loader                 | section/key shapes, flat-field hoist, flat+non-shell and flat+blob conflict errors       |
| Manifest decoder            | flat-field rejection (clean YAML spec); everything else delegates to the shared loader   |
| Harness (`validate_config`) | blob field vocabulary, per declared blob at load and per merged blob at resolve          |
| Harness (`preflight`)       | pre-resolve readiness; required-commands probe when target not in `ctx.to_create`        |
| Harness (`runup`)           | post-resolve readiness; required-commands fallback once target leaves `ctx.to_create`    |
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

`OperationIdentity` carries the model's whole identity chain (system slug, vm, workspace,
agent-or-admin, session), keyed by level, as NAMES, which is what a command-lifecycle harness can
actually use (tool addressing, probe context, error labels). Full representations (the row
dataclasses or projections of them) are deliberately absent until a harness has a concrete need:
handing out row types would make their shape part of the capability contract, and, once plugins
arrive, a compatibility surface, for zero current payoff. When the need lands (artifact placement is
the likely trigger), the addition is a new field on `OperationIdentity`, purely additive; whether it
exposes the row dataclasses or stable read-only projections is decided then, with plugin API
stability arguing for projections. This is why the `RunContext.identity` value is names-only today.

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

### Kind module now, no framework hook

The capability KIND reuses `category` / `builtin_override` / miss-policy machinery as-is; nothing
about the harness required a new framework HOOK, so adding the harness domain itself is a package, a
kind module, and a publisher line. The one genuinely cross-cutting change is the
`RunContext.identity` invariant: `OperationIdentity` plus the required field in
`capabilities/base.py`, the one-line identity argument added at all fourteen existing `RunContext`
construction sites, and the README carve-out. That is a deliberate MODEL change, landing a final
identity API rather than an optional bolt-on, not framework plumbing, and it is bounded: every site
already builds `RunContext` by keyword, so each is a single added argument.

## Open questions / for LLD

- **`RunContext.identity` level per site**: identity is required (see the identity section), so
  every construction site passes one at the per-invocation level. The assignment is settled by the
  rule (level = what the call concerns): vm-create/reinit/rekey platform readiness -> VM; every
  git-credential readiness call -> SYSTEM (the credential and its token are system-global, the probe
  hits the git host, not a VM; this covers the sites in `create_vm`, agent init, and
  `vm add-git-credential`); doctor's site scan -> SYSTEM; the harness -> SESSION. The LLD just
  applies it per site; no WORKSPACE / ADMIN / AGENT identity is constructed (those enum values raise
  NotImplementedError until a real call site defines their rules).
- **Populating the target transport and `to_create` on `RunContext`**: the harness is the first
  consumer of `agent_target` / `admin_target`, and it reads them at PREFLIGHT (for an existing
  agent/workspace and every restart), not only at runup. Pin where `create_session` /
  `restart_session` bind the target transport and set `to_create` on each stage's context (the
  ephemeral entities are in `to_create` at command-start and gone by runup), pin the shape of
  `to_create` (a frozenset of an entity enum vs bools), and confirm the admin-mode selection
  (`agent_target` None, `admin_target` set, admin never in `to_create`) matches the manager's
  existing mode handling.
- **The required-commands single-fire guard**: `harness.preflight` and `harness.runup` both delegate
  to `require_commands(ctx, ...)`, which probes when the target user is not in `ctx.to_create` and
  defers when it is. Pin the guard that makes the probe fire exactly once (at preflight for a
  non-ephemeral session; at runup for the ephemeral one), avoiding a redundant second probe on the
  non-ephemeral path where the target is out of `to_create` at both stages.
- **Claude Code detection and flags**: how a resumable session named `<session>` is detected (CLI
  listing surface vs on-disk session files) and the exact spellings for `permission_mode` / `model`
  / `extra_args` forwarding; verify against the latest stable Claude Code CLI at implementation time
  (latest-stable rule), and pin the fixture strategy for testing it without a real `claude` binary.
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
  bound transport), a mechanical extraction, pinned at LLD.
- **Not an LLD question, recorded as deferred**: the `admin_target` grant vocabulary and enforcement
  (who grants, where it is declared) belongs to the plugin SDD's trust story; this SDD only uses the
  target-user field and keeps the context shape extensible for it.
