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

The session remains the workload specification -- naming, lifecycle, tmux persistence, env
composition, and placement (vm / workspace / agent) stay exactly where they are -- but it delegates
the mechanics of running the underlying process to the harness, which can be smarter than an opaque
command string because it knows the specific tooling it runs. The harness joins the model's core
vocabulary alongside VM, workspace, agent, and session, and the top-level documentation that teaches
the model is rewritten to teach it this way (R10).

The scope of the harness CONTRACT in this SDD is deliberately narrow (maintainer ruling,
2026-07-07): **command lifecycle only** -- session start, session restart, and the
target-environment preflight (today: required commands), the responsibilities the three template
fields express today. Narrow does not mean string-valued: within that lifecycle a harness may
execute arbitrary code on the launch target as the target user (R7), which is what lets it be
smarter than the strings it replaces (R4). Liveness probing of the running session stays core (tmux
boot-id/PID machinery, tool-agnostic), and asset placement (skills, rules, allow/deny lists) is
deferred with the plugin SDD, which owns the operator-declared asset model it requires. The narrow
contract does not narrow the model change: the delegation seam is the point, and the contract grows
behind it without the model changing again.

A concrete future direction makes that point. We expect Agentworks to eventually manage artifacts
such as skills, hooks, and MCP server configurations, and the division of labor is already implied
by this model: the core handles these objects in the general sense (declaration, storage, scoping),
and the harness translates them for its specific tooling -- its directory conventions, file formats,
and registration mechanics. None of that is in this effort; it is called out so the harness is read
as the tool-knowledge seam it is, not as plumbing built just for start/restart semantics.

### Scope

In scope: the `harness` capability kind and code registry; the built-in `shell` and `claude-code`
harnesses; the `session-template.spec.harness` / `spec.harness_config` consumer surface (YAML and
TOML); the inheritance semantics of the pair; the runtime delegation in the sessions manager; the
nested ephemeral-agent secret-resolution hoist that makes the walk-away invariant exact (R7); the
migration-tool and sample updates the surface change requires; the top-level documentation rewrite
that promotes the new model (R10).

Out of scope: plugin-registered harnesses, asset placement, session liveness probing, agent-level
Claude fields (`claude_marketplaces` / `claude_plugins` on agent/admin templates are provision-time
surface, untouched here), and any change to the other capability domains.

## Terminology

- **Harness**: the session-level capability -- code that knows how a named session of a particular
  tool is started and restarted, and what executables that requires. A capability kind per ADR 0016:
  read-only registry rows, error miss policy, implementation in a per-domain code registry
  (`HARNESS_REGISTRY`). Named by `session-template.spec.harness`. The kind takes the domain's
  natural noun with no `-provider` suffix (ADR 0016's naming rule: a disambiguating suffix only on
  collision, and nothing else here is called a harness).
- **Harness config**: the capability-owned blob at the reference site
  (`session-template.spec.harness_config`), validated by the selected harness. The inline form of
  reference+blob (capability-consumers.md rule 2): the template is the only consumer, so the FIELD
  is the selector and no dedicated instance kind exists.
- **`shell` harness**: the built-in default reproducing today's behavior verbatim. Its config
  vocabulary IS the three legacy fields: `command`, `restart_command`, `required_commands`.
- **`claude-code` harness**: the built-in harness owning the Claude Code session conventions --
  launch vs resume (chosen by inspecting tool state, R4), required executable -- so templates stop
  spelling them as strings.
- **Session template, registry, capability, origin**: as defined by the resource-registry and
  resource-manifests SDDs.

## Requirements

### R1: The `harness` capability kind

- New capability kind `harness`: `category = "capability"`, error miss policy, not
  manifest-declarable (a `kind: harness` document gets the standard capability-kind envelope error),
  `builtin_override = "reserved"`. Same shape as `secret-backend` and `git-credential-provider`.
- Implementations live in `HARNESS_REGISTRY` (new `agentworks/harness/` package). The app publishes
  one read-only row per registered harness with `Origin.built_in(source="agentworks.harness")`, so
  template references validate through the framework's uniform miss policy and harnesses appear in
  `agw resource list` / `agw resource kinds` / `agw resource describe harness/<name>` like every
  other resource.
- Built-in members: `shell` (R3) and `claude-code` (R4). Plugin registration is the plugin SDD's.

### R2: Template surface -- `harness` and `harness_config`

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
  owner) -- the default does not silently adopt a blob.
- A declared `spec.harness` emits a `harness`-kind resource reference from the template (usage: "the
  session harness"), so a typo'd name is a finalize-time `ConfigError` naming the template, and the
  harness row's `Referenced by:` lists its templates. Templates that do not declare the field emit
  no edge -- their effective harness is an inherited or defaulted value that is validated where it
  was declared (or is the always-valid built-in default).
- The declared blob is validated by the selected harness at load (decode/parse time), with errors
  carrying the declaration's location (the manifest document's `file:line`; the TOML section's
  location for TOML declarations). Declared-blob validation is VOCABULARY AND SHAPE ONLY (per-field
  checks); completeness rules -- required fields, cross-field constraints -- apply to the MERGED
  blob at use (R5, R7), because a child restating the harness may legitimately declare a partial
  blob. Validation is capability-owned: the core never learns any harness's field vocabulary. It
  uses the capability config-validation contract the resource-manifests SDD shipped (its Phase 5.7,
  the `GitCredentialProvider.validate_config` precedent): `validate_config(owner, config)` returns
  the resource references the blob implies -- none, for both built-in harnesses -- and the session
  template emits them as itself at finalize, alongside the selector edge. A future harness whose
  blob names a secret gets auto-declaration, reachability, and doctor coverage with zero new
  machinery.

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
  strings) -- exactly today's fields, exactly today's semantics. All optional; an empty config is a
  plain login shell, exactly like today's field-less `default` template.
- Behavior: start returns `command` (empty means login shell only); restart returns
  `restart_command` when set, else `command`; the required-commands preflight probes
  `required_commands`. Template-variable substitution (`{{session_name}}`, `{{workspace_name}}`)
  stays core and applies to the returned command string, as today.
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
- HOW the existence check runs (a probe as the session user before the pane command is built,
  runtime shell logic in the pane command, or some combination) is the HLA's call, on the execution
  surface R7 requires.
- Config vocabulary (owned by the harness, snake_case per project convention; the FIELD SET is
  pinned at LLD, with all flag spellings and the detection mechanism verified against the latest
  stable Claude Code CLI at implementation):
  - `permission_mode` (optional string): forwarded as the corresponding CLI flag.
  - `model` (optional string): forwarded as the corresponding CLI flag.
  - `extra_args` (optional list of strings): appended verbatim to the launch/resume invocation, the
    escape hatch that keeps the harness from needing a field per flag.
- Unknown config fields are validation errors naming the harness and the field.
- The chosen path is visible to the operator: the launch surfaces whether it resumed the existing
  Claude session or launched fresh (mechanism at LLD -- an output line or the pane's first visible
  output). The decision must never be silent (review finding, 2026-07-08; operator-control spirit).
- `claude_marketplaces` / `claude_plugins` stay on the agent/admin templates: they are
  provision-time (VM/agent setup) concerns, not session-start concerns. The plugin SDD decides
  whether they move when Claude Code becomes a plugin.

### R5: Inheritance -- the pair travels together

`harness` and `harness_config` inherit as a unit (capability-consumers.md rule 2):

- A child that does not declare `harness` inherits its parents' effective pair unchanged.
- A child that declares `harness` DIFFERENT from the inherited one starts from a fresh (empty)
  config; the parent's blob is addressed to the wrong capability and never leaks across.
- A child that declares the SAME `harness` as the inherited one (restating it -- required, since
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
  the lineage back to shell with a fresh blob -- correct per the switch rule, and called out in the
  docs (R10) so it reads as designed behavior rather than a surprise.
- No new deprecation warning is added for the flat fields themselves: the section already carries
  the TOML-resource deprecation warning. Both TOML spellings are documented, YAML-first (maintainer
  ruling, 2026-07-08): `cli/README.md` documents the nested `harness` / `harness_config` keys
  alongside the flat fields, pointing at YAML manifests as the primary surface, and the flat form
  remains the documented default TOML shape until the TOML resource path retires (Phase 6 of the
  resource-manifests plan).

### R7: Runtime delegation and the harness execution surface

- The sessions manager stops reading command strings off the resolved template and delegates to the
  resolved harness instead: session start, session restart, and the target-environment preflight
  (today: the required-commands check; shaped to grow into file, tool-state, and vm / workspace /
  agent state checks without a contract change) all dispatch through
  `HARNESS_REGISTRY[resolved.harness]` with the resolved config blob.
- **A harness is not limited to returning a static string.** The contract gives it the ability to
  execute arbitrary code on the launch target as the TARGET USER -- the user the session runs as:
  the selected agent, or the admin user in admin mode -- including multiple commands (state probes,
  tool interrogation) in the course of deciding what the session runs. The `shell` harness uses none
  of this; `claude-code` needs it (R4); future harnesses get it for free.
- **Room reserved for a permission model.** The execution surface is shaped so that running as the
  target user is the default grant, and an always-admin channel (admin regardless of mode) can be
  added later behind an explicit grant -- the trust knob for third-party harnesses when the plugin
  SDD arrives. Only the target-user channel ships in this SDD; nothing here needs more. Stated
  plainly so the model is honest: a harness is in-process code, so neither the context's contents
  nor a grant CONFINES a malicious one -- the trust boundary for third-party harnesses is plugin
  installation and enablement (distribution trust, per the plugin SDD's framing). The permission
  model and the context's deliberate minimalism buy misuse-resistance and auditability for
  cooperating code, which is their whole claim.
- **Interactivity first; preflight in the real environment; rollback to consistency.** The operator
  experience this contract guarantees: start the command, deal with any secret prompts or early
  failures immediately, then walk away -- everything after the prompts is non-interactive and either
  succeeds or rolls back to a consistent state with clear error messaging. Concretely: all operator
  interaction (secret prompts) completes before any state change (the established eager-resolve
  principle); ephemeral resources the session needs (workspace, agent) are then provisioned under
  rollback protection; and the preflight runs after provisioning, in the REAL session environment --
  the actual target user, the actual workspace (preflight MAY depend on workspace files), the fully
  resolved env (vm, workspace, agent, and session scopes). A preflight failure tears down the
  just-created ephemerals -- benign by design, and the same rollback path any later failure already
  exercises. When nothing ephemeral was requested, there are no mutations before preflight, so it
  naturally still runs before any change.
- **Harness code never prompts.** `preflight`, `start`, and `restart` are non-interactive by
  contract; all operator interaction happens before dispatch. The walk-away invariant depends on
  this holding for every harness, plugin-registered ones included, so it is part of the capability
  contract, not a convention of the built-ins.
- **The walk-away point becomes exact for nested creates too** (maintainer ruling, 2026-07-08;
  surfaced by review): today `session create --new-agent` lets the nested `create_agent` resolve its
  git-token secrets INSIDE the rollback-protected mutation block -- a prompt can fire after the
  ephemeral workspace exists, violating the invariant this requirement states. This SDD closes that
  gap: the session-create composition root includes the ephemeral agent's needed secrets in its
  single up-front resolve and threads the values through the nested-create seam. This deliberately
  amends the seam contract the resource-manifests SDD pinned (CLI-shaped args only); its guard test
  is updated with the same intent it was written to protect.
- **Restart's post-kill end state is pinned, not wished away.** Failures after the destructive kill
  (the `restart` dispatch or tmux creation) cannot restore the old session; "rolls back to a
  consistent state" for that window means: the session row survives, the old tmux is gone,
  `agw session restart` is cleanly retryable, and the error says exactly which step failed. No
  resurrection is attempted.
- **Getting this API surface right is a big part of the HLA**: what the core hands the harness (the
  execution channel to the target; the model's identity chain -- vm, workspace, agent-or-admin, and
  session NAMES, with full representations as reserved room added when a harness first needs one;
  the config blob) and what the harness returns to the tmux layer are pinned there, not here. The
  requirement is the capability boundary, not its signature.
- **Best-effort robustness, not race-proof.** Harnesses should aim to be as robust as practical, but
  there is no expectation that they are perfect against race conditions and similar challenges. The
  canonical example is R4's existence check: the tool session changing state between the check and
  the launch may be problematic, and that is understood and accepted. Where an easy strengthening
  exists (e.g. moving the check into the launch script/command so check and launch are one
  invocation), take it; it is not worth overengineering beyond that -- these are
  operator-interactive sessions, and a retry is an acceptable outcome for a window this small.
- **The role of tmux is unchanged by this effort.** Every session, regardless of harness, is
  launched by tmux and has its tty owned by tmux. Even a harness whose process has zero need for
  stdin/stdout/stderr still runs under tmux -- it is how Agentworks manages the harness process
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
  `harness_config` form (the default harness -- the docs commit to runtime neutrality; maintainer
  ruling, 2026-07-08), followed by the `claude-code` document as the worked capability example (one
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
  `docs/adrs/` -- receiving its number then -- only at the very end of the effort, so the number is
  assigned once against the directory as it exists at merge time. ADR 0016 is untouched; the new ADR
  references it for the capability model it builds on.
- Per the SDD lifecycle rules, each doc change rides the commits that make its claims true; nothing
  here waits for a closeout pass, and nothing permanent cites this SDD's path.

## Non-goals

- **Plugin-registered harnesses**: the registry dict is where they will land; the loading,
  enablement, namespacing, and origin stamping are the plugin SDD's.
- **Artifact management (skills, hooks, MCP servers, allow/deny lists, ...)**: an expected future in
  which the core manages such artifacts generically and the harness translates them for its specific
  tooling (see the model-change section). Deferred -- it needs an operator-declared artifact model
  first (plugin SDD territory) -- but it is part of why the harness seam exists.
- **Liveness probing behind the harness**: today's boot-id/PID probe is tool-agnostic and stays
  core; a harness-owned probe can be added to the contract when a harness actually needs one.
- **Alternatives to tmux**: possibly introduced someday, but that would be an orthogonal effort --
  the harness concept neither depends on tmux specifics nor would change if the pane host did (R7's
  tmux invariant).
- **A Codex harness**: nothing blocks one, but no member ships until its conventions are pinned;
  `claude-code` proves the multi-member path.
- **Moving `claude_marketplaces` / `claude_plugins`**: agent-provision surface, untouched (R4).
- **A dedicated `harness` declarable kind**: deliberately absent per the capability collapse; if a
  harness someday needs multiple configured instances with identity beyond one reference site, the
  graduate-when-real clause applies.

## Migration notes

Operators upgrading across this SDD see:

- TOML session templates keep working unchanged. YAML manifests written against the unreleased PR
  #156 surface with flat command fields (if any exist) get a load error pointing at `harness_config`
  -- the `!` flag in R2 covers exactly this.
- `agw resource list` gains two `harness` rows; `agw resource kinds` gains the `harness` kind.
- Newly migrated or sampled session templates spell commands under `harness_config`.
- Templates can now say `harness: claude-code` instead of restating the Claude Code command strings.
