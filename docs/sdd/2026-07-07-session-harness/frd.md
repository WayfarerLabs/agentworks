# Session harness capability: functional requirements

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

## Background

The resource-manifests SDD (`docs/sdd/2026-07-01-resource-manifests`, PR #156) landed the
config/resource/capability model this SDD builds on: resources are `(kind, name)` registry rows;
kinds split into declarable kinds (data) and capability kinds (read-only rows backed by a per-domain
code registry); resources reference capabilities directly, many-to-one, carrying capability-owned
config at the reference site (ADR 0016, the 2026-07-07 capability collapse). Its companion doc
`capability-consumers.md` (row 5) sketched the first NEW capability to be built on that model: the
**harness**, selected inline on the session template.

Since that SDD, the capability model has grown a full runtime contract, documented in
`cli/agentworks/capabilities/README.md` and proven on two consuming-side capabilities (`vm-platform`
via the vm-sites work, `git-credential-provider` via the fine-grained-PAT work; PRs #169 and #167).
A capability is now an instance-scoped class extending `capabilities.base.Capability`, living in the
`capabilities/` subtree, and it moves through a five-stage lifecycle: `validate_config` (pure,
declares references) -> construct (binds `(name, config, resolver)`) -> `preflight` (pre-resolve,
dependency-blind, read-only readiness) -> `runup` (post-resolve, authenticated, read-only readiness)
-> ops (the mutation phase). A shared `RunContext` carries the resolved runtime world (global
config, the `admin_target` / `agent_target` transports, resolved `secrets`) to the stages that run
against it, and the boundary that splits the two readiness stages is the single secret-resolve pass.
**This SDD builds the harness as a capability of that shape**, so the harness inherits the model's
lifecycle, its secret handling, and its `doctor` integration rather than inventing its own. The
carried FRD/HLA below predate the lifecycle and are being realigned to it; where an earlier draft
said the harness "preflights the real environment," that post-provision check is the model's `runup`
stage (see R7).

Today a session template expresses "what runs in this session" as opaque strings: `command`,
`restart_command`, and `required_commands` are top-level template fields, and the core knows nothing
about the tool behind them. The fields were always harness-owned; the core just didn't have the
word. Consequences:

1. **Tool knowledge is stringly-typed.** The Claude Code launch/resume conventions live in every
   operator's template (`claude --name {{session_name}}` / `claude --resume {{session_name}}`)
   rather than in code that owns them. A convention change is an every-operator edit.
2. **No home for tool-specific behavior.** Anything a tool needs beyond a command string (a
   permission mode, a model selection) has nowhere to live except more command-string interpolation.
3. **Start/restart cannot adapt to tool state.** `command` and `restart_command` are fixed strings,
   so a template cannot express "resume the named Claude session if one exists, else launch fresh".
   Today a start blindly launches fresh even when a resumable session of that name exists, and
   `agw session restart` blindly runs `claude --resume`, which breaks when Claude Code never saved
   the session (it discards sessions that did no work). Only code that knows the tool can make that
   call (R4).
4. **The plugin system needs the seam.** The draft plugin-system SDD
   (`docs/sdd/2026-07-06-plugin-system`, a draft on its own branch `feat/plugin-system-sdd`, not in
   this tree) wants Claude Code and Codex to ship as plugins that register a harness. That requires
   the harness capability to exist in the core first, in the same layer as `secret-backend` and
   `git-credential-provider`. This SDD builds the capability and its built-in members;
   plugin-registered harnesses remain the plugin SDD's story.

### The model change

This is a fundamental change to the Agentworks model, not an internal refactor. Today the model
reads "a session runs an operator-supplied command as an agent in a workspace on a VM"; after this
SDD it reads:

> **A session is a specification to run a specific harness as an agent in a workspace on a VM.**

The session remains the workload specification: naming, lifecycle, tmux persistence, env
composition, and placement (vm / workspace / agent) stay exactly where they are. It delegates the
mechanics of running the underlying process to the harness, which can be smarter than an opaque
command string because it knows the specific tooling it runs. The harness joins the model's core
vocabulary alongside VM, workspace, agent, and session, and the top-level documentation that teaches
the model is rewritten to teach it this way (R10).

The scope of the harness CONTRACT in this SDD is deliberately narrow (maintainer ruling,
2026-07-07): **command lifecycle only**, session start, session restart, and the target-environment
readiness check (today: required commands), the responsibilities the three template fields express
today. In the capability model's stage vocabulary that is: `start` and `restart` as the harness's
ops (each returns the pane command for its case), and the required-commands check as the harness's
`preflight` when the target already exists (with a `runup` fallback for the ephemeral-create case;
see R7). Narrow does not mean string-valued: within that lifecycle a harness may execute arbitrary
code on the launch target as the target user (R7), which is what lets it be smarter than the strings
it replaces (R4). Liveness probing of the running session stays core (tmux boot-id/PID machinery,
tool-agnostic), and asset placement (skills, rules, allow/deny lists) is deferred with the plugin
SDD, which owns the operator-declared asset model it requires. The narrow contract does not narrow
the model change: the delegation seam is the point, and the contract grows behind it without the
model changing again.

A concrete future direction makes that point. We expect Agentworks to eventually manage artifacts
such as skills, hooks, and MCP server configurations, and the division of labor is already implied
by this model: the core handles these objects in the general sense (declaration, storage, scoping),
and the harness translates them for its specific tooling, its directory conventions, file formats,
and registration mechanics. None of that is in this effort; it is called out so the harness is read
as the tool-knowledge seam it is, not as plumbing built just for start/restart semantics. It also
carries a security dimension the harness is the natural place to own: a tool like Claude Code can
inherit user-level configuration from the operator's own account (for example, user-scoped MCP
servers attached to a `claude.ai` login), so a launched agent could silently gain high-power tools
the operator never granted it for THIS agent. When Agentworks manages that surface, the harness is
where the policy lives, and the safe default is non-inheritance (see R4's future-direction notes).

### Scope

In scope: the `harness` capability kind and code registry (in the `capabilities/` subtree, extending
the `Capability` base); the built-in `shell` and `claude-code` harnesses; the
`session-template.spec.harness` / `spec.harness_config` consumer surface (YAML and TOML); the
inheritance semantics of the pair; the runtime delegation in the sessions manager, including
constructing and holding the harness instance and composing its lifecycle stages (the session is the
model's first RICH consuming resource); the required `RunContext.identity` invariant
(`OperationIdentity`) and its threading through every existing capability composition root (R7); the
migration-tool and sample updates the surface change requires; the top-level documentation rewrite
that promotes the new model (R10).

Already handled upstream, so NOT in scope (was in the pre-realignment draft): the single up-front
secret resolve for a `session create --new-agent` nested ephemeral agent. Main's `create_session`
already folds the ephemeral agent's git-token secrets into one boundary resolve and threads the
resolved values through `create_agent` (which skips its own resolve when tokens are supplied), so
the walk-away invariant is already exact for nested creates and this SDD adds nothing there.

Out of scope: plugin-registered harnesses, asset placement, session liveness probing, agent-level
Claude fields (`claude_marketplaces` / `claude_plugins` on agent/admin templates are provision-time
surface, untouched here), and any change to the other capability domains.

## Terminology

- **Harness**: the session-level capability, code that knows how a named session of a particular
  tool is started and restarted, and what executables that requires. A capability kind per ADR 0016:
  read-only registry rows, error miss policy, implementation in a per-domain code registry
  (`HARNESS_REGISTRY`). A consuming-side capability like `git-credential-provider`: its
  implementations extend `capabilities.base.Capability` and move through the model's lifecycle
  (`validate_config` -> construct -> `preflight` -> `runup` -> ops). Named by
  `session-template.spec.harness`. The kind takes the domain's natural noun with no `-provider`
  suffix (ADR 0016's naming rule: a disambiguating suffix only on collision, and nothing else here
  is called a harness).
- **The lifecycle stages, for the harness**: `validate_config` validates the `harness_config` blob's
  shape (both built-ins return no references); construct binds `(name, merged_config, resolver)`;
  `preflight` is the base's pre-resolve, dependency-blind readiness (near-empty for the built-ins,
  which declare no secrets); `runup` is the post-resolve, real-environment required-commands check
  (R3/R7); the ops are `start` and `restart`, each returning the pane command for its case. A shared
  `RunContext` (`capabilities/base.py`) carries the resolved runtime world (global config, the
  `admin_target` / `agent_target` transports, resolved `secrets`) to `runup` and the ops.
- **Harness config**: the capability-owned blob at the reference site
  (`session-template.spec.harness_config`), validated by the selected harness. The inline form of
  reference+blob (capability-consumers.md rule 2): the template is the only consumer, so the FIELD
  is the selector and no dedicated instance kind exists.
- **`shell` harness**: the built-in default reproducing today's behavior verbatim. Its config
  vocabulary IS the three legacy fields: `command`, `restart_command`, `required_commands`.
- **`claude-code` harness**: the built-in harness owning the Claude Code session conventions, launch
  vs resume (chosen by inspecting tool state, R4), required executable, so templates stop spelling
  them as strings.
- **Session template, registry, capability, origin**: as defined by the resource-registry and
  resource-manifests SDDs.

## Requirements

### R1: The `harness` capability kind

- New capability kind `harness`: `category = "capability"`, error miss policy, not
  manifest-declarable (a `kind: harness` document gets the standard capability-kind envelope error),
  `builtin_override = "reserved"`. Same shape as `git-credential-provider`, the kind strategy
  (`capabilities/harness/kinds.py`) mirrors `_GitCredentialProviderKind` and self-registers into
  `KIND_REGISTRY` via the `resources/kinds/__init__.py` index line.
- Implementations live in the `capabilities/harness/` package (the capability subtree, alongside
  `capabilities/git_credential/` and `capabilities/vm_platform/`), extending
  `capabilities.base.Capability`, and are listed in `HARNESS_REGISTRY` (name -> class). A
  `publish_to(registry)` adds one read-only row per registered harness with
  `Origin.built_in(source="agentworks.capabilities.harness")`, mirroring
  `git_credential.publish_to`, so template references validate through the framework's uniform miss
  policy and harnesses appear in `agw resource list` / `agw resource kinds` /
  `agw resource describe harness/<name>` like every other resource.
- Built-in members: `shell` (R3) and `claude-code` (R4). Plugin registration is the plugin SDD's.
- The capability layering rule holds: `capabilities/harness/` depends only on the framework and
  never imports the `sessions/` domain (the consuming resource depends on the capability, not the
  reverse).

### R2: Template surface, `harness` and `harness_config`

The session template selects a harness inline (capability-consumers.md rule 2):

```yaml
apiVersion: agentworks/v1
kind: session-template
metadata:
  name: claude
  description: Claude Code interactive session
spec:
  inherits: [default]
  harness: claude-code
  harness_config:
    permission_mode: acceptEdits
  env:
    CLAUDE_LOG_LEVEL: info
```

- `spec.harness` (optional string): the capability name. Omitted entirely (and not inherited)
  resolves to `shell`, preserving today's behavior for templates that declare neither field.
- `spec.harness_config` (optional mapping): the blob the selected harness owns and validates.
  Declaring `harness_config` without `harness` on the same template is a load error (a blob with no
  owner); the default does not silently adopt a blob.
- A declared `spec.harness` emits a `harness`-kind resource reference from the template (usage: "the
  session harness"), so a typo'd name is a finalize-time `ConfigError` naming the template, and the
  harness row's `Referenced by:` lists its templates. Templates that do not declare the field emit
  no edge; their effective harness is an inherited or defaulted value that is validated where it was
  declared (or is the always-valid built-in default).
- The declared blob is validated by the selected harness at load (decode/parse time), with errors
  carrying the declaration's location (the manifest document's `file:line`; the TOML section's
  location for TOML declarations). Declared-blob validation is VOCABULARY AND SHAPE ONLY (per-field
  checks); completeness rules, required fields, cross-field constraints, apply to the MERGED blob at
  use (R5, R7), because a child restating the harness may legitimately declare a partial blob.
  Validation is capability-owned: the core never learns any harness's field vocabulary. It uses the
  capability config-validation contract the resource-manifests SDD shipped (its Phase 5.7, the
  `GitCredentialProvider.validate_config` precedent): `validate_config(owner, config)` returns the
  resource references the blob implies (none, for both built-in harnesses), and the session template
  emits them as itself at finalize, alongside the selector edge. A future harness whose blob names a
  secret gets auto-declaration, reachability, and doctor coverage with zero new machinery.

**The YAML spec is clean** (maintainer ruling, 2026-07-07): `command`, `restart_command`, and
`required_commands` are NOT session-template spec fields in manifests. They are `shell`'s config
vocabulary and live only under `harness_config`. A manifest declaring them top-level gets a load
error pointing at `harness: shell` + `harness_config`. This tightens the manifest surface shipped by
PR #156 and is `!`-flagged accordingly (the surface is unreleased at this writing; the flag is
defensive).

```yaml
spec:
  harness: shell
  harness_config:
    command: htop
    required_commands: [htop]
```

### R3: The `shell` harness (built-in default)

- Config vocabulary: `command` (string), `restart_command` (string), `required_commands` (list of
  strings), exactly today's fields, exactly today's semantics. All optional; an empty config is a
  plain login shell, exactly like today's field-less `default` template.
- Behavior: the `start` op returns `command` (empty means login shell only); the `restart` op
  returns `restart_command` when set, else `command`; the `preflight` stage probes
  `required_commands` on the target when it exists at command entry (with a `runup` fallback for the
  ephemeral-create case, R7). Template-variable substitution (`{{session_name}}`,
  `{{workspace_name}}`) stays core and applies to the returned command string, as today.
- Every existing template resolves to `shell` and behaves identically; the golden rule for this SDD
  is behavior parity for any config that loads today, with exactly one documented divergence in
  multi-parent inheritance (R5's divergence note).

### R4: The `claude-code` harness (built-in)

- Owns the Claude Code session conventions the sample template currently spells as fixed strings
  (launch as `<session>`, resume `<session>`, required executable `claude`). Templates select it
  with one line instead of restating the strings.
- **Start and restart are state-aware, and symmetric about it.** Both determine whether Claude Code
  has a resumable session under this session's name and act accordingly: resume it when it exists,
  launch fresh when it does not. This fixes both failure modes of the fixed-string encoding
  (Background item 3):
  - a START when a resumable session of that name already exists resumes it instead of launching a
    fresh session alongside the old one's history;
  - a RESTART after Claude Code discarded the session (it does not save sessions that did no work,
    so killing an empty session leaves nothing to resume) launches fresh instead of failing on a
    blind `--resume`.
- HOW the existence check runs (a probe as the session user via `ctx.agent_target` before the pane
  command is built, runtime shell logic in the pane command, or some combination) is the HLA's call,
  on the execution surface R7 requires. Note the timing: this detection is an op-time concern (it
  runs when `start` / `restart` build the pane command, post-provision), not a preflight concern.
- Config vocabulary (owned by the harness, snake_case per project convention; the FIELD SET is
  pinned at LLD, with all flag spellings and the detection mechanism verified against the latest
  stable Claude Code CLI at implementation):
  - `permission_mode` (optional string): forwarded as the corresponding CLI flag.
  - `model` (optional string): forwarded as the corresponding CLI flag.
  - `extra_args` (optional list of strings): appended verbatim to the launch/resume invocation, the
    escape hatch that keeps the harness from needing a field per flag.
- Unknown config fields are validation errors naming the harness and the field.
- The chosen path is visible to the operator: the launch surfaces whether it resumed the existing
  Claude session or launched fresh (mechanism at LLD, an output line or the pane's first visible
  output). The decision must never be silent (review finding, 2026-07-08; operator-control spirit).
- `claude_marketplaces` / `claude_plugins` stay on the agent/admin templates: they are
  provision-time (VM/agent setup) concerns, not session-start concerns. The plugin SDD decides
  whether they move when Claude Code becomes a plugin.
- **Reserved future directions (NOT in v1; recorded because they show where the harness config
  vocabulary and contract grow).** The pinned field set above is the v1 surface; each item below is
  a future harness_config field or contract extension the design leaves room for, not a commitment:
  - **User-level MCP availability, and its default.** Claude Code can inherit user-scoped MCP
    servers from the operator's own account/login. Attaching a high-power MCP tool to a personal
    `claude.ai` account could then hand every launched agent that tool with no per-agent grant and
    no operator awareness, a real privilege-escalation footgun. The harness is the control point for
    what MCP surface a launched session inherits (for example, a config flag to disable user-level
    MCP inheritance, or to pin an explicit MCP config). When this ships, the SAFE DEFAULT is
    non-inheritance: an agent gets only the MCP surface Agentworks granted it, and inheriting the
    operator's personal servers is an explicit, visible opt-in. Recorded now so v1 does not
    accidentally build in silent inheritance; connects to the artifact-management future (model
    change, above).
  - **Question-timeout control.** Claude Code's interactive question-timeout behavior (how long it
    waits on a prompt before it proceeds or fails) is a candidate `harness_config` field, so an
    unattended/walk-away session can pin the timeout policy the operator wants rather than the CLI
    default.
  - **Claude-subscription (OAuth) authentication.** Authenticating a launched Claude Code session
    via a Claude subscription OAuth flow, as an alternative to an API key, is a future auth mode the
    harness would own (a first interactive step at launch, or a provision-time credential the
    harness consumes). It interacts with the walk-away invariant (any interactivity must precede the
    walk-away point) and with the secret model, so it is deferred until its shape is pinned.

### R5: Inheritance, the pair travels together

`harness` and `harness_config` inherit as a unit (capability-consumers.md rule 2):

- A child that does not declare `harness` inherits its parents' effective pair unchanged.
- A child that declares `harness` DIFFERENT from the inherited one starts from a fresh (empty)
  config; the parent's blob is addressed to the wrong capability and never leaks across.
- A child that declares the SAME `harness` as the inherited one (restating it, required, since
  `harness_config` may not appear without `harness`) merges its blob into the inherited one,
  child-wins per key. How two values for one key combine is capability-owned: the default is
  child-replaces, and `shell` overrides it for `required_commands`, which keeps today's
  append-dedupe union semantics verbatim. (Parity requires this: today `required_commands` unions
  across template inheritance while the scalar fields child-win.)
- `env`, `inherits`, and `metadata.description` are unaffected; they merge as today.
- **Deliberate divergence from today's multi-parent semantics** (maintainer ruling, 2026-07-08,
  amending R3's parity rule; surfaced by review): today a later parent's RESOLVED scalars overwrite
  unconditionally, so `inherits: [has-command, env-only]` wipes the first parent's command to empty
  because the second parent's lineage never declared one. Under the pair rule, a harness-silent
  parent leaves the accumulated pair untouched, so the command survives. The old behavior was a
  footgun, not a contract; the divergence affects only multi-parent lineages where a later parent's
  lineage declares none of the command fields, and it is pinned by test.

### R6: TOML surface (dual-path)

TOML session-template sections remain fully supported (deprecated, per the dual-path model), and the
harness surface is expressible there so TOML-declared templates are not locked out of it:

- `[session_templates.<name>]` gains optional `harness` (string) and `harness_config` (table) keys,
  loaded with the same semantics as the manifest spec.
- The legacy flat fields (`command`, `restart_command`, `required_commands`) keep loading verbatim
  and are hoisted by the loader into `harness = "shell"` + the equivalent `harness_config` (the
  internal representation follows the YAML shape, per the best-representations rule; TOML is the
  lone divergent domain, mapped at its loader).
- Flat fields combined with `harness` naming anything other than `shell` is a load error (the fields
  belong to `shell`; they cannot configure another harness). Flat fields combined with an explicit
  `harness_config` table is also a load error (one spelling per declaration; mixing the two would
  need a merge rule inside a single declaration, which is complexity with no operator payoff).
- One R5 interaction stated explicitly because the operator never typed it: a legacy flat-field
  child template under a `harness: claude-code` parent hoists to `harness = "shell"` and switches
  the lineage back to shell with a fresh blob, correct per the switch rule, and called out in the
  docs (R10) so it reads as designed behavior rather than a surprise.
- No new deprecation warning is added for the flat fields themselves: the section already carries
  the TOML-resource deprecation warning. Both TOML spellings are documented, YAML-first (maintainer
  ruling, 2026-07-08): `cli/README.md` documents the nested `harness` / `harness_config` keys
  alongside the flat fields, pointing at YAML manifests as the primary surface, and the flat form
  remains the documented default TOML shape until the TOML resource path retires (Phase 6 of the
  resource-manifests plan).

### R7: Runtime delegation and the harness execution surface

What a harness is FOR: extracting maximum value from the specific tool it wraps. A harness exists
because it knows its tool deeply, so it can resume the right named session, forward the flags that
matter, check that the tool's executables are present, and generally do the smart thing an opaque
command string cannot. That tool knowledge is the whole payoff, and the execution surface below
exists to serve it.

What a harness is NOT responsible for: the operator's walk-away experience and the system's
consistency guarantees. Those are Agentworks-wide properties delivered by the ISOLATION machinery
(secret resolution, ephemeral agents and workspaces, git-token handling, rollback), not by the
harness. The harness's entire obligation to that machinery is a negative one: stay non-interactive
so it never reopens a prompt after the operator has walked away. The requirements below describe the
execution surface and that one obligation; they are careful not to recast the harness as the thing
that provides walk-away.

- The sessions manager stops reading command strings off the resolved template and delegates to a
  resolved harness INSTANCE instead. It constructs the harness
  (`harness_for(name)(owner_name, merged_config, resolver)`), holds it, and drives its lifecycle:
  `preflight` (pre-resolve), then, after the single resolve pass and ephemeral provisioning, `runup`
  (the target-environment check, today the required-commands probe; shaped to grow into file,
  tool-state, and vm / workspace / agent state checks without a contract change), then the `start` /
  `restart` op for the pane command. The session is the model's first RICH consuming resource (it
  has its own lifecycle, panes, env, tmux, AND composes the harness instance's stages); the
  composition is done imperatively in the manager root, consistent with how `create_vm` and agent
  init drive their capabilities today.
- **A harness is not limited to returning a static string.** The contract gives it the ability to
  execute arbitrary code on the launch target as the TARGET USER (the user the session runs as: the
  selected agent, or the admin user in admin mode), including multiple commands (state probes, tool
  interrogation) in the course of deciding what the session runs. The `shell` harness uses none of
  this; `claude-code` needs it (R4); future harnesses get it for free. This surface is the
  `RunContext`'s execution targets (`ctx.agent_target` in normal mode, `ctx.admin_target` in admin
  mode): the harness is the FIRST capability whose readiness and ops actually run ON the VM as the
  target user (git-credential and vm-platform runups probe from the CLI host over HTTP/API), so it
  is the first real consumer of those RunContext transport fields, which the composition root
  populates for the harness's `runup` / op stages.
- **Room reserved for a permission model.** The execution surface is shaped so that running as the
  target user is the default grant, and the always-admin channel (`ctx.admin_target`, admin
  regardless of mode) is gated by an explicit grant, the trust knob for third-party harnesses when
  the plugin SDD arrives. This falls out of the model for free: `RunContext` already carries both
  transports as optional fields present only when the operation supplies them and (in a future
  permission model) when the capability is granted them. Only the target-user channel is used by the
  built-ins; nothing here needs more. Stated plainly so the model is honest: a harness is in-process
  code, so neither the context's contents nor a grant CONFINES a malicious one; the trust boundary
  for third-party harnesses is plugin installation and enablement (distribution trust, per the
  plugin SDD's framing). The permission model and the context's deliberate minimalism buy
  misuse-resistance and auditability for cooperating code, which is their whole claim.
- **The readiness check is `preflight` wherever the target exists, `runup` only for the ephemeral
  hole.** The target-environment check (today: required commands) should fail as early as possible,
  before the operator is prompted for anything, which means `preflight`, pre-resolve. Preflight is
  dependency-blind: it may not check state a later step of the same command creates. The model's
  mechanism for that is the optional context: preflight is handed the session's target only when it
  already exists. So the harness `preflight` probes `required_commands` exactly when the context
  carries the target (a session on an EXISTING agent/workspace, and every `session restart`),
  running pre-resolve and bailing before any prompt. The only case it cannot cover is a
  `--new-agent` / `--new-workspace` create, whose target user/workspace this command creates later
  (the direct analog of a git-credential preflight not checking a VM `vm create` has not made yet);
  that one case falls to `runup`, post-provision, against the real target. This keeps today's
  fail-before-prompt discipline for the common path and every restart, and confines `runup` to the
  ephemeral case where nothing earlier could have checked. (Probing the ephemeral target as admin at
  preflight was rejected earlier: it false-aborts on agent-template user-level tooling. The check
  runs as the real target user, or not yet.)
- **The harness slots into the existing ordering; it does not own it.** The command's order is the
  capability model's own: preflight-all (before any prompt or mutation) -> the single secret resolve
  at the preflight boundary (the one prompt session) -> ephemeral provisioning under rollback
  protection -> the harness `runup` in the real environment -> the `start` op -> tmux. The
  operator-facing consistency of that order (prompts up front, then non-interactive to success or a
  clean rollback with clear errors) is produced by the secret and isolation machinery, and the
  harness simply fits into it. Its one contribution to the failure story is small and uniform with
  every other stage: a `runup` failure tears down the just-created ephemerals via the same rollback
  path any later failure already exercises; when nothing ephemeral was requested there are no
  mutations before `runup`, so it still naturally precedes any change.
- **Harness code never prompts, and that is its whole obligation to the walk-away property.**
  `validate_config`, `preflight`, `runup`, `start`, and `restart` are non-interactive by contract;
  all operator interaction happens before dispatch. This is the one place the harness could break
  the system's walk-away guarantee, and forbidding it is the entire ask: the harness does not
  otherwise deliver walk-away, it just must not reopen a prompt once the interactive phase is done.
  The contract binds every harness, plugin-registered ones included, so it is part of the capability
  contract, not a convention of the built-ins.
- **The nested-create secret hoist is already handled upstream.** The pre-realignment draft carried
  a requirement to fold a `--new-agent` ephemeral agent's git-token secrets into the single up-front
  resolve (the nested `create_agent` used to resolve them inside the mutation block). Main's
  `create_session` already does this: it constructs the ephemeral agent's providers against the same
  resolver, folds their tokens into the one boundary resolve, and passes the resolved values to
  `create_agent` (which skips its own resolve when given them). So the walk-away point is already
  exact for nested creates, and this SDD adds nothing there. (An in-code comment near that path
  still claims `create_agent` re-resolves; it is stale relative to the current behavior and worth a
  cleanup commit, but that is not this SDD's surface.)
- **Restart's post-kill end state is pinned, not wished away.** Failures after the destructive kill
  (the `restart` op or tmux creation) cannot restore the old session; "rolls back to a consistent
  state" for that window means: the session row survives, the old tmux is gone,
  `agw session restart` is cleanly retryable, and the error says exactly which step failed. No
  resurrection is attempted. On restart the target already exists, so the required-commands check is
  a plain `preflight` and runs BEFORE the resolve (and before the kill): this preserves today's
  discipline that a declined or doomed restart never prompts for secrets it would discard, and a
  missing binary aborts before any prompt, with the old session still running. (Main's
  `restart_session` already resolves the session ENV chain via `resolve_for_command` only after its
  BROKEN/confirm gates; the harness preflight sits ahead of that.)
- **The `RunContext` gains the model's identity chain as a REQUIRED invariant, and this SDD adds
  it** (maintainer ruling, 2026-07-16). `RunContext` today carries global config, the execution
  targets, and resolved secrets, but no identity: no vm / workspace / agent / session NAMES. A
  harness needs them, at minimum the session name to address the tool session
  (`claude --name <session>`, distinct from the template name it is constructed under), plus the
  chain for probe context and error labels. This SDD adds a self-validating `OperationIdentity`
  value keyed by a `level` (the scope of the specific capability invocation: `system`, `vm`,
  `workspace`, `admin`, `agent`, or `session`) and makes it a REQUIRED field on `RunContext`, not an
  optional extra. A level is what THAT call concerns, not the ambient command: in one `vm create`
  the platform readiness concerns the VM (VM level) while each git-credential readiness call
  concerns a system-global credential (SYSTEM level). Each level carries its own name plus its
  ancestors up to the system slug (a session identity is the vm, workspace, agent-or-admin, and
  session names; a vm op is just the vm; the system-global capabilities `secret-backend` and
  `git-credential-provider` run at SYSTEM with identity being only the slug, as do cross-system
  scans like doctor's vm-site check). The object validates that the identity matches its level
  exactly, so it is always consistent and valid. The full hierarchy is enumerated, but only the
  levels a call site constructs today (`system`, `vm`, `session`) have their rules implemented;
  `workspace` / `admin` / `agent` raise `NotImplementedError` until a real call site needs them
  (their name fields still exist and are validated within a session). A context without an identity
  is an incomplete object, so the object enforces its presence and every construction site supplies
  one at the right level. Identity can be required where the timing-populated fields (targets,
  secrets) cannot, because the invocation's names and level are fixed at command entry (the one
  caveat: the system slug can be prompted once on a first-ever create, before any context is built).
  It is names-only for now, with room reserved for fuller representations (the HLA pins the shape).
- **Threading identity through the existing capability roots is in scope.** Because identity is now
  required on the shared `RunContext`, this SDD updates every current construction site (in
  `vms/manager.py`, `agents/manager.py`, `git_credentials/__init__.py`, and `doctor.py`) to pass the
  `OperationIdentity` appropriate to its operation, and carves out the capability README's "every
  field is optional" line for identity. This widens the SDD's blast radius beyond harness-local
  code, deliberately: the alternative (ship identity optional now, tighten later) would launch a
  non-final API, which the model should not do.
- **Getting the rest of this API surface right is a big part of the HLA**: what the core hands the
  harness (the `RunContext`: execution targets, resolved secrets, identity chain, bound config) and
  what the harness returns to the tmux layer are pinned there, not here. The requirement is the
  capability boundary, not its signature.
- **Best-effort robustness, not race-proof.** Harnesses should aim to be as robust as practical, but
  there is no expectation that they are perfect against race conditions and similar challenges. The
  canonical example is R4's existence check: the tool session changing state between the check and
  the launch may be problematic, and that is understood and accepted. Where an easy strengthening
  exists (e.g. moving the check into the launch script/command so check and launch are one
  invocation), take it; it is not worth overengineering beyond that. These are operator-interactive
  sessions, and a retry is an acceptable outcome for a window this small.
- **The role of tmux is unchanged by this effort.** Every session, regardless of harness, is
  launched by tmux and has its tty owned by tmux. Even a harness whose process has zero need for
  stdin/stdout/stderr still runs under tmux; it is how Agentworks manages the harness process
  (lifecycle, attach/detach, persistence), not just how the operator watches it. Harnesses decide
  WHAT runs in the pane, never HOW the pane is hosted.
- Template-variable substitution, `exec` wrapping, env composition, tmux mechanics, liveness probing
  of the running session, and every other session behavior stay core and unchanged.
- The resolved pair is validated at use exactly as declared blobs are validated at load; a resolved
  harness name always came from some declared (and therefore reference-validated) value or the
  built-in default, so no new failure mode exists at session-create time beyond today's.

### R8: Inspection surfaces

- `agw resource list` shows the two `harness` rows (built-in origin); `agw resource kinds` gains the
  kind with its category and description; `agw resource describe harness/<name>` shows the row, its
  description, and `Referenced by:` (the session templates that declare it).
- `agw resource describe session-template/<name>` renders `harness` / `harness_config` like any
  other spec fields, and the reference appears in its references list when declared.
- No doctor changes beyond what the registry surfaces provide for free.

### R9: Migration tool and samples

- `agw resource migrate` emits migrated session templates in the clean YAML shape: flat TOML fields
  emit as `harness: shell` + `harness_config: {...}` (mirroring the git-credential `provider_config`
  nesting precedent); declared `harness` / `harness_config` TOML keys pass through. The per-run
  registry-equivalence verification proves the divergence is shape-only, since the TOML loader's
  hoist (R6) and the migrator's emission land on the identical internal value.
- The bundled session-template sample teaches the new shape, leading with the `shell` +
  `harness_config` form (the default harness; the docs commit to runtime neutrality, maintainer
  ruling 2026-07-08), followed by the `claude-code` document as the worked capability example (one
  line where the old sample restated three command strings).
- `agw resource sample session-template` output updates accordingly; the samples-load-clean test
  keeps covering it.

### R10: Top-level documentation teaches the new model

The model change (Background) lands in the permanent docs, not just in code and this SDD:

- The top-level `README.md` "Sessions - the Workloads" section is rewritten around the new
  formulation: a session is a specification to run a specific harness as an agent in a workspace on
  a VM. The harness enters the model narrative as a first-class concept alongside VM, workspace,
  agent, and session (including the Ephemerality and Declarative Configuration sections where the
  layer vocabulary appears, as applicable).
- `cli/README.md` updates its session-template configuration schema (the `harness` /
  `harness_config` surface in YAML AND the nested TOML keys, the flat-field rules and the
  legacy-child harness-switch note per R6), its command reference where kind vocabulary appears, and
  its description of what a session runs.
- `cli/agentworks/sample-config.toml`: the commented session-template example stops hardcoding the
  Claude command strings; it shows the flat shell form (the documented default TOML shape per R6)
  and points at `agw resource sample session-template` as the primary authoring surface.
- `docs/guides/resources.md` gains the harness in its capability story (it already carries
  `secret-backend` and `git-credential-provider`), with worked session-template examples in the new
  shape.
- The decision to introduce the harness is its own ADR covering: the model formulation, the inline
  reference+blob consumer shape as shipped, and the pair-inheritance rule. Per the SDD style, the
  ADR is DRAFTED in this feature directory (`adr-session-harness.md`, unnumbered) and moves into
  `docs/adrs/`, receiving its number then, only at the very end of the effort, so the number is
  assigned once against the directory as it exists at merge time. ADR 0016 is untouched; the new ADR
  references it for the capability collapse and `capabilities/README.md` for the lifecycle contract
  the harness adopts (it is the first RICH consuming resource of that model, and the first to use
  the `RunContext` execution targets, worth calling out in the ADR's consequences).
- Per the SDD lifecycle rules, each doc change rides the commits that make its claims true; nothing
  here waits for a closeout pass, and nothing permanent cites this SDD's path.

## Non-goals

- **Plugin-registered harnesses**: the registry dict is where they will land; the loading,
  enablement, namespacing, and origin stamping are the plugin SDD's.
- **Artifact management (skills, hooks, MCP servers, allow/deny lists, ...)**: an expected future in
  which the core manages such artifacts generically and the harness translates them for its specific
  tooling (see the model-change section). Deferred: it needs an operator-declared artifact model
  first (plugin SDD territory), but it is part of why the harness seam exists. Its security
  dimension (a launched agent silently inheriting the operator's user-level MCP servers) is recorded
  in R4's reserved future directions, with the safe default (non-inheritance) pinned now so v1 does
  not build in the footgun.
- **claude-code auth modes, question-timeout control, and MCP-inheritance policy**: recorded as R4
  reserved future directions, not built in v1. The v1 `claude-code` config vocabulary is the pinned
  `permission_mode` / `model` / `extra_args` set.
- **Liveness probing behind the harness**: today's boot-id/PID probe is tool-agnostic and stays
  core; a harness-owned probe can be added to the contract when a harness actually needs one.
- **Alternatives to tmux**: possibly introduced someday, but that would be an orthogonal effort; the
  harness concept neither depends on tmux specifics nor would change if the pane host did (R7's tmux
  invariant).
- **A Codex harness**: nothing blocks one, but no member ships until its conventions are pinned;
  `claude-code` proves the multi-member path.
- **Moving `claude_marketplaces` / `claude_plugins`**: agent-provision surface, untouched (R4).
- **A dedicated `harness` declarable kind**: deliberately absent per the capability collapse; if a
  harness someday needs multiple configured instances with identity beyond one reference site, the
  graduate-when-real clause applies.

## Migration notes

Operators upgrading across this SDD see:

- TOML session templates keep loading unchanged, with one deliberate behavioral divergence: a
  multi-parent lineage where a later parent declares none of the command fields no longer wipes an
  earlier parent's command to empty (R5's pair-inheritance divergence). Single-parent and
  command-declaring lineages are unaffected. YAML manifests written against the unreleased PR #156
  surface with flat command fields (if any exist) get a load error pointing at `harness_config`; the
  `!` flag in R2 covers exactly this.
- `agw resource list` gains two `harness` rows; `agw resource kinds` gains the `harness` kind.
- Newly migrated or sampled session templates spell commands under `harness_config`.
- Templates can now say `harness: claude-code` instead of restating the Claude Code command strings.
