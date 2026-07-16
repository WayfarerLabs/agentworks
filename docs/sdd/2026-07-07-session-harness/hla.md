# Session harness capability: high-level architecture

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

## Overview

The harness is a new capability domain built entirely on machinery that already exists: a code
registry plus read-only capability rows (the `secret-backend` shape), an inline reference+blob
consumer field on a declarable kind (the shape capability-consumers.md rule 2 defined for exactly
this case), and runtime dispatch from the owning manager (the shape the secrets resolution loop
established). No resources-framework changes are needed; the new code is the `agentworks/harness/`
package, one kind module, and the consumer changes in the session-template path.

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
                                        | agentworks.harness           |
runtime (sessions.manager)              |   HARNESS_REGISTRY           |
+------------------------------+        |   { shell, claude-code }     |
| resolve_template(registry)   |        +---------------^--------------+
|   -> (harness, harness_config)|                       |
| HarnessContext(run, names,   |  preflight/start/      |
|   admin) --------------------+--- restart ------------+
|   <- pane command string     |
| core substitutes {{vars}},   |   ctx.run executes on the launch
| tmux hosts the pane          |   target AS THE TARGET USER
+------------------------------+
```

The FRD's model change ("a session is a specification to run a specific harness as an agent in a
workspace on a VM") lands architecturally as a narrowing of what the sessions manager knows: it
stops interpreting command strings and starts brokering between the resolved template's
`(harness, harness_config)` pair and the capability behind it. Everything else the manager does --
target preparation, env composition, secret resolution, tmux hosting, liveness -- is untouched.

## Package layout

```text
cli/agentworks/harness/
  __init__.py         # public surface: HARNESS_REGISTRY, harness_for(), publish_to()
  base.py             # Harness protocol, HarnessContext, require_commands, default config merge
  kinds.py            # capability kind strategy + HarnessEntry row (domains own their kinds)
  shell.py            # built-in 'shell' harness (owns command/restart_command/required_commands)
  claude_code.py      # built-in 'claude-code' harness (launch-vs-resume state logic)

cli/agentworks/sessions/launch.py           # extracted launch path: context build, dispatch,
                                            #   template-var substitution, env-scope composition
                                            #   (see the file-size design decision)
```

Mirrors `agentworks/secrets/backends.py` (registry + protocol + publisher) split across a package
because two non-trivial members ship on day one. Pure Python, no Typer dependency (typer-isolation
rule). `harness/kinds.py` follows `secrets/kinds.py`'s `SecretBackendEntry` pattern under the
domains-own-their-kinds placement (resource-manifests Phase 5.8): `category = "capability"`,
`miss_policy = "error"`, `builtin_override = "reserved"`, `auto_declare_names = None`, a frozen
`HarnessEntry(name, description, origin, references)` row type, and a `synthesize` that raises
`NoUnreferencedDefaultError`. Registration is the one-line import added to
`resources/kinds/__init__.py`, the pure index that populates `KIND_REGISTRY` per domain.

`publish_to(registry)` adds one `HarnessEntry` per registered harness with
`Origin.built_in(source="agentworks.harness")`; `bootstrap.build_registry` gains the call in the
built-in publisher block alongside `git_credentials.publish_to` / `secrets.publish_to`.

## The Harness API

The centerpiece of this design (FRD R7). The contract is deliberately small; the power is in the
context object.

```python
# agentworks/harness/base.py (pseudocode-level; exact types at LLD)

@dataclass(frozen=True)
class HarnessContext:
    """The execution environment a harness receives for one launch.

    Carries the model's full identity chain as NAMES; full
    representations are reserved room (see the "Names now" design
    decision)."""

    run: RunCommand         # executes on the launch target AS THE TARGET USER --
                            #   the user the session runs as (the selected agent,
                            #   or the admin user in admin mode); callable any
                            #   number of times, one target exec per call
    vm_name: str
    workspace_name: str
    agent_name: str | None  # None exactly when admin is True (no agent in admin mode)
    session_name: str
    admin: bool             # admin mode (run is bound to the admin user)

    # Deliberately absent, room reserved:
    # - an always-admin channel (`run_admin`) gated by an explicit
    #   permission grant ("Two channels" design decision);
    # - full vm / workspace / agent / session representations ("Names
    #   now, representations reserved" design decision).


class Harness(Protocol):
    """The session-domain capability: how a named session of a tool runs.

    Stateless. ``validate_config`` MUST be cheap and side-effect-free
    (it runs at config load). ``preflight``, ``start``, and ``restart``
    MAY execute code on the launch target via ``ctx.run`` (state probes,
    tool interrogation) but MUST NOT be interactive: no prompts, ever --
    all operator interaction precedes dispatch (the walk-away
    invariant), and that holds for plugin harnesses too.
    """

    name: str
    description: str

    def validate_config(
        self, owner: str, config: Mapping[str, object]
    ) -> tuple[ConfigReference, ...]:
        """The capability config-validation contract shipped by the
        resource-manifests SDD (its Phase 5.7; the
        GitCredentialProvider.validate_config precedent): validate the
        harness_config blob owned by ``owner`` (display context for
        errors, e.g. "session-template/claude") and return the resource
        references it implies -- both built-ins imply none and return
        (). VOCABULARY AND SHAPE only (per-field checks; unknown fields
        are errors, FRD R4); completeness rules (required fields,
        cross-field constraints) belong to the merged blob at resolution
        -- a child's partial blob must validate standalone (FRD R2/R5).
        Raises ConfigError naming the offending field; blob-boundary
        callers prefix the declaration's location."""

    def preflight(self, ctx: HarnessContext, config: Mapping[str, object]) -> None:
        """Validate the target environment before any tmux work: missing
        executables today; files, tool state, or vm / workspace / agent
        state tomorrow. Runs in the REAL session environment -- the
        actual target user, the actual workspace (preflight MAY depend
        on workspace files; ephemerals are provisioned first, rollback-
        protected), the fully composed env. Raises StateError with
        actionable detail; returning means "safe to proceed". The common
        case is one call to base.require_commands(ctx, [...]), which
        packages today's required-commands probe loop and error shape."""

    def start(self, ctx: HarnessContext, config: Mapping[str, object]) -> str:
        """The pane command for a fresh `session create`. Empty string
        means login shell only."""

    def restart(self, ctx: HarnessContext, config: Mapping[str, object]) -> str:
        """The pane command for `session restart` (invoked after the old
        session is killed)."""

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

- **The return value is a pane command string, and that is the whole tmux story.** The core applies
  template-variable substitution and `exec` wrapping to it and hands it to `tmux.create_session`,
  exactly as it does for today's template `command`. The harness never sees tmux (FRD R7's tmux
  invariant): it decides WHAT runs in the pane, and returning a string is the mechanism that makes
  "never HOW" structural. A multi-step launch is a compound shell snippet in the returned string.
- **`ctx.run` is the arbitrary-code surface, bound to the TARGET USER.** "Target user" is the
  established vocabulary (the direct-user-SSH SDD's "operations whose target user is the agent"):
  the user the session runs as -- the selected agent, or the admin user in admin mode. It is the
  same user-bound `RunCommand` the manager already holds for the preflight, so "execute code as the
  target user" costs no new plumbing and inherits the existing logging and error shaping. Harness
  logic itself runs CLI-side; only the commands it issues run on the target. Nothing is uploaded or
  installed.
- **`preflight` is a check, not a list.** The template's `required_commands` field was declarative
  because TOML could be nothing else; a capability can simply CHECK. Today every built-in's
  preflight is one `require_commands(...)` helper call (the manager's probe loop and error shape,
  relocated verbatim into `harness/base.py`), but the hook's shape already covers what comes next:
  files, tool state, and eventually vm / workspace / agent state, without another contract change.
  Full vm / workspace / agent / session representations in the context are additive when a harness
  first needs one (the "Names now" design decision).
- **Preflight runs in the real environment; ephemerals are provisioned first and rolled back on
  failure.** An "equivalent environment" pre-provisioning check was considered and rejected: it
  cannot exist at all for an ephemeral agent (the Linux user is created by `create_agent`; probing
  as admin false-aborts on agent-template user-level tooling), and for an ephemeral workspace it
  would forbid preflight from ever depending on workspace files -- giving up real checks to avoid a
  benign teardown. Instead, ephemerals are provisioned under the existing rollback protection and
  preflight then probes the actual target user, in the actual workspace, with the fully composed
  env; a failure tears down only what this call just created, the same path any later failure (tmux
  included) already exercises. When nothing ephemeral was requested there are no mutations before
  preflight, so it still naturally precedes any change. The invariant that matters is the FRD's
  walk-away rule: all interactivity completes before any state change; everything after is
  non-interactive and ends in success or a clean rollback with a clear error.
- **`start` and `restart` are separate methods** rather than one method with a flag: they are the
  contract's verbs (FRD R3/R7 vocabulary), implementations that differ (shell) read naturally, and
  implementations that coincide (claude-code, where both reduce to resume-or-launch) share a private
  helper. A future restart-specific concern (pre-resume cleanup) has an obvious home.
- **Config travels as an explicit parameter**, not on the context: the context is the execution
  environment (one per launch), the blob is declaration data (validated, merged), and keeping them
  separate mirrors `SecretBackend`'s `(secret, mapping)` parameter shape.
- **Errors**: `validate_config` raises `ConfigError`; `start`/`restart`/probes raise the standard
  typed errors (`StateError`, `ExternalError`) with `entity_kind="session"`, which the CLI already
  renders.

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
- Blob validation runs through the shipped capability config-validation contract at exactly the
  git-credential invocation points (resource-manifests Phase 5.7): the manifest decoder validates
  the TRUE blob with `file:line` framing, the TOML loader validates its assembled (hoisted) blob,
  and unknown harness names skip invocation so the reference miss policy reports them uniformly at
  finalize.
- `SessionTemplate.referenced_resources()` emits one `harness`-kind reference when `harness` is
  declared (usage "the session harness", source `("session-template", <name>)`), plus any
  capability-implied `ConfigReference`s from `validate_config`, emitted as itself per the contract
  (none today for either built-in; the plumbing is uniform so a future harness whose blob names a
  secret Just Works).

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

The resolver validates the MERGED blob through `validate_config` once resolution completes (FRD R7:
the resolved pair is validated at use) -- a merged blob is a new value no single declaration ever
saw, so declared-blob validation alone cannot cover it. This is also where completeness lives:
declared blobs validate vocabulary/shape only (a restating child may be partial), and any
required-field or cross-field rule a harness has is checked here, against the merged whole.

### Sessions manager (`sessions/manager.py`)

The two consumers of the removed fields become harness dispatch:

- `_build_session_command(template, session_name, workspace_name, restart)` becomes a thin
  dispatcher: build `HarnessContext` from values the call sites already hold (the target-user
  `run_command`, the names, the mode flag), call `start` or `restart`, then apply template-variable
  substitution to the returned string exactly as today.
- `_assert_required_commands`'s probe loop, docstring rationale, and error shape relocate verbatim
  to `harness/base.py` as the `require_commands(ctx, commands, ...)` helper; the manager's preflight
  call sites become `harness.preflight(ctx, config)`.

Sequencing note (pinned so the LLD preserves it): on restart, `preflight` runs BEFORE the
destructive kill (as today's required-commands check does), while `restart(ctx, config)` runs AFTER
it -- which is exactly what claude-code needs, since Claude Code's session state is only settled
once the old process is dead. On create, `start` runs after the preflight, before
`tmux.create_session`. Both paths already hold the correctly-bound `run_command` at those points; no
new threading.

### Env composition (unchanged; stated so the story is explicit)

Nothing about env moves, and the invariant is simple: **everything gets the appropriately resolved
env.** Secrets resolve exactly once per command (`resolve_for_command` at the service entry),
`compose_env` merges the vm / workspace / admin / agent / session scopes with the resolved values,
and the composed env reaches the pane through the existing belt-and-suspenders delivery (SSH
`SetEnv` on the connection plus tmux `-e` session-environment flags). The command a harness returns
therefore executes with the fully resolved env exactly as today's template `command` did; a harness
never composes, reads, or forwards env itself, and `spec.env` stays core template vocabulary, never
harness config.

One deliberate upgrade over today: `ctx.run` is bound with the composed env (the same `SetEnv`
delivery the pane's SSH connection uses), so preflight and launch-time probes see the env the pane
will see -- today's login-env-only probe misses PATH-affecting values the session itself gets.
Probes run in the workspace directory, matching the pane's working directory (the workspace exists
by preflight time; ephemerals are provisioned first).

### Registry, bootstrap, inspection

- `harness/kinds.py` self-registers into `KIND_REGISTRY` at import (one index line added to
  `resources/kinds/__init__.py`), which makes `agw resource kinds`, `--kind harness` filters, the
  `resource_refs` completer, and the envelope's capability-kind rejection all work with zero
  additional wiring.
- `bootstrap.build_registry` gains `harness.publish_to(registry)` in the built-in block. Publisher
  order within the block is irrelevant (no cross-kind collisions).
- `agw resource list/describe/kinds` need no code changes; the rows and references render through
  the existing framework surfaces (FRD R8).

### Database: unchanged, deliberately

No schema migration. `SessionRow` stores the template NAME (plus placement and tmux lifecycle
fields); the pane command is not persisted today and the harness pair is not persisted either.
Restart re-resolves the template by the stored name and dispatches fresh, so the harness and its
config always come from current declared config -- exactly the semantics template edits already have
between create and restart. Nothing else in the design wants DB state.

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
  narrative, `cli/README.md` schema/reference, `docs/guides/resources.md` capability story.
- The ADR is drafted as `adr-session-harness.md` in this feature directory and is promoted (and
  numbered) into `docs/adrs/` at the very end of the effort (FRD R10).

## Service layer orchestration

`create_session` is already the single service endpoint this SDD needs -- the ephemeral-session work
gave it that shape: CLI-flag-shaped parameters, consolidation and validation in the service, atomic
ephemeral workspace/agent provisioning with rollback on any failure, and one secret resolve. Harness
dispatch slots INSIDE that endpoint without changing its shape, and the CLI stays dead simple per
cli-conventions (`agw session create` gains no new flags; the service layer is the authority).

The orchestration order, with the harness steps slotted in. The governing rule is the FRD's
walk-away invariant: **all interactivity (secret prompts) completes before any state change**; from
that point on the command is non-interactive and every failure ends in a clean rollback with a clear
error. Preflight consumes the composed env and probes the real environment, so the fixed sequence is
resolve -> compose -> provision (rollback-protected) -> preflight -> tmux. Create path:

1. `build_registry(config)` -- once. Harness rows publish; a template's declared `harness` reference
   validates at finalize, so a typo'd name dies here, before any prompt or mutation.
2. Flag-shape validation, DB checks, operator prompting (unchanged).
3. Template resolution -> `(harness, harness_config)`; merged-blob `validate_config` -- a bad blob
   fails before any prompt or mutation.
4. Ensure the VM is running (unchanged; precedes the resolve today). Interactive by design when a
   stopped VM needs a tailscale auth key -- interactivity that rightly precedes the walk-away point.
   Power-state convergence is idempotent declared-state maintenance, not a rollback-tracked
   mutation; it is called out so "any state change" in the invariant reads precisely.
5. The single secret resolve (`resolve_for_command`; "single" = the one resolve call per command,
   prompt-once structural per the no-caching runtime model) and env composition. When `--new-agent`
   is in play, the ephemeral agent's needed secrets (its git-credential tokens) JOIN this resolve --
   the walk-away gap closure of FRD R7 -- and the values thread through the nested-create seam
   (whose resource-manifests guard test is deliberately amended). The harness adds no resolve calls
   and no prompts. **This is the walk-away point.**
6. Ephemeral workspace/agent provisioning (unchanged; rollback-protected from here down), then
   target preparation. Nothing in this block prompts.
7. `harness.preflight(ctx, config)` in the real environment -- actual target user, actual workspace,
   composed env. A failure rolls back the ephemerals and reports; when nothing ephemeral was
   requested, nothing has mutated yet and the abort is free.
8. `harness.start(ctx, config)` -> pane command; template-var substitution; `tmux.create_session`
   with the composed env.

`restart_session` runs the same sequence against the existing session (template re-resolved by the
stored name): resolve -> compose -> preflight -> kill -> `harness.restart(ctx, config)` (after the
kill, the claude-code sequencing requirement). A bad blob, a missing binary, or an unresolvable
secret all abort with the old session still running. This REORDERS today's restart, which preflights
before resolving: composed-env preflight needs the resolved values first, and one uniform sequence
beats preserving the old "check before prompting" micro-optimization (the cost is that an operator
may answer a secret prompt and then hit a missing-binary abort; the old session survives either
way). Past the kill, the failure contract changes shape (FRD R7): a `restart` dispatch or tmux
failure cannot restore the old session, so the pinned end state is session row intact, old tmux
gone, `agw session restart` cleanly retryable, and an error naming the failed step -- no
resurrection attempt.

## Validation responsibilities

| Layer                       | Owns                                                                                   |
| --------------------------- | -------------------------------------------------------------------------------------- |
| TOML loader                 | section/key shapes, flat-field hoist, flat+non-shell and flat+blob conflict errors     |
| Manifest decoder            | flat-field rejection (clean YAML spec); everything else delegates to the shared loader |
| Harness (`validate_config`) | blob field vocabulary, per declared blob at load and per merged blob at resolve        |
| Harness (`preflight`)       | target-environment checks in the real session environment, pre-tmux (today: commands)  |
| Registry finalize           | unknown `harness` name via the kind's error miss policy (declared references only)     |
| Sessions manager            | orchestration order, dispatch, template-var substitution                               |

All errors are the standard typed `AgentworksError` subclasses; the layer determines the framing.

## Design decisions

### The context carries a RunCommand, not a new execution abstraction

"Execute arbitrary code as the target user" could have grown a script-upload mechanism or a
harness-side agent. It doesn't need to: the manager already holds a user-bound `RunCommand` for the
preflight at every dispatch point, and handing it to the harness gives multi-command probe ability
with the existing logging, quoting, and error conventions. The cost model is honest too -- each
`ctx.run` is one SSH exec, so a harness author sees exactly what a probe costs and can prefer the
in-pane runtime check (below) when one round-trip matters.

### Two channels, one granted by default (room reserved, not built)

The context's execution surface is shaped for a future permission model: `run` (the target user) is
what every harness gets by default, and an always-admin channel (`run_admin`, bound to the VM admin
regardless of mode) can be added later behind an explicit grant -- the natural trust knob for
third-party harnesses when the plugin SDD's distribution tiers arrive. Nothing beyond the shape is
built here: the built-ins need only the target-user channel, so no grant vocabulary, no plumbing.
The extension is cheap when it comes, because the manager already holds both bindings at every
dispatch point (`_prepare_vm` returns the admin-side channel; the target-user channel follows the
mode) -- adding the field is wiring plus policy, not new transport work.

A further rung, recorded while it is fresh (2026-07-08 discussion; recorded, not designed): the
grant ladder need not stop at channel selection. A lower tier could shape the CONTRACT itself -- a
payload-only harness returns declarative payloads (the pane command; a preflight script the core
executes on the target and interprets) and holds no live channel at all. That tier is qualitatively
different for third-party code, because it is the one rung that is actually ENFORCEABLE: a
zero-callback, data-only contract is trivially hostable out of process (or need not be code at all),
and everything such a harness contributes executes inside the VM's existing isolation as the target
user -- the same boundary Agentworks already trusts for agents themselves. The built-ins need none
of this; it is the natural bottom tier for untrusted plugin harnesses if the plugin SDD ever wants
confinement stronger than distribution trust.

### Names now, full representations reserved (and why that is not a security call)

The context carries the model's whole identity chain -- vm, workspace, agent (None in admin mode),
session -- as names, which is what a command-lifecycle harness can actually use (tool addressing,
probe context, error labels). Full representations (the row dataclasses or projections of them) are
deliberately absent until a harness has a concrete need: handing out row types would make their
shape part of the capability contract -- and, once plugins arrive, a compatibility surface -- for
zero current payoff. When the need lands (artifact placement is the likely trigger), the addition is
a new context field, purely additive; whether it exposes the row dataclasses or stable read-only
projections is decided then, with plugin API stability arguing for projections.

Withholding data is NOT a security mechanism, and this design does not pretend it is. A harness is
in-process Python: it can import the DB, the config loader, and the transports regardless of what
the context hands it, and no in-process sandbox exists to preserve. The trust boundary for
third-party harnesses is plugin installation and enablement (distribution trust, the plugin SDD's
story), not runtime confinement. What the context's minimalism -- and the reserved `run_admin` grant
above -- actually buys is misuse-resistance and auditability for cooperating code: a harness that
only uses its handed surface is easy to review, and a grant is an explicit, visible declaration.
Valuable properties; neither confines malice, and the docs say so rather than imply a boundary that
is not there.

### claude-code's existence check: prefer runtime logic in the pane command

FRD R4 leaves the mechanism open between a `ctx.run` probe and runtime shell logic in the returned
command. The HLA's preference (final call at LLD, against the Claude Code CLI surface at
implementation time): fold the check into the launch snippet, so check and launch are a single
invocation -- this is the FRD's own "easy strengthening" for the check-to-launch race, and it makes
start/restart symmetry trivial (both return the same resume-or-launch snippet). `ctx.run` remains
the right tool if the detection logic proves too awkward for a readable one-liner; per the FRD's
robustness posture, neither variant aims to be race-proof.

### Same-harness blobs merge; the capability owns the merge

A child restating the harness merges per-key (child wins) rather than replacing the blob, because
that is what today's field semantics do once the fields become blob keys -- a child overriding just
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

### File-size splits: opportunistic, scoped to the region we touch

`sessions/manager.py` (2832 lines) and `config.py` (1409 post-5.8, down from 1809) both exceed the
code-style ceiling (goal 500, never past 1000 without necessity). This SDD splits what it touches
and no more. The harness work already moves the preflight probe loop out (`require_commands` in
`harness/base.py`) and deletes `_build_session_command`; riding the same commits, the launch path --
harness context construction and dispatch, template-variable substitution, and the session env-scope
composition helpers -- extracts to a new focused `sessions/launch.py` (goal <= 500 lines) as a
behavior-preserving refactor, with `create_session` / `restart_session` staying in `manager.py` as
the service endpoints. That does NOT bring `manager.py` under the ceiling; the full split
(create/restart/lifecycle/attach families) is a dedicated refactor effort, deliberately out of scope
-- mixing it in would swamp the harness diff. `config.py` gains only the ~30-line session-template
loader change: its oversize is dominated by the TOML resource loaders that the resource-manifests
Phase 6 plan already schedules for deletion, so extracting them now would be churn against a planned
removal.

### Kind module now, framework untouched

The capability kind reuses `category` / `builtin_override` / miss-policy machinery as-is; nothing
about the harness required a framework hook, which is the resource-manifests SDD's model paying off:
adding a capability domain is a package, a kind module, and a publisher line.

## Open questions / for LLD

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
- **Consumer inventory**: LLD confirms the only readers of the removed template fields are the two
  manager call sites (`env/show.py` touches only `env`; the DB stores the template NAME, so restart
  re-resolves -- both verified during HLA drafting) and sweeps tests/docs for field mentions.
- **At-use validation home**: whether the merged-blob `validate_config` call lives in
  `resolve_template` or at the manager dispatch points (it must run exactly once per resolution
  either way).
- **`require_commands` helper signature**: the exact shape the relocated probe loop exposes
  (target-label wording, aggregation of missing commands, `check=False` probe semantics) -- a
  mechanical extraction, pinned at LLD.
- **Env binding on probes**: how the composed env binds onto `ctx.run` (`SetEnv` on the command
  channel, bound at context construction) and the probe working-directory mechanics (workspace dir,
  matching the pane) -- pinned at LLD.
- **Not an LLD question, recorded as deferred**: the `run_admin` grant vocabulary and enforcement
  (who grants, where it is declared) belongs to the plugin SDD's trust story; this SDD only keeps
  the context shape extensible for it.
