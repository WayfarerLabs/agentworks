# Session harness capability: high-level architecture

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

## Overview

The harness is a new capability in the `capabilities/` subtree, built on two merged foundations: the
capability model (ADR 0016, `capabilities/README.md`) and the orchestration layer
(`docs/sdd/2026-07-16-orchestration-layer`, merged and locked). The orchestration layer is what
makes this SDD small. It already built, as general machinery, everything an earlier draft of this
HLA tried to invent harness-side: the `Readiness` / `Node` split, `OperationScope` / `ScopeLevel`,
pending nodes and the realized/defer signal, the boundary resolve and walk-away ordering, unwind,
and the `RunContext` accessor surface. It even shipped the harness's seat: the merged session node
(`sessions/nodes.py`) holds a `Readiness` stand-in, `RequiredCommandsCheck`, described in its own
code as "the harness-LIKE stand-in the harness capability replaces when it lands," and drives an
interim imperative pane-command path (`_build_session_command`).

So this HLA pins one thing: **the harness is a `Readiness` capability the session node holds, and
this SDD swaps it in for the interim stand-in (readiness) and for `_build_session_command` (the
ops).** The harness is not a graph node; it has no `key` and no `deps`; the orchestrator never walks
it. It is held by the session node and composed by that node's `preflight` / `runup`, exactly as the
merged code already holds and composes `RequiredCommandsCheck`.

```text
declaration                                registry
+------------------------------+          harness/shell, harness/claude-code
| session-template             |   ref    (capability rows, built-in, error miss)
|   spec.harness ------------- + -------->        ^
|   spec.harness_config (blob) |                  | publish_to
| (TOML flat fields hoist to   |          capabilities/harness/ (HARNESS_REGISTRY)
|  shell's blob at the loader) |
+------------------------------+
             |
             v  resolve -> (harness, harness_config)
   sessions/nodes.py: the SESSION NODE (a Node)
     holds a harness INSTANCE (a Readiness), constructed by the session factory
       preflight/runup  -> harness.preflight/runup   (readiness: required-commands fork)
       start/restart op -> harness.start/restart(ctx) (returns the pane command string)
     the orchestrator walks the session node; the harness is held, never walked
```

Everything else the session path does (target preparation, env composition, the boundary secret
resolve, tmux hosting, liveness, unwind) is the merged orchestration layer's and is untouched. The
FRD's model change lands architecturally as: the session stops carrying command strings and carries
a `(harness, harness_config)` pair, and the session node holds the capability behind it.

## Package layout

```text
cli/agentworks/capabilities/harness/
  __init__.py         # public surface: HARNESS_REGISTRY, harness_for(), publish_to()
  base.py             # Harness(Capability) ABC, the required-commands probe, merge_config default
  kinds.py            # capability kind strategy + HarnessEntry row (domains own their kinds)
  shell.py            # built-in 'shell' harness (command/restart_command/required_commands)
  claude_code.py      # built-in 'claude-code' harness (launch-vs-resume state logic)
```

Mirrors `capabilities/git_credential/` exactly: a `base.py` with the `Capability` subclass and
shared helpers, per-member modules, a `kinds.py` following `_GitCredentialProviderKind`, and an
`__init__.py` carrying `HARNESS_REGISTRY` (name -> class) plus `publish_to`. Pure Python, no Typer
dependency (typer-isolation rule), and no import of the `sessions/` domain or the `orchestration/`
package (the harness depends only on the framework and on the `Readiness` contract it satisfies
structurally).

`harness/kinds.py` defines `_HarnessKind` (`category = "capability"`, `miss_policy = "error"`,
`builtin_override = "reserved"`, `auto_declare_names = None`, a `synthesize` that raises
`NoUnreferencedDefaultError`) and a frozen `HarnessEntry(name, origin, references)` row, and
registers into `KIND_REGISTRY` at import. The one-line import is added to
`resources/kinds/__init__.py`, the pure index that populates `KIND_REGISTRY` per domain.

`publish_to(registry)` adds one `HarnessEntry` per registered harness with
`Origin.built_in(source="agentworks.capabilities.harness")`; `bootstrap.build_registry` gains the
call in the built-in publisher block alongside `git_credential.publish_to` / `secrets.publish_to`.

The required-commands probe body (today the merged `RequiredCommandsCheck._probe`, the
`$SHELL -lic 'command -v <cmd>'` loop and its error shape) relocates into `harness/base.py` as a
shared helper the `shell` and `claude-code` harnesses call; the interim `RequiredCommandsCheck`
retires when the swap lands.

## The Harness API

A harness is a `Capability` (so it satisfies `Readiness` and gets the
registry/kind/`validate_config` machinery) whose ops are `start` / `restart`. Beyond the base
`(owner_name, config)` it is constructed with the session's OWN identity, its name plus its
row-carried ancestors (vm, workspace, agent-or-admin), the same values the merged
`RequiredCommandsCheck` takes plus `workspace_name`. Everything else the harness reads at call time
from the `RunContext`, but its identity is its own (see the design decision), not the operation
scope's.

```python
# capabilities/harness/base.py (pseudocode-level; exact types at LLD)

class Harness(Capability):
    """The session-domain capability: how a named session of a tool runs.

    A Capability (so it satisfies Readiness and gets validate_config,
    the registry row, and preflight/runup), HELD by the session node and
    composed by it, never a graph node itself. owner_kind =
    "session-template". Non-interactive by contract: no prompts in
    validate_config, preflight, runup, start, or restart (all operator
    interaction precedes the boundary resolve the orchestrator owns).
    """

    owner_kind = "session-template"

    def __init__(
        self,
        owner_name: str,            # the session-template name (config owner)
        config: Mapping[str, object],  # the merged harness_config blob
        *,
        session_name: str,          # the session's own name (addresses the tool)
        vm_name: str,               # the session's VM ancestor
        workspace_name: str,        # the session's workspace ancestor
        target: Node | None,        # the agent node it runs as; None in admin mode
        admin: bool,                # admin mode (uses ctx.admin_target())
    ) -> None: ...
    # Construction captures the SESSION's OWN identity: its name plus its
    # row-carried ancestors (vm, workspace, and agent-or-admin, the last
    # via target+admin). This is layer-1 identity, threaded from the
    # session's rows by the factory, and it is the harness's OWN identity,
    # distinct from ctx.operation_scope (the OPERATION's identity, layer
    # 2). The two coincide for a session command, but the harness owns its
    # identity rather than trusting the operation context to be about it:
    # at SESSION level it VERIFIES the scope's names match its own (belt
    # and suspenders against a mis-wired ctx handing it the wrong VM or
    # agent), and it reads only the LEVEL off the scope for the
    # skip/defer/probe fork. It addresses and frames through its own
    # identity, never through the scope's names.

    @classmethod
    def validate_config(
        cls, owner: str, config: Mapping[str, object]
    ) -> tuple[ConfigReference, ...]:
        """Validate the harness_config blob owned by `owner`
        (e.g. "session-template/claude") and return the references it
        implies. Both built-ins accept only their own vocabulary and imply
        none, returning (). VOCABULARY AND SHAPE only (unknown fields are
        errors, FRD R4); completeness rules are checked on the merged blob
        at resolve (FRD R2/R5). A classmethod: called at load with no
        instance."""

    # preflight(ctx) / runup(ctx): the required-commands readiness, the
    #   four-way fork the merged RequiredCommandsCheck already implements
    #   (see "Readiness" below). Inherited-shape from Capability
    #   (Readiness); the shell/claude-code harnesses fill them.

    def start(self, ctx: RunContext) -> str:
        """Op: the pane command for a fresh `session create`. Reads its
        blob from self.config and the session name from self._session_name.
        Empty string means login shell only. Replaces the interim
        _build_session_command for create."""

    def restart(self, ctx: RunContext) -> str:
        """Op: the pane command for `session restart` (invoked after the
        old session is killed). Replaces _build_session_command for
        restart."""

    # BASE classmethod with a shallow default (the ABC supplies it, so
    # the resolver calls it unconditionally, no getattr guard):
    #   def merge_config(cls, base: Mapping, child: Mapping) -> dict
    # Inheritance-time blob merge for same-harness parent/child pairs;
    # the default is shallow {**base, **child}. `shell` overrides it to
    # union required_commands (parity with today's append-dedupe).
```

Contract points:

- **The harness is a `Capability`, not a plain `Readiness`, because it needs the registry.** It has
  a kind, a row in `agw resource list`, and `validate_config` for its blob; `Capability` provides
  all of that and satisfies `Readiness` (its `preflight`/`runup`). The instance is what the session
  node holds; the class is what `HARNESS_REGISTRY` publishes and `validate_config` runs on at load.
- **Construction captures the session's OWN identity; the blob is the config.** `owner_name` is the
  session-template name (the config owner, for `validate_config` framing); `config` is the merged
  `harness_config`. The `session_name` / `vm_name` / `workspace_name` / `target` / `admin` kwargs
  are the session's layer-1 identity, its name plus its row-carried ancestors, supplied by the
  session factory from the session's rows (extending the set the merged `RequiredCommandsCheck`
  takes with `workspace_name`, so the full chain is present for framing and verification). The
  harness addresses the tool session by its OWN `session_name` (`claude --name <session>`) and
  frames its errors with its own vm/agent, never through `ctx.operation_scope`'s names, which are
  the OPERATION's identity (layer 2), a different thing that only coincides. From the scope the
  harness reads the LEVEL (the skip/defer/probe fork, below) and, at SESSION level, VERIFIES the
  scope's names match its own as a belt-and-suspenders guard against a mis-wired context (a harness
  executes commands on a VM as a user, so being handed a context for the wrong session is exactly
  what this catches). No bound resolver: `Capability` construction is `(owner_name, config)` on the
  merged base, and runup/ops read secrets through `ctx.secret(name)`.
- **The return of `start`/`restart` is a pane command string, and that is the whole tmux story.**
  The session path applies template-variable substitution and `exec` wrapping and hands it to tmux,
  exactly as `_build_session_command`'s output is handled today. The harness never sees tmux: it
  decides WHAT runs in the pane; returning a string keeps "never HOW" structural. A multi-step
  launch is a compound shell snippet in the string.
- **`ctx.agent_target()` / `ctx.admin_target()` are the execution surface.** The harness runs
  commands on the launch target as the target user through these merged `RunContext` accessors
  (agent in normal mode, admin in admin mode). `shell` uses none of it (static string ops);
  `claude-code` uses it for the resume-vs-launch detection (R4). Harness logic runs CLI-side; only
  the commands it issues run on the target.
- **Errors** are the standard typed subclasses (`ConfigError` from `validate_config`; `StateError` /
  `ExternalError` from the rest) with `entity_kind="session"`, which the CLI already renders.

### Readiness: the required-commands fork, inherited from the merged model

The harness's `preflight` / `runup` ARE the target-environment check, and its shape is already
merged as `RequiredCommandsCheck`; the harness inherits it verbatim (the merged code says so). The
fork reads the operation scope's LEVEL and the target node's realized state:

- **no scope at all** (`ctx.operation_scope is None`): a LOUD error. A scope-reading node handed a
  scope-less context is an orchestrator bug, never a silent skip (the merged check raises a
  `StateError` here today, and the harness preserves it). This is the precondition to the level fork
  below.
- **out of scope for the level** (a system-scoped doctor scan reaching a session,
  `ctx.operation_scope .level is not ScopeLevel.SESSION`): SKIP, a legitimate no-op. This is how
  doctor gets a harness health row with no session context and no special-casing.
- **in scope, target pending** (`self._target is not None and not self._target.realized`): DEFER to
  runup (the probe needs a real user on a real VM, which a `--new-agent` create makes later).
- **in scope, target realized** (or admin mode, whose admin target always exists): PROBE now, at
  preflight, pre-resolve, so an existing agent/workspace and every restart fail before any prompt.
- **in scope, target absent for another reason** (agent mode, no target node): a LOUD error, never a
  silent skip.

At SESSION level (every branch except the SKIP), before it acts, the harness first VERIFIES the
scope's identity matches its own captured identity (`scope.session == self._session_name`, same for
`vm` / `workspace`, and the agent-or-admin choice); a mismatch is a loud error, the belt-and-
suspenders against a mis-wired context. The SKIP branch does no such check, because at SYSTEM level
the scope legitimately describes a broader operation than this session (that is what the skip is
for). This guard does not exist on the interim `RequiredCommandsCheck` (confirmed at HEAD), so it
arrives with the harness; the harness owns its identity regardless.

Two things this inherits from the merged model rather than reinvents:

- **Pending-ness lives on the node, not on the context.** The harness reads `self._target.realized`,
  the flag the orchestrator flips via `mark_realized`. There is no `to_create` context field (an
  earlier draft of this SDD proposed one; the merged model made it unnecessary). The one-object
  contract makes this work: the merged session factory hands the SAME agent node to both the
  session's dependency edge and the harness's `target`, so the flip the orchestrator makes is the
  flip the harness sees.
- **The single-fire guard.** `preflight` and `runup` both run the probe helper, which no-ops once it
  has fired (the merged `RequiredCommandsCheck._probed` flag), so the probe runs exactly once: at
  preflight for a target that already exists, at runup for one just realized. `preflight` and
  `runup` stay general hooks; a future harness may add other checks to either.

## How the harness plugs into the merged session node

This is the swap, and it is small because the merged code was written for it.

- **`sessions/nodes.py` construction.** The `pending_session_node` / `live_session_node` factories
  today build a `RequiredCommandsCheck` from the resolved template's `required_commands` and hand it
  to the session node as its held readiness. The swap: they instead construct
  `harness_for(resolved.harness)(...)` (the template name and `harness_config` positionally, then
  the session's own identity as kwargs: `session_name` / `vm_name` / `workspace_name` / `target` /
  `admin`) and hand THAT to the session node. The factory has all these on hand already, from the
  same rows it builds the check from; it adds `workspace_name`, which the check does not currently
  take. The one-object target wiring the factory already enforces (the same agent node as the
  session's dep and the harness's `target`) carries over unchanged; the harness takes the agent node
  where the check did.
- **The session node composes it, unchanged.** `LiveSessionNode` / `PendingSessionNode` already
  delegate `preflight` / `runup` to their held readiness (`self._check.preflight(ctx)`); after the
  swap they delegate to the held harness (`self._harness.preflight(ctx)`). `deps()`,
  `secret_refs()`, `mark_realized()`, `teardown()`, and the key stay as they are, except
  `secret_refs()` now folds in the harness's declared secrets (none for the built-ins; the plumbing
  is there for a future harness).
- **The session orchestrator's ops.** Where the session create/restart orchestrator calls
  `_build_session_command` today to get the pane command string, it calls `harness.start(ctx)` /
  `harness.restart(ctx)`. Two wiring details the swap must handle, since `_build_session_command`
  takes no context today: the call site ASSEMBLES an op-start `RunContext` (the execution targets
  and the secrets scoped to the session node's `secret_refs()`, mirroring the readiness context the
  merged path already builds, the runup context on create and the preflight context on restart,
  since the restart path retains no runup context and must capture the boundary resolver's values
  for the op ctx), and template-variable substitution, which currently lives INSIDE
  `_build_session_command`, lifts out to wrap the harness's returned string. `exec` wrapping and
  tmux creation stay where they are, operating on that string exactly as on
  `_build_session_command`'s output. (The plan pins both as Phase 3 items and the harness-api LLD
  details them.)
- **Restart ordering is the orchestrator's, preserved.** On restart the target exists, so readiness
  probes at preflight (pre-resolve, pre-kill), and `restart` is called after the kill (claude-code
  needs the old process dead before it decides resume-vs-launch). The merged orchestrator already
  sequences this for the interim path; the harness slots into the same points.

## Layer changes

### Declaration layer (`sessions/template.py` + `config.py`)

`SessionTemplate` lives in `sessions/template.py` (domains own their dataclasses and kinds;
`config.py` keeps settings and the legacy TOML loaders). The reshape, following the
best-representations rule (internal shape = YAML shape):

- Fields `command`, `restart_command`, `required_commands` are REMOVED; `harness: str | None` and
  `harness_config: dict[str, object] | None` are added (`None` = not declared, distinct from
  declared-empty for inheritance).
- `_load_session_templates` accepts the new keys plus the legacy flat fields and applies the FRD R6
  rules: flat fields hoist to `harness="shell"` + the equivalent blob; flat + non-`shell` `harness`
  is a `ConfigError`; flat + an explicit `harness_config` table is a `ConfigError`.
- Blob validation dispatches through `HARNESS_REGISTRY[name].validate_config(owner, blob)` (a
  classmethod, no instance) at the git-credential invocation points: the manifest decoder validates
  the true blob with `file:line` framing, the TOML loader its hoisted blob; unknown harness names
  skip invocation so the reference miss policy reports them uniformly at finalize.
- `SessionTemplate.referenced_resources()` emits one `harness`-kind reference when `harness` is
  declared (usage "the session harness"), plus any capability-implied references from
  `validate_config` (none today for either built-in).

### Manifest decoder (`manifests/decode.py`)

`_decode_session_template` rejects the flat fields before delegating (the clean-spec rule, FRD R2),
error pointing at `harness: shell` + `harness_config`; `harness` / `harness_config` pass through to
the shared TOML loader. Same pre-check-then-delegate shape as `_decode_git_credential`'s `type`
rejection.

### Template resolver (`sessions/templates.py`)

`ResolvedSessionTemplate` becomes `(name, description, env, harness: str, harness_config: dict)`,
defaults `("shell", {})`. The merge walk keeps its depth-first, left-to-right order; the pair merges
per FRD R5:

```python
def _merge_pair(acc_name: str | None, acc_config: dict, child: SessionTemplate):
    if child.harness is None:          # child says nothing about the pair
        return acc_name, acc_config    #   (harness_config without harness cannot load)
    base = acc_config if child.harness == acc_name else {}   # fresh blob on switch
    merged = harness_for(child.harness).merge_config(base, child.harness_config or {})
    return child.harness, merged
# after the walk: (None, {}) -> ("shell", {})   # the undeclared default
```

`merge_config` is the class-level hook, invoked without an instance. The resolver validates the
MERGED blob through `validate_config` once resolution completes (a merged blob is a value no single
declaration saw); this is where completeness (required-field, cross-field) rules apply, since
declared blobs validate shape-only and a restating child may be partial.

### Sessions domain (`sessions/nodes.py` + the session orchestrator)

The swap described under "How the harness plugs in": the factories construct a harness instead of a
`RequiredCommandsCheck`; the session node composes the harness's readiness (already the delegation
shape); the orchestrator's `start`/`restart` calls replace `_build_session_command`. The interim
`RequiredCommandsCheck` class is deleted, its probe body relocated to `harness/base.py` as the
shared helper the built-ins use.

### Registry, bootstrap, inspection

- `capabilities/harness/kinds.py` self-registers into `KIND_REGISTRY` at import (one index line in
  `resources/kinds/__init__.py`), making `agw resource kinds`, `--kind harness` filters, the
  `resource_refs` completer, and the envelope's capability-kind rejection all work with no extra
  wiring.
- `bootstrap.build_registry` gains `harness.publish_to(registry)` in the built-in block.
- `agw resource list/describe/kinds` need no code changes; rows and references render through the
  existing framework surfaces (FRD R8).

### Database: unchanged, deliberately

No schema migration. `SessionRow` stores the template NAME (plus placement and tmux lifecycle
fields); neither the pane command nor the harness pair is persisted. Restart re-resolves the
template by the stored name, reconstructs the harness, and dispatches fresh, so harness and config
always come from current declared config, the semantics template edits already have between create
and restart.

### Migration tool (`migrate/planning.py`)

`_emit_document` gains a `session-template` branch mirroring `git-credential`'s: pop the flat
fields; when present, emit `harness: shell` plus a `harness_config` blob built from them; pass
declared `harness` / `harness_config` through. The TOML loader's hoist and this emission land on the
identical internal value, so the per-run registry-equivalence verification proves the divergence is
value-free.

### Samples and docs

- `manifests/samples/session-template.yaml`: rewritten to lead with a `claude-code` document (one
  `harness:` line where the old sample restated three command strings) followed by the `shell` +
  `harness_config` form; stays fully commented per the sample rules.
- Permanent docs per FRD R10, each riding its commit: top-level `README.md` model narrative,
  `cli/README.md` schema/reference, `docs/guides/resources.md` capability story, and
  `capabilities/README.md` gaining the harness as a worked example of a capability held by a rich
  consuming node.
- The ADR is drafted as `adr-session-harness.md` here and promoted/numbered into `docs/adrs/` at the
  end of the effort (FRD R10), referencing ADR 0016 (capability collapse) and the
  orchestration-layer ADR (the node/readiness model the harness plugs into).

## Validation responsibilities

| Layer                         | Owns                                                                            |
| ----------------------------- | ------------------------------------------------------------------------------- |
| TOML loader                   | section/key shapes, flat-field hoist, flat+non-shell and flat+blob conflicts    |
| Manifest decoder              | flat-field rejection (clean YAML spec); everything else delegates to the loader |
| Harness (`validate_config`)   | blob field vocabulary, per declared blob at load and per merged blob at resolve |
| Harness (`preflight`/`runup`) | the required-commands fork (skip/defer/probe/error by scope level + target)     |
| Registry finalize             | unknown `harness` name via the kind's error miss policy (declared refs only)    |
| Session node / orchestrator   | holds and composes the harness; calls its ops; assembles the `RunContext`       |
| Orchestration layer (merged)  | the walk, boundary resolve, scope, pending/realized, unwind, tmux, env          |

All errors are the standard typed `AgentworksError` subclasses; the layer determines the framing.

## Design decisions

### The harness is a Capability held by the session node, not a graph node

A capability instance is `Readiness`-only in the merged model, held and composed by a node, never
walked (the identity reason: only naturally-unique consuming/live resources are keyed nodes; a
per-session harness instance has no name of its own). The session node is that holder, and it
already holds and composes a `Readiness` (`RequiredCommandsCheck`) exactly this way. So the harness
needs no graph identity, no `key`, no `deps`; it is a plain capability instance the session factory
builds and the session node delegates to. This is the framing the earlier drafts of this SDD reached
for ("a rich consuming resource composing its held instance") and that the merged model makes
native.

### Readiness inherited, not reinvented

The four-way required-commands fork, the one-object target contract, the single-fire guard, and the
level-driven skip are all merged (`sessions/nodes.py`). The harness inherits them by taking the
`RequiredCommandsCheck`'s role, so this SDD does not redesign readiness; it moves the check's config
source from the template's flat `required_commands` to the `shell` harness's `harness_config` and
lets `claude-code` supply its own required executable. Everything the earlier draft proposed to
build for this (an `OperationIdentity` object, a `to_create` context field, a required
`RunContext.identity` invariant and its threading through fourteen sites) is SUPERSEDED and dropped:
the orchestration layer provides the operation scope, the pending-node signal, and the accessor
context, and the harness reads them.

### The harness carries its OWN identity, not the operation scope's

The harness captures the session's identity (name plus vm/workspace/agent-or-admin ancestors) at
construction, from the session's rows, rather than reading those names off `ctx.operation_scope`.
The distinction is the merged model's own: layer-1 identity is a node's kind/name and its
row-carried ancestors, and the session's VM, workspace, and agent are exactly those ancestors, so
they are the harness's OWN identity. `OperationScope` is layer 2, the OPERATION's identity ("why is
this running"), which coincides with the session's identity for a session command but is a different
thing. Reading `ctx.operation_scope.vm` as "my VM" would trust that coincidence; the model
explicitly says a node acts and frames through its own layer-1 identity, not the scope's descriptive
names.

Owning the identity buys a belt-and-suspenders guard: at SESSION level the harness verifies the
scope's names match its own before it acts, so a mis-wired context (the orchestrator handing this
harness a context assembled for a different session) is a loud error rather than commands run
against the wrong VM or agent, which for something that executes on a VM as a user is worth
catching. The cost is one more construction kwarg (`workspace_name`) than the merged
`RequiredCommandsCheck` takes; the factory has it in hand from the same rows. (An earlier draft of
this SDD went the other way, reading `vm_name` from the scope to shrink the constructor; that
conflated the two identities and was reversed, 2026-07-18.) The one thing the harness DOES read from
the scope is the LEVEL, which is genuinely the operation's property, not the session's: it answers
"am I being called as a real session command or as a doctor scan," which the session's own identity
cannot say.

### claude-code's existence check: prefer runtime logic in the pane command

The resume-vs-launch detection can be a `ctx.agent_target()` probe or runtime shell logic folded
into the returned command. The HLA's preference (final call at LLD, against the Claude Code CLI at
implementation time): fold the check into the launch snippet, so check and launch are a single
invocation (the FRD's "easy strengthening" for the check-to-launch race), which also makes
start/restart symmetry trivial. A `ctx.agent_target()` probe remains available if the one-liner gets
awkward. Either way this is op-time work, addressing the tool by the harness's own `session_name`.

### Same-harness blobs merge; the capability owns the merge

A child restating the harness merges per-key (child wins) rather than replacing the blob: a child
overriding just `command` must not silently drop the parent's `required_commands`. The union
semantics of `required_commands` cannot be a generic shallow merge, and inventing a core-side
per-key merge vocabulary for an opaque blob would leak capability knowledge into the core; the
optional `merge_config` hook keeps blob semantics with the blob's owner at zero cost to harnesses
that do not care.

### Hoist at the TOML loader, one internal shape

The legacy flat fields could survive as internal dataclass fields with a compat shim. Rejected on
the best-representations rule: one internal shape (harness/harness_config) means the resolver, the
session node, the migrator, and the equivalence verification all reason about one encoding, and the
flat fields' eventual retirement (resource-manifests Phase 6) deletes loader lines, not consumer
logic.

### claude-code config vocabulary: pinned v1, reserved future

v1 is `permission_mode` / `model` / `extra_args` (FRD R4). Four further concerns are reserved (not
built): user-level MCP inheritance, a question-timeout field, a Claude-subscription OAuth auth mode,
and remote-control enablement. Only ONE is a harness-owned security fix: MCP inheritance, because
authenticating an agent with the operator's own Claude account SILENTLY hands it that account's
user-level MCP servers, so the harness must default to not inheriting. Remote control is a
well-defined feature that is off unless enabled (the harness's default), so exposing the toggle is
the whole job and securing the feature is Anthropic's. The OAuth mode would touch the contract (any
auth interactivity must precede the boundary resolve), so it is deferred until its shape is pinned;
`extra_args` is the escape hatch in the meantime.

## Open questions / for LLD

- **The exact swap points**: where `sessions/nodes.py`'s factories construct the harness (replacing
  `RequiredCommandsCheck`), and where the session orchestrator calls `harness.start` /
  `harness.restart` (replacing `_build_session_command`). Both are named above; the LLD pins the
  diffs and confirms the session node's composition delegation and one-object target wiring carry
  over unchanged.
- **The scope-vs-identity verification**: DECIDED that the harness captures its own session identity
  at construction and reads only the LEVEL off the scope (see the design decision). The LLD pins the
  mechanics: exactly which fields the SESSION-level guard compares (`session` / `vm` / `workspace` /
  agent-or-admin), whether it raises or warns on mismatch (raise recommended, it is an orchestrator
  bug), and whether the guard also lands on the merged `RequiredCommandsCheck` before the swap or
  arrives with the harness.
- **Claude Code detection and flags**: how a resumable session named `<session>` is detected (CLI
  listing vs on-disk session files) and the exact spellings for `permission_mode` / `model` /
  `extra_args`; verify against the latest stable Claude Code CLI at implementation (latest-stable
  rule), and pin the fixture strategy for testing without a real `claude` binary.
- **The required-commands probe relocation**: moving `RequiredCommandsCheck._probe` (the
  `$SHELL -lic` loop, `check=False`, the missing-command error and label parity) into
  `harness/base.py` as the shared helper `shell` and `claude-code` call, and deleting the interim
  class.
- **`merge_config` hook shape**: classmethod vs instance, and the `shell` override that unions
  `required_commands` while other keys child-win.
- **Template-variable substitution on harness output**: today's `_substitute_template_vars` runs on
  operator-authored strings; with harness-returned strings it must not mangle legitimate literal
  braces in generated snippets. Pin the escaping (or restrict substitution to shell's
  operator-authored config values).
- **`ResolvedSessionTemplate.description` default**: "Login shell" is today's field default; decide
  whether it should come from the resolved harness instead (cosmetic).
- **Consumer inventory**: confirm the readers of the removed template fields are the session
  node/orchestrator only (`env/show.py` touches only `env`; the DB stores the template NAME), and
  sweep tests/docs/samples for flat-field mentions.
