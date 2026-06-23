# Resource registry: plan

**Status:** Draft **Repo:** `agentworks`

The plan is phased. Each phase ends at a green CI and a usable intermediate state. Order matches the
HLA's phasing section; refer to FRD / HLA for the design.

Reviewer pass with the `agentworks-reviewer` agent runs after each phase. The bar is "this is
perfect" (per the user's standing direction); iterate until findings are addressed before moving to
the next phase.

## Phase 0: Rename `Config` -> `Registry`; add `tomlkit`

Goal: prepare the codebase for the framework. Pure refactor / dependency add; no behavior change.

- [ ] **Rename**: `agentworks.config.Config` -> `agentworks.registry.Registry`;
      `agentworks/config.py` -> `agentworks/registry.py`; `load_config()` -> `load_registry()`.
      Mechanical refactor via VSCode language server (or `ruff`-aware AST rename). Every import,
      every `Config` typed parameter, every test reference, every docstring mention.
- [ ] Nested sub-types keep their `*Config` names: `AdminConfig`, `SecretConfig`,
      `SecretBackendConfig`, etc. Only the top-level container renames. TOML paths
      (`[admin.config]`, `[secret_config]`) are unchanged operator-facing surface.
- [ ] `cli/pyproject.toml`: add `tomlkit` dependency. The loader switches to it for line-number
      capture (per HLA "tomlkit for line tracking"). Existing `tomllib` calls migrate mechanically;
      the parsed-types surface that downstream code consumes doesn't change shape.
- [ ] `agentworks/registry.py` (formerly `config.py`): the parse step now captures
      `(file: Path, line: int)` for each top-level resource section's opening line, threaded into
      the parsed-types so the validation pass can populate `Origin`. Phase 0 just captures the data;
      Phase 1a uses it.
- [ ] **Tests**: existing test suite stays green. Add `cli/tests/test_registry_line_capture.py`
      pinning that every operator-declared resource carries a `(file, line)` tuple after load,
      across all kinds today (`secrets`, `vm_templates`, `agent_templates`, `workspace_templates`,
      `session_templates`, `git_credentials`).

Definition of done: `from agentworks.registry import Registry, load_registry` works; the parsed
types carry `(file, line)` on every operator-declared section; `tomlkit` is on `cli/pyproject.toml`;
CI green; reviewer-approved.

## Phase 1a: Framework foundations

Goal: stand up the `agentworks.resources` package with the core types, the `_compose_resources`
loader step (with orphan-rejection), the validation pass (walk requirements, dispatch miss policies,
attach metadata, detect cycles), and `SecretKind`. No consumers wired yet.

- [ ] `cli/agentworks/resources/__init__.py`: public surface re-exports (`ResourceRequirement`,
      `SecretRequirement`, `ResourceKind`, `Origin`, `UsageEntry`, `collect_secrets_for`).
- [ ] `cli/agentworks/resources/requirement.py`:
  - `ResourceRequirement` immutable dataclass (base): `name`, `kind`, `usage`, `source`.
  - `SecretRequirement(ResourceRequirement)` concrete subclass (no extra fields in Phase 1; the
    subclass exists so producers and the framework agree on the target kind without
    string-dispatch).
  - `UsageEntry(source, text)` immutable dataclass for per-resource usage list entries.
- [ ] `cli/agentworks/resources/origin.py`:
  - `Origin` dataclass with `variant: Literal["operator-declared", "auto-declared"]` and
    variant-specific fields (`file`, `line` for operator-declared; `source: tuple[str, str]` for
    auto-declared). Set once at registration; never mutated.
- [ ] `cli/agentworks/resources/kind.py`:
  - `ResourceKind` Protocol: `kind`, `miss_policy`, `auto_declare_names`,
    `synthesize(requirements) -> Resource`.
  - `KIND_REGISTRY: dict[str, ResourceKind]` populated at import time.
- [ ] `cli/agentworks/resources/kinds/__init__.py` + `kinds/secret.py`:
  - `SecretKind(ResourceKind)`: `kind="secret"`, `miss_policy="auto-declare"`,
    `auto_declare_names=None` (any name). `synthesize` builds a `SecretDecl` with framework-set
    `usage` list (paired `UsageEntry`s) and `origin=auto-declared`.
- [ ] `cli/agentworks/resources/registry.py`:
  - `_compose_resources(parsed, registry)`: composes parsed TOML sections into resources; enforces
    orphan-rejection (R2) -- sub-sections without explicit parent raise `ConfigError` with a clear
    "declare the parent or move the content" message. Singletons (`admin`, `secret_config`) are
    exempt.
  - `_run_validation_pass(registry)`: collects requirements via `required_resources()` across all
    resources, groups by `(kind, name)`, dispatches per `KIND_REGISTRY` miss policy, populates
    registry, attaches framework metadata (`origin` + `usage` list) to operator-declared resources,
    then runs cycle detection (DFS three-coloring).
  - Both functions called from `registry.load_registry()`: parse -> compose -> validate -> return.
- [ ] `cli/agentworks/secrets/base.py`: `SecretDecl` gains `origin: Origin` and
      `usage: list[UsageEntry]` fields. Frozen dataclass updated; defaults preserve backward-compat
      for any existing construction sites that don't set them (operator-declared parsed in Phase 0;
      auto-declared synthesized in Phase 1a).
- [ ] `cli/agentworks/errors.py`: confirm `ConfigError` carries `entity_kind`, `entity_name`,
      `source` (a `(kind, name)` pair) fields if not already; existing shape is preserved.
- [ ] **Tests**:
  - `cli/tests/resources/test_requirement.py`: dataclass invariants, base-vs-subclass shape.
  - `cli/tests/resources/test_origin.py`: variant invariants, immutability.
  - `cli/tests/resources/test_compose.py`: orphan-rejection across kinds; singleton exceptions for
    admin and secret_config; additive sub-section composition (`[vm_templates.x]` +
    `[vm_templates.x.env]` produces a single resource).
  - `cli/tests/resources/test_validation_pass.py`: walk + dispatch; auto-declare on miss for
    secrets; reserved-name restriction (Phase 1 stub for templates -- the dispatch behaves correctly
    even though no Phase 2 producers yet); error miss policy; first-matching origin rule; per-key +
    walk-order determinism.
  - `cli/tests/resources/test_cycle_detection.py`: synthetic cycle producer for testing; clear error
    reporting.
  - `cli/tests/resources/test_kind_registry.py`: `KIND_REGISTRY["secret"]` lookup,
    `SecretKind.synthesize` shape.

Definition of done:
`from agentworks.resources import ResourceRequirement, ResourceKind, Origin, collect_secrets_for`
works; the validation pass runs on every config load (no-op for current configs since no producers
wired yet); orphan-rejection fires on synthetic test configs; CI green; reviewer-approved.

## Phase 1b: Env-block secret-reference migration

Goal: env-block `{ secret = "..." }` references emit `SecretRequirement` via `required_resources()`.
Undeclared secrets auto-declare (per the new framework). The env-and-secrets SDD's strict "must
declare" error path is removed; visibility is preserved via `agw doctor` and `agw secret list`
showing every auto-declared secret with its origin source.

- [ ] `cli/agentworks/env/entry.py`: `EnvEntry`'s secret-ref form gains
      `required_resources(source: tuple[str, str]) -> list[ResourceRequirement]`. Returns one
      `SecretRequirement` per referenced secret. Usage text derived from the env-block context
      (e.g., `"the ANTHROPIC_API_KEY env var"`).
- [ ] `cli/agentworks/registry.py`: env-block resources (admin, vm_template, workspace_template,
      agent_template, session_template -- each of which has an env block) implement
      `required_resources()` by iterating their `env` dict and aggregating per-entry requirements.
- [ ] Remove the env-and-secrets validation that errored on undeclared env-block secret refs
      (`render` raising `ConfigError` for unknown-secret refs in the env-block path).
- [ ] **Tests**:
  - `cli/tests/test_env_block_requirements.py`: an env block referencing an undeclared secret no
    longer errors; the secret auto-declares; doctor / secret-list display surfaces it with origin =
    `auto-declared by <scope>:<name>`.
  - Update `cli/tests/test_secrets_resolver.py` (or equivalent) to reflect the new "no config-load
    error for undeclared refs" behavior.

Definition of done: env-block secret references go through the framework end-to-end; existing
env-and-secrets tests pass with the new auto-decl behavior; doctor reflects auto-declared secrets;
CI green; reviewer-approved.

## Phase 1c: Tailscale auth key migration

Goal: VM template gains `tailscale_auth_key`; `vm create` / `vm reinit` walk the VM-template
subgraph, eager-resolve the auth-key secret, thread it as a kwarg to the Tailscale install runner.
Legacy resolution path removed.

- [ ] `cli/agentworks/registry.py` (VMTemplate parsed type): add
      `tailscale_auth_key: str = "tailscale-auth-key"` field. Validate: must be a bare string (no
      `{ secret = "..." }` polymorphism, no plaintext-literal heuristic). Sample config updated.
- [ ] `cli/agentworks/vms/templates.py` (`ResolvedVMTemplate`): same field; threaded through
      template-inheritance resolution like other fields.
- [ ] `ResolvedVMTemplate.required_resources()` emits a `SecretRequirement` for the configured
      `tailscale_auth_key` (default `"tailscale-auth-key"`) with usage
      `"the VM-provisioning auth key"` and source `("vm_template", template.name)`.
- [ ] `cli/agentworks/vms/manager.py`: `create_vm` and `reinit_vm` walk the resolved VM template's
      requirement subgraph via `collect_secrets_for(registry, ("vm_template",     <name>))`,
      eager-resolve the result via `resolve_for_command(extra_decls=...)`, and pass the resolved
      Tailscale auth key as a function argument into the Tailscale install runner.
- [ ] `cli/agentworks/vms/initializer.py` (or wherever the Tailscale-install code lives after PR
      #130's polymorphic-transports refactor): the function that runs
      `tailscale up     --authkey=...` gains a `*, auth_key: str` keyword-only argument. No `env=`
      injection. No profile-fragment writes for the auth key.
- [ ] Remove the legacy `AW_TAILSCALE_AUTH_KEY` env-var-or-prompt resolution path. The framework's
      resolver is the only source of the auth key.
- [ ] Update `cli/agentworks/sample-config.toml`: VM template stanza documents `tailscale_auth_key`
      and the default secret name; remove any legacy `AW_TAILSCALE_AUTH_KEY` mention.
- [ ] **Tests**:
  - `cli/tests/test_vm_create_tailscale_eager_resolve.py`: `vm create` resolves the Tailscale secret
    BEFORE any state mutation; the install runner receives the value as a kwarg; no `env=`
    injection.
  - `cli/tests/test_tailscale_legacy_removed.py`: source-level tripwire that `AW_TAILSCALE_AUTH_KEY`
    is no longer referenced.
  - `cli/tests/test_resolved_vm_template_requirements.py`: a resolved VM template emits the expected
    `SecretRequirement` shape.

Definition of done: `vm create` / `vm reinit` prompt for the Tailscale auth key via the framework
(or pick it up from the configured backend chain) at command start; legacy resolution path removed;
existing env-block tests still pass; CI green; reviewer-approved.

## Phase 1d: Git credential token migration

Goal: each `[git_credentials.<name>]` entry gains a `token` field referencing a secret; admin /
agent provisioning walks the requirement subgraph; tokens reach the install runner as kwargs. Legacy
`obtain_token` path removed.

- [ ] `cli/agentworks/registry.py` (`GitCredentialEntry`): add `token: str = f"git-token-{name}"`
      field. Validate: bare string. Same shape rule as `tailscale_auth_key`.
- [ ] `GitCredentialEntry.required_resources()` emits a `SecretRequirement` for the configured
      `token` (default `git-token-<name>`) with usage `"the auth token"` and source
      `("git_credentials", name)`.
- [ ] `AdminConfig.required_resources()` / `AgentTemplate.required_resources()`: when
      `git_credentials = ["name1", "name2"]` is set, emit `GitCredentialRequirement`-shaped entries
      per name. (Phase 2b folds these into a `GitCredentialEntry` kind in the framework with the
      error miss policy; Phase 1d emits the requirements so the transitive walk works even with
      bespoke validation still active.)
- [ ] `cli/agentworks/agents/manager.py` / `cli/agentworks/vms/manager.py`: at `agent create` /
      `agent reinit` / `vm create` / `vm reinit`, walk the requirement subgraph transitively
      (admin/agent_template -> git_credentials -> secret) and eager-resolve.
- [ ] The git-credentials install runner gains a `*, tokens: dict[str, str]` (name -> token)
      keyword-only argument; the function that writes `~/.git-credentials` reads from the passed
      dict, not from `obtain_token`.
- [ ] Remove `agentworks.git_credentials.base.obtain_token` and any related env-var / prompt
      resolution code. Provider classes (`github.py`, `azdo.py`) keep their
      `credential_lines(token=...)` formatting methods.
- [ ] Update `cli/agentworks/sample-config.toml`: `[git_credentials.<name>]` stanzas document the
      new `token` field and the default secret-name convention.
- [ ] **Tests**:
  - `cli/tests/test_git_credentials_token_resolve.py`: token resolves via the framework's backend
    chain; `~/.git-credentials` write receives the resolved value; no `AW_GIT_CREDENTIALS_*` lookup.
  - `cli/tests/test_obtain_token_removed.py`: source-level tripwire that `obtain_token` is no longer
    defined or called.
  - `cli/tests/test_git_credentials_subgraph_walk.py`: requirement walk traverses admin ->
    git_credentials -> secret transitively.

Definition of done: git credential tokens flow through the framework end-to-end at provisioning
time; legacy path removed; provider formatting unchanged; CI green; reviewer-approved.

## Phase 1e: `agw secret describe`

Goal: add the per-secret detail view to the existing `cli/agentworks/cli/commands/secret.py`.
Service-layer logic lives in `agentworks.secrets`.

- [ ] `cli/agentworks/secrets/inspect.py`: add `describe_secret(registry, name)` that returns a
      structured `SecretDescription` (name, kind, origin with full detail, usage list with per-entry
      source + text, per-backend mapping status, resolution preview). No prompting, no value
      resolution.
- [ ] `cli/agentworks/cli/commands/secret.py`: add the `describe` typer subcommand. Calls the
      service-layer function; renders the structured result using the existing table / panel
      helpers.
- [ ] Renderer covers all four output sections (header, usages, backend mappings, resolution
      preview). Per FRD R10.
- [ ] Update `cli/agentworks/completions/`: the shell-completion tree picks up the new subcommand
      automatically (or via the project's regen step).
- [ ] **Tests**:
  - `cli/tests/test_secret_describe.py`: per-section rendering; operator-declared shows file:line;
    auto-declared shows the first requirement source; multiple usages render one row each; backend
    status shows the per-backend disposition without merging.
  - `cli/tests/test_secret_describe_no_prompt.py`: describe never calls the prompt source and never
    resolves a value.

Definition of done: `agw secret describe <name>` works for both operator-declared and auto-declared
secrets; output matches FRD R10; CI green; reviewer-approved.

**Phase 1 ships at this point.** PR sequence on the `feat/resource-registry-sdd` branch covers
Phases 0-1e. Lockfile authored after Phase 1e's reviewer pass.

## Phase 2a: Template kinds

Goal: bring template inheritance into the framework. Define `VMTemplateKind`,
`WorkspaceTemplateKind`, `AgentTemplateKind`, `SessionTemplateKind`. `inherits = [...]` becomes
`TemplateRequirement` emission. Built-in defaults migrate into `synthesize()`. Operator-facing
behavior is unchanged.

- [ ] `cli/agentworks/resources/requirement.py`: add `TemplateRequirement(ResourceRequirement)` with
      no extra fields (the template kind handles its own defaults inside `synthesize`).
- [ ] `cli/agentworks/resources/kinds/vm_template.py`, `workspace_template.py`, `agent_template.py`,
      `session_template.py`:
  - Each `XxxKind` declares `miss_policy="auto-declare"`, `auto_declare_names={"default"}`.
  - `synthesize(requirements)` produces the kind's code-defined default template (the same defaults
    today live in the existing resolver's "implicit default" fallback; move them here verbatim in
    Phase 2a).
- [ ] Each `XxxTemplate.required_resources()` emits `TemplateRequirement` entries for each name in
      `inherits = [...]`.
- [ ] The existing inheritance resolver in `agentworks.config` / template modules shrinks:
      requirement-emission now happens via `required_resources()`; the framework's cycle-detection
      runs uniformly; the resolver walks `inherits` with the framework having already validated
      names and reachability. Per-template field-merging logic stays where it is (it's
      template-inheritance behavior, not framework behavior).
- [ ] Sweep existing template tests for any that asserted on the bespoke validation's error
      messages; update to match the framework's consistent error shape.
- [ ] **Tests**:
  - `cli/tests/resources/test_template_kinds.py`: each kind's `synthesize` produces the expected
    defaults; reserved-name restriction errors on non-default missing names.
  - `cli/tests/resources/test_template_cycle_detection.py`: inheritance cycles caught uniformly;
    error messages match the framework's shape.
  - Update `cli/tests/test_vm_templates.py` / `test_agent_templates.py` / etc. to reflect the new
    error shape.

Definition of done: template inheritance flows through the framework; built-in defaults are
single-sourced (in `synthesize` per kind); cycle detection covers all four template kinds;
operator-facing behavior unchanged except for error message shape; CI green; reviewer-approved.

## Phase 2b: Catalog command, git credential provider, and backend kinds

Goal: bring the remaining bespoke-validation kinds into the framework. All use the error miss
policy.

- [ ] `cli/agentworks/resources/kinds/catalog.py`: `CatalogKind` for catalog commands
      (`apt_packages`, `system_install_commands`, `user_install_commands`). Error miss policy.
- [ ] `cli/agentworks/resources/kinds/git_credential_provider.py`: `GitCredentialProviderKind` for
      the `type` field on `[git_credentials.<name>]`. Error miss policy. (The provider
      implementations in `agentworks.git_credentials/` stay; the kind validates the `type` field
      against the known set.)
- [ ] `cli/agentworks/resources/kinds/secret_backend.py`: `SecretBackendKind` for
      `[secret_backends.<kind>]` and `secret_config.backends` references. Error miss policy.
- [ ] Existing bespoke validation removed in favor of framework dispatch. Error messages get the
      framework's consistent shape.
- [ ] **Tests**:
  - `cli/tests/resources/test_catalog_kind.py`: unknown catalog command name errors with requirement
    source; known names resolve.
  - `cli/tests/resources/test_git_credential_provider_kind.py`: unknown `type` value errors;
    `"github"` and `"azdo"` resolve.
  - `cli/tests/resources/test_secret_backend_kind.py`: unknown backend kind errors; declared kinds
    resolve.

Definition of done: every kind in the registry uses framework dispatch; bespoke validation removed
from the loader; CI green; reviewer-approved.

## Phase 2c: `agw resource list` / `agw resource describe`

Goal: add the cross-kind inspection commands.

- [ ] `cli/agentworks/resources/inspect.py`: add
      `list_resources(registry, kinds=None, origin=None) -> list[ResourceSummary]` and
      `describe_resource(registry, kind, name) -> ResourceDescription`.
- [ ] `cli/agentworks/cli/commands/resource.py` (new): typer command group with `list` and
      `describe` subcommands. Two-positional `describe <kind> <name>` (FRD R12 rationale).
- [ ] Renderers cover columns per HLA: list shows kind, name, origin, usage count, description;
      describe shows kind, name, full origin detail, full usage list, description (no kind-specific
      detail -- that belongs in `agw secret describe` et al.).
- [ ] Update `cli/agentworks/completions/`.
- [ ] **Tests**:
  - `cli/tests/test_resource_list.py`: kind filter (CSV), origin filter, header summary.
  - `cli/tests/test_resource_describe.py`: per-section rendering for each kind; two-positional
    parsing; useful error message when `<kind>` is unknown.

Definition of done: `agw resource list` and `agw resource describe <kind> <name>` work across all
kinds; CI green; reviewer-approved.

**Phase 2 ships at this point.** PR sequence on `feat/resource-registry-phase-2` (separate branch /
PR from Phase 1). Lockfile updated after Phase 2c's reviewer pass.

## Sequencing notes

- **Phase order**: 0 -> 1a -> 1b -> 1c -> 1d -> 1e -> ship Phase 1 -> 2a -> 2b -> 2c -> ship
  Phase 2. Each phase ends at a green CI and a usable intermediate state.
- **Why env-block migration before system secrets**: Phase 1b gives the framework a real producer of
  `SecretRequirement` exercised end-to-end before Phase 1c / 1d wire in the system-secret producers.
  Bugs in the validation pass surface against the larger surface area first.
- **Why Phase 2 in a separate PR**: Phase 2 is primarily a refactor with no operator-facing config
  changes; bundling it with Phase 1 would inflate the diff without adding feature value. Splitting
  also lets Phase 1 reviewer feedback iterate without holding up the refactor work.
- **Interaction with PR #130 (polymorphic transports)**: PR #130 renamed `ExecTarget` to `Transport`
  and introduced the `transports/` package. Phase 1c / 1d wire kwargs into the Transport-shaped
  install runners. No conflict with this SDD's design; the function signatures are the integration
  point.
- **Reviewer cadence**: `agentworks-reviewer` agent runs after each phase. Aim for "this is
  perfect"; iterate until findings are addressed before moving to the next phase. Capture per-phase
  findings as commits with descriptive messages so the lockfile can summarize the iteration trail.
- **Lockfile**: written after Phase 1e (covers Phase 1 ship) and updated after Phase 2c (covers
  Phase 2 ship). Each lockfile records the framework shape as shipped, deferred items, ADRs (none
  anticipated for this SDD).
- **Out-of-scope reminders**: no plugin source (future SDD); no DB-backed resources (future manifest
  SDD); no namespaces; no per-source miss policies; no per-field merge.

## Open items for the LLD

The plan above stays at HLA fidelity; the LLD (or commit-by-commit notes during Phase 1a) should
pin:

- The exact `tomlkit` API surface used for `(file, line)` capture (token positions vs. document
  walk).
- Per-kind error message templates (string format) for the framework's `ConfigError`.
- The exact subclass hierarchy of `ResourceRequirement` (frozen dataclasses with `kw_only`? what
  about hashability when used as dict keys?).
- The `UsageEntry` serialization shape for `agw secret describe` rendering.
- Whether `KIND_REGISTRY` is mutable at import (for Phase 2 kinds to register) or built once with a
  manifest.
