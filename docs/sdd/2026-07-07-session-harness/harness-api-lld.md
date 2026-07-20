# Harness API: low-level design

**Status:** Written **Repo:** `agentworks` **Path:** `cli/agentworks/`

Pins the shared harness contract so the `shell` and `claude-code` built-ins and the session-node
swap all reason about one shape. Read `hla.md` "The Harness API" and "Open questions / for LLD"
first; this document turns those open questions into pinned answers, each grounded in the merged
code at HEAD (`file:line` citations throughout). It does not pin the `claude-code` detection
mechanism or its flag spellings: those are `claude-code-lld.md`'s job.

All line citations are against HEAD as read while writing this LLD; a phase that edits a file should
re-confirm the anchor, since earlier phases in the same file shift line numbers.

## 1. `Harness(Capability)` construction

`Harness` extends `capabilities.base.Capability` (`capabilities/base.py:281`), mirroring
`GitCredentialProvider` (`capabilities/git_credential/base.py:104`). The base `__init__`
(`capabilities/base.py:295-317`) binds `(owner_name, config)`, re-runs `validate_config`, and folds
the declared `secret` references into `self._secret_refs`; the subclass extends it with the
session's own identity, exactly as `GitCredentialProvider.__init__`
(`capabilities/git_credential/base.py:126-136`) extends it with `description`.

```python
# capabilities/harness/base.py

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING, ClassVar, Protocol

from agentworks.capabilities.base import Capability

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentworks.capabilities.base import RunContext
    from agentworks.resources.reference import ConfigReference

    # Structural, TYPE_CHECKING-only: the harness satisfies Readiness and
    # reads a target's `.realized` / `.name`, but capabilities/harness/
    # must not import orchestration/ or sessions/ at runtime (layering
    # rule, FRD R1 / HLA package-layout). A Protocol keeps the type
    # without the import edge. The members are READ-ONLY properties: real
    # agent nodes expose name/realized as @property, and a read-write
    # protocol attribute would not be satisfied by them (mypy).
    class _Target(Protocol):
        @property
        def name(self) -> str: ...
        @property
        def realized(self) -> bool: ...


class Harness(Capability):
    owner_kind: ClassVar[str] = "session-template"

    def __init__(
        self,
        owner_name: str,               # the session-template name (config owner)
        config: Mapping[str, object],  # the merged harness_config blob
        *,
        session_name: str,             # the session's own name (addresses the tool)
        vm_name: str,                  # the session's VM ancestor
        workspace_name: str,           # the session's workspace ancestor
        target: _Target | None,        # the agent node it runs as; None in admin mode
        admin: bool,                   # admin mode (uses ctx.admin_target())
        state: dict[str, object],      # the harness's per-session persisted blob (section 12)
    ) -> None:
        super().__init__(owner_name, config)
        self._session_name = session_name
        self._vm_name = vm_name
        self._workspace_name = workspace_name
        self._target = target
        self._admin = admin
        self._state = state            # mutated in-place by ops; the manager persists it
        self._probed = False           # single-fire guard (relocated verbatim)

    @property
    def state(self) -> dict[str, object]:
        """The harness's per-session state blob, mutated by the ops; the
        session manager persists it after the op (section 12)."""
        return self._state

    @abstractmethod
    def start(self, ctx: RunContext) -> str: ...

    @abstractmethod
    def restart(self, ctx: RunContext) -> str: ...

    # Optional one-line op-output note (None default; the session manager
    # renders it after start/restart). claude-code reports resume-vs-new.
    def launch_note(self) -> str | None: ...
```

Pinned points:

- **`owner_kind = "session-template"`** is a `ClassVar[str]`, matching the base annotation
  (`capabilities/base.py:293`) and the `GitCredentialProvider` override
  (`capabilities/git_credential/base.py:124`). The config owner is the session TEMPLATE (the blob
  lives at `session-template.spec.harness_config`), not the session; `_owner_display`
  (`capabilities/base.py:319-321`) renders `session-template/<name>` for error framing.
- **The `target` type is structural, `TYPE_CHECKING`-only.** The interim `RequiredCommandsCheck`
  imports `AgentNode = LiveAgentNode | PendingAgentNode` under `TYPE_CHECKING`
  (`sessions/nodes.py:43-46`), but that pulls the `sessions`/`agents` domain, which the capability
  layer may not import (FRD R1, HLA package layout). The harness reads only `target.realized` (the
  defer/probe fork) and `target.name` (error framing and the SESSION-level guard), so a local
  `Protocol` with those two members is the whole contract. This also keeps `orchestration/node.py`
  unimported: the harness satisfies `Readiness` structurally (it exposes `preflight`/`runup`)
  without importing the protocol.
- **`start`/`restart` are abstract** and return the raw pane command string (empty string = login
  shell only). They replace `_build_session_command` (`sessions/manager.py:872-897`); template-var
  substitution and `exec` wrapping stay OUTSIDE them (section 8). `self.config` is the merged blob;
  `self._session_name` addresses the tool.
- **No new `RunContext` fields.** The harness reads everything else at call time from the merged
  `RunContext` (`capabilities/base.py:160-252`): `ctx.operation_scope` (the level + the guard),
  `ctx.agent_target()` / `ctx.admin_target()` (execution surface), `ctx.secret(name)` (declared
  secrets; unused by both built-ins). `Capability` holds no bound resolver.
- **`_probed`** relocates from `RequiredCommandsCheck.__init__` (`sessions/nodes.py:85`) onto the
  harness base, since the single-fire guard is now the harness's (section 5).

## 2. `validate_config` split (shape at load, completeness at resolve)

`validate_config` stays the base classmethod signature verbatim (`capabilities/base.py:323-352`):
`(cls, owner: str, config: Mapping[str, object]) -> tuple[ConfigReference, ...]`. It is a
`@classmethod` (called at load with no instance, like `GitHubCredentialProvider.validate_config`,
`capabilities/git_credential/github.py:120-125`).

- **At LOAD (per declared blob): VOCABULARY AND SHAPE ONLY.** Unknown fields raise `ConfigError`
  naming the harness and field (FRD R2/R4); each present field is type/vocab-checked. It is invoked
  at three boundaries, mirroring git-credential: the manifest decoder on the true blob with
  `file:line` framing (`manifests/decode.py:236-238`), the TOML loader on the hoisted blob, and the
  template's `referenced_resources()` at finalize (`git_credentials/credential.py:106-119`). A
  restating child may declare a PARTIAL blob (FRD R5), so completeness rules must NOT run here.
- **At RESOLVE (once, on the merged blob): COMPLETENESS.** After the inheritance walk produces the
  merged pair, the resolver calls `validate_config` once more on the merged blob (a value no single
  declaration saw), where required-field / cross-field rules belong. Both built-ins have no such
  rules in v1, so this second call is shape-only too, but the SLOT is pinned so a future harness
  gets it for free.
- **Both built-ins return `()`.** Neither `shell` nor `claude-code` implies a resource reference in
  v1 (FRD R2/R3/R4). The base already re-runs `validate_config` at construct
  (`capabilities/base.py:313-317`), so a shape error dies at construction too, never later in
  preflight.

`shell.validate_config` accepts exactly `command` (str), `restart_command` (str),
`required_commands` (list[str]); all optional. `claude-code.validate_config` accepts
`permission_mode` / `model` / `extra_args` (the field set is pinned in `claude-code-lld.md`). Any
other key is a `ConfigError` naming the harness and the offending key(s), following the
`_validated_scope` unknown-field shape (`capabilities/git_credential/github.py:52-61`).

## 3. `merge_config` hook

Pinned as a **concrete base `@classmethod` with a default**, invoked WITHOUT an instance from
`_merge_pair` (section 9):

```python
# capabilities/harness/base.py, on Harness
@classmethod
def merge_config(
    cls, base: Mapping[str, object], child: Mapping[str, object]
) -> dict[str, object]:
    """Inheritance-time blob merge for a same-harness parent/child pair
    (FRD R5). Default: shallow child-wins. Overridden per capability
    where a key needs richer combination."""
    return {**base, **child}
```

- **Classmethod, not instance, and not getattr-gated.** The HLA pseudocode floated a "getattr-gated,
  absent means shallow" shape (`hla.md:158-162`); that phrasing is superseded because `Harness` is
  the shared ABC and supplies the default outright, so `_merge_pair` can always call
  `harness_for(name).merge_config(...)` unconditionally. Classmethod because the merge runs at
  resolve time from `(harness, harness_config)` values with no instance yet, exactly as
  `validate_config` runs classmethod-side (`sessions/templates.py` merge walk, HLA "Template
  resolver").
- **`shell` overrides to union `required_commands`.** The scalars (`command`, `restart_command`)
  child-win via the shallow default; `required_commands` unions append-dedupe, preserving today's
  semantics. The union uses an `_append_dedupe` helper the harness carries as its OWN per-domain
  copy (the trivial helper at `sessions/templates.py:33-41`); the capability layer may not import
  `sessions/` (layering rule R1), and the codebase already keeps a copy per domain
  (`sessions/templates.py`, `agents/templates.py`), so a copy is the sanctioned shape, not a reuse
  edge:

  ```python
  # capabilities/harness/shell.py, on ShellHarness
  @classmethod
  def merge_config(cls, base, child):
      merged = {**base, **child}          # scalars: child wins
      base_cmds = base.get("required_commands", []) or []
      child_cmds = child.get("required_commands", []) or []
      union = _append_dedupe(list(base_cmds), list(child_cmds))
      if union:
          merged["required_commands"] = union
      return merged
  ```

  This is what keeps a child that overrides only `command` from silently dropping the parent's
  `required_commands` (HLA "Same-harness blobs merge").

## 4. `require_commands` shared helper (probe relocation)

`RequiredCommandsCheck._probe` (`sessions/nodes.py:141-200`) relocates VERBATIM into
`harness/base.py` as a module-level (or base-class staticmethod) helper the built-ins call from
`preflight`/`runup`. Signature:

```python
# capabilities/harness/base.py
def require_commands(
    commands: tuple[str, ...],
    transport: Transport,
    *,
    harness_name: str,    # error subject: "the '<harness>' harness ..."
    template_name: str,   # invoker ref: "(session-template '<name>') requires ..."
    session_name: str,    # StateError entity_name
    target_label: str,    # "VM '<vm>'" (admin/no-target) or "agent '<name>'"
) -> None: ...
```

The body is the `$SHELL -lic 'command -v <cmd>'` loop (`sessions/nodes.py:174-178`) with
`check=False`, the missing-command accumulation, the singular/plural verb, and the `StateError`
(`entity_kind="session"`, `entity_name`, `hint`) verbatim (`sessions/nodes.py:179-200`). Two shape
notes so the relocation stays a pure move:

- **Label parity is passed in, not recomputed.** The interim `_probe` computes `target_label`
  internally from `self._admin` / `self._target` (`sessions/nodes.py:182-187`). Because
  `require_commands` is now shared by two harnesses and takes no `self`, the caller computes the
  label and passes it. The `shell`/`claude-code` `preflight`/`runup` code that calls it derives the
  label the same way (`f"VM '{self._vm_name}'"` when `self._admin or self._target is None`, else
  `f"agent '{self._target.name}'"`), preserving the exact strings.
- **Per the plan, P1 COPIES this body** (`RequiredCommandsCheck._probe` stays) and P3 deletes the
  interim class, making `require_commands` the sole copy (plan "Named interim seams"). The copy must
  be byte-for-byte in the error strings so the parity tests pass against either.

The four-way fork itself (the `_check` scaffolding, `sessions/nodes.py:93-139`) is NOT part of this
helper; it relocates into the harness's `preflight`/`runup` wiring (section 5).

## 5. Readiness fork relocation + single-fire guard

The `RequiredCommandsCheck._check` fork (`sessions/nodes.py:93-139`) relocates into the harness's
readiness UNCHANGED, including the fifth `scope is None` loud branch. "Unchanged" here means the
branch STRUCTURE and semantics; the internal orchestrator-bug message strings are reworded from "the
required-commands check ..." to "the harness ..." (the class IS the harness now, and these are
internal-bug strings, not operator-facing parity surfaces). Only `require_commands`'s probe strings
are byte-for-byte (section 4), because those are what the probe-parity tests assert. `preflight` and
`runup` stay thin dispatchers over one `_readiness(ctx, stage=...)` method (mirroring
`sessions/nodes.py:87-91`); the built-ins fill the probe slot with `require_commands`. The fork, in
order:

1. **`ctx.operation_scope is None`** -> LOUD `StateError` (`sessions/nodes.py:97-106`). Orchestrator
   bug, never a silent skip. Relocated verbatim.
2. **`scope.level is not ScopeLevel.SESSION`** -> SKIP, a legitimate no-op
   (`sessions/nodes.py:107-110`). This is the doctor/system-scan branch (FRD R8: no doctor-specific
   code). `ScopeLevel` imports from `capabilities.base` (`capabilities/base.py:62-72`), already a
   permitted framework import.
3. **SESSION-level identity guard** (NEW, section 6) runs here, after the level check and before the
   `_probed` short-circuit, on every non-SKIP branch.
4. **`self._probed`** -> return (`sessions/nodes.py:111-112`). The single-fire guard.
5. **admin mode** -> `transport = ctx.admin_target()` (`sessions/nodes.py:113-114`).
6. **agent mode, `self._target is None`** -> LOUD `StateError` (`sessions/nodes.py:117-123`). The
   anti-silent-skip branch.
7. **agent mode, `not self._target.realized`** -> return (defer to runup)
   (`sessions/nodes.py:124-125`).
8. **agent mode, realized** -> `transport = ctx.agent_target()` (`sessions/nodes.py:126`).
9. **`transport is None`** -> at `preflight`, return (the command-start ctx did not carry the
   target); at `runup`, LOUD `StateError` (`sessions/nodes.py:127-137`).
10. probe (`require_commands(...)`) then `self._probed = True` (`sessions/nodes.py:138-139`).

**Single-fire guard.** `self._probed` (moved onto the base, section 1) makes the probe fire exactly
once per operation: `preflight` for a target already realized (existing agent, admin, every
restart), `runup` for a target just realized by a `--new-agent` create. `preflight`/`runup` stay
general hooks; a future harness may add target-independent checks to `preflight` or authenticated
checks to `runup` around the shared readiness call.

`shell` and `claude-code` share this fork by inheriting a base helper that runs steps 1-9 and then
calls a subclass hook for the probe payload; the cleanest shape (pinned) is a base
`_run_readiness(ctx, stage)` that performs the fork and, at step 10, calls
`self._probe_target(transport)`, which each harness implements as a `require_commands(...)` call
with its own command tuple (`shell`: the merged `required_commands`; `claude-code`: `("claude",)`).
This keeps the fork in ONE place (no duplication across the two members) while letting each member
name its required commands.

## 6. SESSION-level identity guard

New with the harness (confirmed absent from `RequiredCommandsCheck` at HEAD, `sessions/nodes.py`;
`hla.md:222-224`). It runs on every non-SKIP branch (step 3 above), before the harness acts, and
**RAISES** on mismatch (recommended, ratified: an assembled-for-the-wrong-session context is an
orchestrator bug, and the harness runs commands on a VM as a user, so a silent warn is not safe).

Step 3 places the guard BEFORE the `_probed` single-fire short-circuit (step 4), so it fires on
every non-SKIP readiness call, both preflight and runup. This is intentional: it validates each
context the harness is handed, and it is cheap value-equality, so re-running it costs nothing.

Fields compared, harness identity (left) vs `ctx.operation_scope` (right):

| Harness field          | Scope field       | Source of the scope field                                       |
| ---------------------- | ----------------- | --------------------------------------------------------------- |
| `self._session_name`   | `scope.session`   | `OperationScope(..., session=name)` (`manager.py:1670`, `2307`) |
| `self._vm_name`        | `scope.vm`        | `manager.py:1668`, `2305`                                       |
| `self._workspace_name` | `scope.workspace` | `manager.py:1669`, `2306`                                       |
| `self._admin`          | `scope.admin`     | `manager.py:1672`, `2309`                                       |
| `self._target.name`    | `scope.agent`     | only when NOT admin; `manager.py:1671`, `2308`                  |

The agent-or-admin check is: `scope.admin == self._admin`, and when `not self._admin`,
`self._target is not None and scope.agent == self._target.name`. The `self._target is not None` is
checked EXPLICITLY, not deferred to the fork's step 6 (which has not executed when the guard runs at
step 3): reading `self._target.name` must be null-safe so a mis-wired agent-mode context with a
`None` target raises a clean `StateError` (or falls through to step 6's target-absent `StateError`)
rather than an `AttributeError`. Agent mode always carries a non-`None` target by the factory
invariant (`pending_session_node` / `live_session_node` accept exactly one of agent/admin,
`sessions/nodes.py:385-389`, `418-436`), so the explicit check is belt-and-suspenders, but it is the
guard's own to make, not step 6's. A `SESSION`-level scope is guaranteed by
`OperationScope.__post_init__` to carry non-`None` `vm`/`workspace`/`session` and exactly one of
`agent`/`admin` (`capabilities/base.py:92`, `142-146`), so every compared SCOPE field is present;
the guard adds value-equality on top of that structural guarantee. On any mismatch, raise a
`StateError` (`entity_kind="session"`, `entity_name=self._session_name`) naming the field that
disagreed and both values, e.g. "session '<s>': operation scope names VM '<scope.vm>' but this
harness is wired for VM '<self.\_vm_name>'; the orchestrator handed a context assembled for a
different session."

The SKIP branch (step 2) does NOT run the guard: at SYSTEM level the scope legitimately describes a
broader operation than this session (that is what the skip is for; `hla.md:218-221`).

## 7. Op-start `RunContext` assembly

`_build_session_command` takes no context (`sessions/manager.py:872-878`); `harness.start` /
`harness.restart` do. The call sites assemble an op-start `RunContext` carrying execution targets
and scoped secrets. `RunContext` is constructed fresh per stage (never `dataclasses.replace`,
`capabilities/base.py:211-219`).

**Create (`start`), replacing `_build_session_command` at `manager.py:1932-1934`.** Mirror the runup
ctx at `manager.py:1873-1880` (`config`, `operation_scope=scope`, `admin_target=target`,
`agent_target=agent_target`) and ADD scoped secrets:

```python
ctx = RunContext(
    config=config,
    operation_scope=scope,
    admin_target=target,
    agent_target=agent_target,
    secrets=ScopedSecrets(secret_values, session_node.secret_refs()),
)
command = harness.start(ctx)
```

`secret_values` is already captured at `manager.py:1762` (`resolver.values`). This is the same
scoping the create path already uses for `scoped_ctx` (`manager.py:1764-1769`) and
`credential_tokens` (`manager.py:1814`): declare-and-receive, scoped to the node's own
`secret_refs()` union. The session node's `secret_refs()` returns `()` for both built-ins today
(`sessions/nodes.py:238-239`, `287-288`), so `ScopedSecrets` never delivers anything and `.get`
never fires; the plumbing is present for a future secret-declaring harness (HLA "Node reshape":
`secret_refs()` folds in the harness's declared secrets).

**Restart (`restart`), replacing `_build_session_command` at `manager.py:2483-2488`.** Mirror the
preflight ctx at `manager.py:2373-2381` (`admin_target=admin_target`,
`agent_target=None if is_admin else session_target`) and assemble it AFTER the kill (the destructive
step at `manager.py:2450-2478`), because `claude-code` must decide resume-vs-launch with the old
process already dead (HLA "Restart ordering"):

```python
# after the kill, replacing manager.py:2483-2488
ctx = RunContext(
    config=config,
    operation_scope=scope,
    admin_target=admin_target,
    agent_target=None if is_admin else session_target,
    secrets=ScopedSecrets(graph_secret_values, session_node.secret_refs()),
)
command = harness.restart(ctx)
```

One restart-only wiring detail to flag (section 12): the graph boundary resolve
(`resolver.resolve()`, `manager.py:2382`) does not currently capture `resolver.values` into a
variable on the restart path (unlike create at `manager.py:1762`); the env-chain `secret_values`
built later (`manager.py:2420-2435`, via `resolve_for_command`) is a SEPARATE resolve and is NOT the
graph union. `graph_secret_values` above must be `resolver.values` captured right after
`manager.py:2382`. For the built-ins this is inert (empty `secret_refs()`), so P3 can pass an empty
mapping and defer the capture to the first secret-declaring harness; pinned as: capture
`resolver.values` after the boundary resolve so the shape is correct now, matching create.

The `session_node.runup(...)` call at `manager.py:1873` (readiness) is unchanged by the swap: it
delegates to `harness.runup`, which needs no secrets for the built-ins. The op ctx above is a
SEPARATE, later construction for the `start`/`restart` OP, not the runup readiness.

## 8. Template-variable substitution relocation

Today `_substitute_template_vars` runs INSIDE `_build_session_command`
(`sessions/manager.py:895-896`), over `_KNOWN_TEMPLATE_VARS = {"session_name", "workspace_name"}`
with `_TEMPLATE_VAR_RE = re.compile(r"\{\{(\w+)\}\}")` (`sessions/manager.py:33-34`). It LIFTS OUT
to wrap the harness's returned string:

```python
command = harness.start(ctx)                # or harness.restart(ctx)
command = _substitute_template_vars(
    command, {"session_name": name, "workspace_name": workspace_name}
)
```

The value map is what the create path already passes (`name`, `workspace_name`). The restart path
uses the SAME two keys but sources `workspace_name` from `session.workspace_name` (as
`_build_session_command` does today at `manager.py:2486`), not from the scope, so the lift stays
parity-exact.

**Brace-safety decision (pinned): restrict what the harness's CODE-GENERATED pieces emit, do NOT add
an escaping syntax.** The regex substitutes `{{word}}` (an ASCII-word token in doubled braces) and
RAISES on an unknown token (`sessions/manager.py:600-604`). Substitution runs over the WHOLE
returned string, so it covers not just `shell`'s operator strings but also `claude-code`'s
operator-authored `extra_args`, which are appended verbatim into the returned string (R4). The rule
the harnesses honor, and its consequence for `extra_args`:

- The two template variables (`{{session_name}}`, `{{workspace_name}}`) are an OPERATOR-authored
  convenience. In `shell` they appear in the operator's `command` / `restart_command`; in
  `claude-code` they can appear in an operator's `extra_args` element. Both substitute the known
  vars and RAISE on an unknown `{{word}}`, identically. This is PARITY with today's shell behavior
  (an unknown `{{word}}` in an operator string has always raised), and arguably a feature: an
  operator can write `extra_args: ["--append-system-prompt", "session {{session_name}}"]` and have
  it substituted.
- A harness's own CODE-GENERATED pieces (`claude-code`'s `claude --session-id <uuid> --name <name>`,
  the visible-decision `echo`) MUST NOT emit a literal `{{word}}`. They are built from
  `shlex.quote`d literal values (a uuid, the session name, a fixed message); single braces
  (`${VAR}`, brace expansion, JSON) are untouched by the regex (it requires DOUBLED braces around a
  bare word), so the generated skeleton is a safe pass-through. Only the operator-supplied slices
  (shell's strings, claude-code's `extra_args`) carry `{{...}}` semantics, exactly as intended.
- If a future harness genuinely needs to emit a LITERAL `{{` from its code-generated pieces, the
  pinned answer is to have that harness emit its output already-substituted and the call site skip
  substitution for it, rather than teaching the regex an escape; but no v1 harness needs this.

This keeps substitution mechanical and unchanged (no new escape grammar for operators to learn)
while guaranteeing generated snippets are not mangled. It is covered by the P4 substitution-safety
carry (plan: "`claude-code`'s returned snippet is the first harness output that can carry literal
braces").

## 9. `ResolvedSessionTemplate` reshape + `_merge_pair`

`ResolvedSessionTemplate` (`sessions/templates.py:21-30`) reshapes from the flat fields to the pair:

```python
@dataclass
class ResolvedSessionTemplate:
    name: str
    description: str = "Login shell"
    env: dict[str, EnvEntry] = field(default_factory=dict)
    harness: str = "shell"
    harness_config: dict[str, object] = field(default_factory=dict)
```

`command`, `restart_command`, `required_commands` are removed. Default is `("shell", {})` (an
undeclared template is a plain login shell, FRD R3). `SessionTemplate`
(`sessions/template.py:41-54`) loses the same three fields and gains `harness: str | None` /
`harness_config: dict | None` (`None` = not declared, distinct from declared-empty, HLA "Declaration
layer").

**`_merge_pair`.** The merge walk (`_resolve`/`_merge`/`_merge_template`,
`sessions/templates.py:77-135`) keeps its depth-first, left-to-right order; the flat-field merge
lines for `command`, `restart_command`, and `required_commands` (a NON-contiguous set in
`_merge`/`_merge_template`, interleaved with the `description` and `env` merges, which STAY,
section 10) are replaced by one pair-merge per FRD R5. The implementer removes exactly those three
field merges and leaves the `description`/`env` merges untouched. Because resolution accumulates
into a `ResolvedSessionTemplate`, the accumulator carries the running pair; the merge folds each
parent's resolved pair and then the child's declared pair through the SAME rule:

```python
def _merge_pair(
    acc_name: str | None, acc_config: dict[str, object], child_harness: str | None,
    child_config: dict[str, object] | None,
) -> tuple[str | None, dict[str, object]]:
    if child_harness is None:                 # says nothing about the pair
        return acc_name, acc_config           #   (harness_config without harness cannot load)
    base = acc_config if child_harness == acc_name else {}   # fresh blob on switch
    merged = harness_for(child_harness).merge_config(base, child_config or {})
    return child_harness, merged
# after the whole walk: (None, {}) -> ("shell", {})   # the undeclared default
```

- A child that does not declare `harness` (`child_harness is None`) leaves the accumulated pair
  UNTOUCHED (FRD R5; this is the deliberate multi-parent divergence: a harness-silent later parent
  no longer wipes an earlier parent's command, `frd.md:405-411`).
- A child declaring a DIFFERENT `harness` starts from a fresh (empty) `base`; the parent's blob was
  addressed to the wrong capability and never leaks (FRD R5).
- A child declaring the SAME `harness` merges via that harness's `merge_config` (section 3),
  child-wins per key, `shell` unioning `required_commands`.
- The internal accumulator threads `(name | None, config)` and collapses the post-walk `(None, {})`
  to `("shell", {})`. Since `ResolvedSessionTemplate.harness` is a non-`None` `str`, the resolver
  holds the running pair as locals during the walk and writes the collapsed result onto the
  dataclass at the end (or seeds the accumulator dataclass with `harness="shell"` and treats "still
  shell + empty because nothing was declared" identically, which is the same observable value).

**`merge_config` runs with no instance** (`harness_for(name).merge_config(...)`), consistent with
section 3. **Completeness validation** runs once after the walk: the resolver calls
`harness_for(resolved.harness).validate_config(f"session-template/{resolved.name}", resolved.harness_config)`
on the MERGED blob (section 2), the value no single declaration saw.

`harness_for(name)` is the registry lookup exported from `capabilities/harness/__init__.py` (HLA
package layout), a `HARNESS_REGISTRY[name]` access with typed framing on a miss (a `ConfigError`),
mirroring how the resolver reaches capability classes today. Unknown names are normally caught
earlier by the kind's error miss policy at finalize (FRD R2), so `harness_for` in the resolver only
sees names that already validated as references; its typed miss is defense in depth.

## 10. `ResolvedSessionTemplate.description` default (deferred item, now decided)

**Pinned: keep the literal `"Login shell"` default** (`sessions/templates.py:27`); do NOT source it
from the resolved harness. Rationale: it is cosmetic (a describe/list nicety), sourcing it would
make every harness carry a display-description surface it does not otherwise need, and changing the
shown description for existing `shell`-resolving templates is a gratuitous divergence from today's
output under this SDD's parity rule (FRD R3). `description` remains an independently-declared,
independently- merged field (unaffected by the pair, FRD R5), exactly as today.

## 11. Registry / kind surface (mirror, for completeness)

Not a new "to pin" item, but the constructor and `validate_config` above only make sense atop the
kind/registry wiring, which mirrors git-credential exactly and is detailed in the HLA; restated in
one line each so this LLD is self-contained:

- `harness/kinds.py`: `_HarnessKind` (`category="capability"`, `miss_policy="error"`,
  `builtin_override="reserved"`, `synthesize` raising `NoUnreferencedDefaultError`) +
  `HarnessEntry(name, origin, references)`, self-registering into `KIND_REGISTRY`, mirroring
  `capabilities/git_credential/kinds.py:86-110`.
- `harness/__init__.py`: `HARNESS_REGISTRY` (name -> class), `harness_for(name)`, `publish_to` (one
  `HarnessEntry` per harness with `Origin.built_in(source="agentworks.capabilities.harness")`),
  mirroring `capabilities/git_credential/__init__.py:44-79`.

## 12. Harness-state persistence (the per-session blob)

A harness gets a general-purpose per-session state blob it can read and update, persisted on the
session row. `claude-code` is the first user (it stores its Claude session id,
`claude-code-lld.md`); `shell` uses none of it; the slot is there for any harness. This REVERSES the
SDD's original "Database: unchanged" decision (FRD/HLA), a deliberate change, not incidental.

- **Schema.** The `sessions` table gains a `harness_state` column, a JSON object (`TEXT` holding
  JSON, default `'{}'`), harness-owned and OPAQUE to the core (it never inspects the keys). A
  forward-only migration adds the column and backfills existing rows to `{}` (the SQLite
  table-rebuild discipline is not needed for a pure additive column with a default). `SessionRow`
  gains a `harness_state: dict[str, object]` field.
- **Construction.** The session factory (`_harness_for_template`, `sessions/nodes.py`) reads the
  session row's `harness_state` (or `{}` for a fresh create, where no row exists yet) and passes it
  as the harness constructor's `state=` kwarg (section 1). The SAME dict object flows in.
- **Read/write during the op.** A harness reads `self._state.get(key)` and may mutate it in place
  (`claude-code`: mint and record `session_id` on the first `start`). The op return value is
  unchanged (still the pane command string); state is a side channel.
- **Persistence.** After the manager calls `harness.start(ctx)` / `harness.restart(ctx)`, it reads
  `harness.state` (section 1's property) and writes it to the session row: folded into the row
  INSERT on create (so a freshly minted `session_id` lands with the new row) and an UPDATE on
  restart (usually a no-op, the id already stored). The manager owns the DB write; the harness never
  touches the DB (layering).
- **Restart re-resolution.** Restart re-resolves the template and reconstructs the harness, but now
  loads `harness_state` from the stored row, so a value minted on create survives to restart. This
  is what lets `claude-code` store rather than re-derive its id.

## Contradictions with pinned decisions / FRD / HLA assumptions

Nothing at HEAD contradicts a pinned decision. Two places where this LLD REFINED the HLA's wording;
both have since been reconciled into the HLA (2026-07-19), so they are recorded here as resolved,
not open:

1. **`merge_config` shape.** An earlier HLA pseudocode floated a "getattr-gated, optional" hook.
   Since `Harness` is the shared ABC and supplies a concrete default, this LLD pins a plain base
   classmethod default (section 3) that `_merge_pair` calls unconditionally. The HLA now states
   exactly this ("a BASE classmethod with a shallow default ... no getattr guard").

2. **Restart op-ctx secret source.** An earlier HLA phrasing said the op ctx's scoped secrets mirror
   "the runup context the merged path already builds." On the RESTART path there is no runup ctx and
   the graph boundary resolver's values are not retained (`resolver.resolve()` at `manager.py:2382`
   is unassigned; `resolve_for_command` at `manager.py:2420-2435` is a separate env-chain resolve).
   This LLD pins capturing `resolver.values` after `manager.py:2382` for the op ctx (section 7),
   inert for the built-ins. The HLA now names both shapes (the runup context on create, the
   preflight context plus the captured resolver values on restart).

## Genuinely-open sub-questions (candidates for you / the operator)

- **`claude-code` required-command tuple and detection** are `claude-code-lld.md`'s, not this LLD's;
  this LLD only pins that `claude-code` supplies its own tuple (`("claude",)` expected) to the
  shared fork (section 5). No open question here for the shared contract.
- **Whether P3 captures `resolver.values` on restart or defers it** (section 7 / contradiction 2).
  DECIDED by the lead (2026-07-19): capture `resolver.values` after the boundary resolve now, so the
  restart op-ctx is shape-correct and matches the create path. It is inert for the built-ins (empty
  `secret_refs()`), so there is no behavior risk, and it avoids a future secret-declaring harness
  discovering the gap. P3 does the capture.

No other sub-question remained un-pinnable from the code.

## Done when

Confirmed met. Every HLA "Open questions / for LLD" item (`hla.md:471-502`) is pinned or explicitly
deferred here:

- The exact swap points -> sections 5, 7 (fork relocation, op-ctx assembly at the cited anchors);
  node composition delegation and one-object target wiring carry over unchanged (HLA "How the
  harness plugs in"; the factory already wires the same agent object as dep and `target`,
  `sessions/nodes.py:390-397`, `437-444`).
- The scope-vs-identity verification -> section 6 (fields compared, raise on mismatch, arrives with
  the harness).
- Claude Code detection and flags -> explicitly DEFERRED to `claude-code-lld.md` (out of this LLD's
  scope by the plan's LLD split).
- The required-commands probe relocation -> section 4.
- `merge_config` hook shape -> section 3.
- Template-variable substitution on harness output -> section 8.
- `ResolvedSessionTemplate.description` default -> section 10 (decided: keep "Login shell").
- Consumer inventory -> confirmed the removed flat fields are read only by the session
  node/orchestrator (`_build_session_command` and the `RequiredCommandsCheck` construction);
  `ResolvedSessionTemplate` reshape (section 9) forces every reader to compile against the new
  shape, and P4's field deletion is the green-gate that proves no flat-field reader remains (plan
  P4). The DB stores the template NAME, not the pair (HLA "Database: unchanged").
