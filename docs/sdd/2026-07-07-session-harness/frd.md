# Session harness capability: functional requirements

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

## Background

Two landed foundations set the stage for this SDD.

**The capability model** (resource-manifests SDD, `docs/sdd/2026-07-01-resource-manifests`, PR #156,
ADR 0016). Resources are `(kind, name)` registry rows; kinds split into declarable kinds (data) and
capability kinds (read-only rows backed by a per-domain code registry); resources reference
capabilities directly, carrying capability-owned config at the reference site (the capability
collapse). Its companion `capability-consumers.md` (row 5) sketched the first NEW capability to
build on that model: the **harness**, selected inline on the session template.

**The orchestration layer** (`docs/sdd/2026-07-16-orchestration-layer`, MERGED and locked). This is
the load-bearing change for this SDD, because it built, as general machinery, almost everything an
earlier draft of this harness SDD had tried to invent harness-side. It split composition from
resources:

- Two contracts (`orchestration/node.py`): `Readiness` (`preflight` + `runup`) and `Node`
  (`Readiness` + `key` + `deps` + `secret_refs`). A capability instance is a `Readiness`, HELD by a
  node and composed by that node's `preflight`/`runup`, never walked. Only consuming resources and
  live resources are nodes.
- Per-command orchestrators walk the node graph, run preflight-all, resolve secrets once at the
  boundary, drive runups and ops, and own unwind. Nodes declare; they never walk or resolve.
- Pending nodes carry a `realized` flag the orchestrator flips (`mark_realized`); readiness checks
  defer on a pending target and probe on a realized one.
- `OperationScope` + `ScopeLevel` (`capabilities/base.py`): the command's static identity chain,
  keyed by level (`system` / `vm` / `workspace` / `agent` / `session`), one per command, on the
  context; a node reads the LEVEL off it (the readiness fork) and treats its names as descriptive.
- `RunContext`: `config` and `operation_scope` as plain fields; execution targets and secrets
  through accessor methods (`ctx.admin_target()` / `ctx.agent_target()` / `ctx.secret(name)`,
  pass-through in v1). `Capability` constructs `(owner_name, config)`, no bound resolver; runup and
  ops read `ctx.secret`.

Crucially, the merged session node (`sessions/nodes.py`) already holds a `Readiness` stand-in,
`RequiredCommandsCheck`, described in its own code as "the harness-LIKE stand-in the harness
capability replaces when it lands," plus the interim imperative pane-command path
(`_build_session_command`). **This SDD delivers the real harness and swaps it in for both.** The
harness is not new orchestration machinery; it is one `Readiness` capability the session node holds,
so this SDD is now small: the capability kind, two built-ins, the template surface, and the swap.

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
readiness check (today: required commands). In the merged model's vocabulary that is a `Readiness`
capability (its `preflight`/`runup` are the readiness check) with two ops, `start` and `restart`,
each returning the pane command for its case. Narrow does not mean string-valued: within that
lifecycle a harness may execute code on the launch target as the target user (R7, through
`ctx.agent_target()` / `ctx.admin_target()`), which is what lets it be smarter than the strings it
replaces (R4). Liveness probing of the running session stays core (tmux boot-id/PID machinery,
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

### Target state: the harness as a tool adapter (forward-looking, NOT v1)

This subsection records the cohesive target state the v1 harness fits into, so the v1 boundaries
read as deliberate rather than arbitrary. None of it is built in this effort; it exists to keep the
design pointed somewhere and to explain why v1 leaves certain adjacent surfaces alone (maintainer
discussion, 2026-07-18).

**A harness is a tool's ADAPTER to Agentworks, and adaptation happens at more than one point.** The
session harness this SDD builds is the RUNTIME face: it adapts "run this workload" to the tool's
launch semantics (start/restart, resume). The other face is PROVISIONING: adapting Agentworks's
generic agent artifacts to the tool's user-provisioning semantics (where its config files live, how
skills and rules and MCP register). Both faces do the same essential job, translate generic intent
into tool-specific mechanics, which is what makes them the harness rather than incidental plumbing.

**Naming: `harness` is the runtime; provisioning is `<level>-harness-prep`.** A harness is
inherently a session-level concept (it runs the tool), so it keeps the bare name and never needs a
`session-` qualifier. The provisioning pieces are not harnesses (they run nothing); they PREPARE an
environment for the harness, so they are `<level>-harness-prep`, level-qualified because prep can
exist at more than one level while a runtime harness cannot. The first is `agent-harness-prep` (the
agent user's home: `~/.claude`, user plugins, skills, rules, MCP). Later candidates follow the
tool's own config layering: `workspace-harness-prep` (project-scoped config in the workspace dir)
and, rarely, `vm-harness-prep` (system-scoped). ("agent" vs "user" for the first level is unsettled:
the merged model's `ScopeLevel` says AGENT, but the provisioning is user-home-scoped and an
admin-run session's user is not an agent; resolved when the prep is designed.)

**What `agent-harness-prep` holds, two kinds:**

- **Tool-native config**, its own capability-owned blob (the prep's equivalent of `harness_config`).
  This is the eventual home of today's `claude_marketplaces` / `claude_plugins` agent-template
  fields: they are Claude-Code-native, so they re-home onto the claude-code prep the same way the
  session command strings re-homed onto the session harness. v1 deliberately leaves them where they
  are (R4), because moving them belongs with the prep capability, not this SDD.
- **The translator role.** Generic artifacts (skills, rules, MCP intent, "here is your env") are
  emitted tool-agnostically by features and the core artifact model (WHAT); the prep exposes them
  per the tool's conventions (HOW). This is the artifact-management future above, made concrete: the
  generic model knows no tool, and each tool's prep is the only tool-aware translator. The R4
  user-level-MCP security item lands here on the PROVISIONING side: user MCP is generic intent the
  prep exposes, and the non-inheritance default is the policy on that translation (the session
  harness's role is reading/enforcing at launch, not provisioning).

**Coupling is DECLARATIVE, not runtime-detected.** Most prep output is enrichment: absent it, the
session still runs, just worse, so there is nothing to probe for and turn into an error. The
reliable tie is a declared-prep record: a session's `harness: claude-code` implies a required
`claude-code` agent-harness-prep, and the check is "was this agent prepped with it" (a
provisioning-record check, warning-level), never an inspection of individual artifacts. Only a hard
prereq (if a prep also installs the tool binary) stays detectable, via the session harness's own
readiness; enrichment does not.

**The session-side schema, when it exists, sorts by OWNERSHIP into three buckets** (the principle,
concrete field names deferred to the prep design):

- `harness` (framework-owned selector) and `harness_config` (the selected capability's own config),
  as v1 ships them; plus
- a capability-AGNOSTIC framework group (working name `harness_support`) for cross-cutting settings
  the specific harness does not own: `require_agent_prep` (an escape hatch; the selector implies its
  own prep, so this is only for extra or cross-harness requirements) and `expose_artifacts` (deliver
  THIS session's agent's generic artifacts into the session workspace at launch, as an alternative
  when the agent cannot be pre-prepped). `expose_artifacts` is scoped to the session's own agent,
  not a roll-up of every agent's data; even so it carries a residual visibility caveat (workspace
  co-tenants can see the exposed artifacts), so it is an explicit opt-in, not a default. The rule
  that keeps this coherent: does a field configure the specific harness (-> `harness_config`), the
  harness framework generically (-> the agnostic group), or the plain session (-> the spec root)?

The packaging unit for all of this is the plugin (plugin SDD): a tool ships as a plugin that
registers a `harness` plus its `harness-prep`(s) under one identity, which is what lets the
session's implied-prep coupling and the shared translation knowledge work.

### Scope

In scope: the `harness` capability kind and code registry (in the `capabilities/` subtree, extending
the `Capability` base); the built-in `shell` and `claude-code` harnesses; the
`session-template.spec.harness` / `spec.harness_config` consumer surface (YAML and TOML); the
inheritance semantics of the pair; swapping the real harness into the merged session node in place
of the interim `RequiredCommandsCheck` (readiness) and `_build_session_command` (ops); the
migration-tool and sample updates the surface change requires; the top-level documentation rewrite
that promotes the new model (R10).

Provided by the merged orchestration layer, so NOT in scope here: the readiness lifecycle
(`preflight`/`runup`) and the boundary-resolve/walk-away machinery; the operation scope and its
level (the harness READS the level, does not define it); pending nodes and the realized/defer signal
(the harness reads a target node's `realized`, it does not carry a `to_create` field); the session
node itself, its edges, and unwind; the single up-front secret resolve for a `--new-agent` ephemeral
agent. Everything an earlier draft of this SDD proposed to add to `RunContext` (a required identity
object, a `to_create` set) is superseded: it now lives in the orchestration layer, and the harness
simply consumes it.

Out of scope: plugin-registered harnesses, asset placement, session liveness probing, agent-level
Claude fields (`claude_marketplaces` / `claude_plugins` on agent/admin templates are provision-time
surface, untouched here), and any change to the other capability domains or the orchestration layer.

## Terminology

- **Harness**: the session-level capability, code that knows how a named session of a particular
  tool is started and restarted, and what executables that requires. A capability kind per ADR 0016:
  read-only registry rows, error miss policy, implementation in a per-domain code registry
  (`HARNESS_REGISTRY`). A consuming-side capability like `git-credential-provider`: its
  implementations extend `capabilities.base.Capability` (so they satisfy `Readiness`), and the
  session node HOLDS one and composes it. Named by `session-template.spec.harness`. The kind takes
  the domain's natural noun with no `-provider` suffix (ADR 0016's naming rule: a disambiguating
  suffix only on collision, and nothing else here is called a harness).
- **The harness's lifecycle**: `validate_config` validates the `harness_config` blob's shape (both
  built-ins return no references); construct binds `(name, merged_config)`; `preflight` and `runup`
  are the readiness (the required-commands check, which floats between the two stages by the target
  node's pending-ness exactly as the merged `RequiredCommandsCheck` does today); the ops are `start`
  and `restart`, each returning the pane command for its case. A `RunContext` carries `config`,
  `operation_scope`, and the `ctx.agent_target()` / `ctx.admin_target()` / `ctx.secret()` accessors.
- **Harness config**: the capability-owned blob at the reference site
  (`session-template.spec.harness_config`), validated by the selected harness. The inline form of
  reference+blob (capability-consumers.md rule 2): the template is the only consumer, so the FIELD
  is the selector and no dedicated instance kind exists.
- **`shell` harness**: the built-in default reproducing today's behavior verbatim. Its config
  vocabulary IS the three legacy fields: `command`, `restart_command`, `required_commands`.
- **`claude-code` harness**: the built-in harness owning the Claude Code session conventions, launch
  vs resume (chosen by inspecting tool state, R4), required executable, so templates stop spelling
  them as strings.
- **Node, Readiness, operation scope, pending/realized**: as defined by the orchestration-layer SDD;
  the harness consumes them.
- **Session template, registry, capability, origin**: as defined by the resource-registry and
  resource-manifests SDDs.

## Requirements

### R1: The `harness` capability kind

- New capability kind `harness`: `category = "capability"`, error miss policy, not
  manifest-declarable (a `kind: harness` document gets the standard capability-kind envelope error),
  `builtin_override = "reserved"`. Same shape as `git-credential-provider`; the kind strategy
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
  never imports the `sessions/` domain (the consuming node depends on the capability, not the
  reverse); the harness never imports the `orchestration/` package either, only the `Readiness`
  contract it satisfies structurally.

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
  capability config-validation contract (`Capability.validate_config(owner, config)`): it returns
  the resource references the blob implies (none, for both built-in harnesses), and the session
  template emits them as itself at finalize, alongside the selector edge. A future harness whose
  blob names a secret gets auto-declaration, reachability, and doctor coverage with zero new
  machinery, and those secrets fold into the session node's `secret_refs` (the merged model: a held
  instance's declared secrets surface through its holder).

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
  returns `restart_command` when set, else `command`; readiness probes `required_commands` on the
  launch target, floating between `preflight` and `runup` by the target node's pending-ness (R7),
  which is exactly the four-way fork the merged `RequiredCommandsCheck` already implements.
  Template-variable substitution (`{{session_name}}`, `{{workspace_name}}`) stays core and applies
  to the returned command string, as today.
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
- HOW the existence check runs (a probe as the session user via `ctx.agent_target()` before the pane
  command is built, runtime shell logic in the pane command, or some combination) is the HLA's call.
  It addresses the tool session by its own `session_name` (given at construction, per the merged
  identity model), not through the operation scope's names. The detection is an op-time concern (it
  runs when `start` / `restart` build the pane command), not a readiness concern.
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
  provision-time (VM/agent setup) concerns, not session-start concerns. Their eventual home is the
  `agent-harness-prep` capability (see the target-state subsection above); v1 leaves them untouched.
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
    accidentally build in silent inheritance. The PROVISIONING of user MCP is `agent-harness-prep`'s
    job (target-state subsection above); the session harness's role is reading/enforcing the policy
    at launch.
  - **Question-timeout control.** Claude Code's interactive question-timeout behavior (how long it
    waits on a prompt before it proceeds or fails) is a candidate `harness_config` field, so an
    unattended/walk-away session can pin the timeout policy the operator wants rather than the CLI
    default.
  - **Claude-subscription (OAuth) authentication.** Authenticating a launched Claude Code session
    via a Claude subscription OAuth flow, as an alternative to an API key, is a future auth mode the
    harness would own (a first interactive step at launch, or a provision-time credential the
    harness consumes). It interacts with the walk-away invariant (any interactivity must precede the
    boundary resolve) and with the secret model, so it is deferred until its shape is pinned.
  - **Remote-control enablement.** Claude Code has a well-defined remote-control feature (a running
    session can be attached-to and driven from a remote surface, the Claude app or web). It fits the
    walk-away model, so exposing it is a candidate `harness_config` field: whether a launched
    session enables remote control. Unlike the MCP item above, this carries NO special harness
    responsibility. It is off unless the operator turns it on, which is the obvious right default
    and already how the harness behaves (it enables nothing it is not told to), and anyone who
    enables it knows what the feature is. Securing the remote-control channel itself is Anthropic's,
    not the harness's; the harness's whole job here is to expose the toggle with the off default. It
    is a plain config field, called out only so the vocabulary is complete, not a footgun the
    harness has to defend against. (The contrast with MCP inheritance is the point: that one is a
    SILENT side effect of authentication, which is why it is the harness's to fix; this one is an
    explicit, understood feature the operator opts into.)

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

### R7: The harness is a `Readiness` capability the session node holds

The merged model gives the harness its home: the session node (`sessions/nodes.py`) holds a
`Readiness` instance and composes it, and today that instance is the interim
`RequiredCommandsCheck`. This SDD replaces that instance with the real harness and replaces the
interim `_build_session_command` pane-command path with the harness's `start` / `restart` ops. What
a harness is FOR is unchanged: extracting maximum value from the specific tool it wraps (resume the
right named session, forward the flags that matter, check the required executables). What it is NOT
responsible for, the operator's walk-away experience and the system's consistency, is delivered by
the orchestration and isolation machinery around it; the harness's whole obligation to that
machinery is to stay non-interactive.

- **The session node holds the harness instance and composes its readiness.** The session factory
  constructs `harness_for(name)(session_template_name, merged_harness_config)` from the resolved
  template's `(harness, harness_config)` pair and hands it to the session node, replacing the
  `RequiredCommandsCheck` the node holds today. The session node's `preflight` / `runup` delegate to
  the harness's, exactly as they delegate to the check today (`self._harness.preflight(ctx)`), and
  the session orchestrator calls the harness's `start` / `restart` op where it calls
  `_build_session_command` today. The harness is a `Readiness`, never a graph node: it has no `key`
  and no `deps`, and the orchestrator never walks it.
- **Readiness is the required-commands check, and it keeps the merged fork.** The readiness fork the
  merged `RequiredCommandsCheck` implements moves into the harness unchanged. Its precondition: a
  scope-less context (`ctx.operation_scope is None`) is itself a LOUD error, an orchestrator bug,
  never a silent skip. Within a present scope the four-way fork runs: out of scope for the
  operation's LEVEL (a system-scoped doctor scan reaching a session) SKIPS; in scope with a pending
  target DEFERS to runup; in scope with a realized target PROBES now (the earlier-failure win for
  existing agents and every restart); in scope with the target absent for another reason is a LOUD
  error, never a silent skip. The harness reads the LEVEL off `ctx.operation_scope` and the target's
  `realized` off the agent-or-admin node it is constructed with; `to_create` does not exist in this
  model (pending-ness lives on the node). The harness carries its OWN session identity (name plus
  vm/workspace/agent-or-admin ancestors, captured at construction from the session's rows) and acts
  and frames through that, not through the operation scope's names, which are the operation's
  identity, a different thing that only coincides; at SESSION level it verifies the scope matches
  its own as a guard against a mis-wired context (HLA). `preflight` and `runup` are general hooks (a
  future harness may add target-independent checks to preflight or authenticated checks to runup);
  the built-ins fill only the required-commands slice.
- **The harness executes on the launch target as the target user**, through `ctx.agent_target()`
  (normal mode) or `ctx.admin_target()` (admin mode), the accessor methods the merged `RunContext`
  exposes. The `shell` harness uses none of this (its ops return a static string); `claude-code`
  needs it for the resume-vs-launch detection (R4); future harnesses get it for free. Harness logic
  runs CLI-side; only the commands it issues run on the target.
- **The one-object target contract (merged, restated for the swap).** The agent-or-admin node the
  session node depends on and the same node the harness is constructed with as its `target` MUST be
  the same object, so when the orchestrator flips the target `realized` the harness sees it. The
  merged `pending_session_node` / `live_session_node` factories already enforce this for the interim
  check; the harness swap preserves it (the factory hands the one node to both the session's dep and
  the harness's target).
- **Harness code never prompts.** `validate_config`, `preflight`, `runup`, `start`, and `restart`
  are non-interactive by contract; all operator interaction happens before the boundary resolve,
  which the orchestrator owns. This is the harness's only obligation to the walk-away property; it
  does not otherwise deliver it. The contract binds every harness, plugin-registered ones included.
- **Restart's post-kill end state is pinned** (parity with today, preserved by the orchestrator): a
  failure after the destructive kill cannot restore the old session, so the session row survives,
  the old tmux is gone, `agw session restart` is cleanly retryable, and the error names the failed
  step. On restart the target already exists, so readiness probes at `preflight`, pre-resolve and
  pre-kill, so a missing binary aborts with the old session still running.
- **Best-effort robustness, not race-proof.** Harnesses aim to be as robust as practical; there is
  no expectation of perfection against races. R4's existence check is the canonical example: the
  tool session changing state between check and launch is understood and accepted; where an easy
  strengthening exists (folding the check into the launch snippet so check and launch are one
  invocation) take it, and do not overengineer beyond that.
- **The role of tmux is unchanged.** Every session, regardless of harness, is launched by tmux and
  has its tty owned by tmux. Harnesses decide WHAT runs in the pane (the returned string), never HOW
  the pane is hosted. Template-variable substitution, `exec` wrapping, env composition, tmux
  mechanics, and liveness probing stay core and unchanged.
- The resolved pair is validated at use exactly as declared blobs are validated at load; a resolved
  harness name always came from some declared (and therefore reference-validated) value or the
  built-in default, so no new failure mode exists at session-create time beyond today's.

### R8: Inspection surfaces

- `agw resource list` shows the two `harness` rows (built-in origin); `agw resource kinds` gains the
  kind with its category and description; `agw resource describe harness/<name>` shows the row, its
  description, and `Referenced by:` (the session templates that declare it).
- `agw resource describe session-template/<name>` renders `harness` / `harness_config` like any
  other spec fields, and the reference appears in its references list when declared.
- No doctor changes beyond what the registry surfaces provide for free. (Doctor already reaches
  session readiness at SYSTEM scope in the merged model; the harness's readiness SKIPS there, per
  the fork above, so no doctor-specific branch is needed.)

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
- `capabilities/README.md` gains the harness as a worked example of a capability HELD by a rich
  consuming node (the session), the case its thin-vs-rich guidance describes.
- The decision to introduce the harness is its own ADR covering: the model formulation, the inline
  reference+blob consumer shape as shipped, and the pair-inheritance rule. Per the SDD style, the
  ADR is DRAFTED in this feature directory (`adr-session-harness.md`, unnumbered) and moves into
  `docs/adrs/`, receiving its number then, only at the very end of the effort. ADR 0016 is
  untouched; the new ADR references it for the capability collapse and the orchestration-layer ADR
  for the node/readiness model the harness plugs into (the harness is a `Readiness` held by the
  session node, not a graph node, worth stating in the consequences).
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
- **claude-code auth modes, question-timeout control, MCP-inheritance policy, and remote-control
  enablement**: recorded as R4 reserved future directions, not built in v1. The v1 `claude-code`
  config vocabulary is the pinned `permission_mode` / `model` / `extra_args` set.
- **Any change to the orchestration layer**: it is merged and locked; the harness consumes it as-is.
  If the swap reveals a genuine gap in the node/readiness contract, that is an orchestration-layer
  follow-up, raised separately, not folded in here.
- **Liveness probing behind the harness**: today's boot-id/PID probe is tool-agnostic and stays
  core; a harness-owned probe can be added to the contract when a harness actually needs one.
- **Alternatives to tmux**: possibly introduced someday, but that would be an orthogonal effort; the
  harness concept neither depends on tmux specifics nor would change if the pane host did (R7's tmux
  invariant).
- **A Codex harness**: nothing blocks one, but no member ships until its conventions are pinned;
  `claude-code` proves the multi-member path.
- **The provisioning face of the harness (`agent-harness-prep` and its `workspace`/`vm` kin)**: the
  whole two-face target state, the prep capability, the generic-artifact translation, the session
  `harness_support` schema, and the declared-prep coupling, is recorded as forward-looking direction
  (the target-state subsection) and built by a later effort, not here.
- **Moving `claude_marketplaces` / `claude_plugins`**: agent-provision surface, untouched; their
  target-state home is `agent-harness-prep` (target-state subsection), not this SDD.
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
- Session behavior is otherwise unchanged: the same commands run, the same prompts at the same times
  (the orchestration layer already governs that), and existing sessions restart identically.
