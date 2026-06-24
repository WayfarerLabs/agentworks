# Resource registry: plan

**Status:** Draft **Repo:** `agentworks`

The plan is phased. Each phase ends at a green CI and a usable intermediate state. Order matches the
HLA's phasing section; refer to FRD / HLA for the design.

Reviewer pass with the `agentworks-reviewer` agent runs after each phase. The bar is "this is
perfect" (per the user's standing direction); iterate until findings are addressed before moving to
the next phase.

## Phase 0: `tomlkit` + Resource composition + orphan rejection + `SourceLocation` at Config

Goal: prepare the parser layer for the framework. No rename. `Config` stays in
`agentworks/config.py`; per the HLA's "Two layers", Config is the Resource-producing layer. Phase 0
makes that responsibility explicit by switching the parser to `tomlkit`, capturing file/line,
composing Resources from top-level + sub-section pairs, enforcing orphan rejection, and attaching
Config's own `SourceLocation(file, line)` to each composed Resource. The framework's `Origin` type
(with two variants) is a Registry-layer concept; the Registry translates Config's `SourceLocation`
into `Origin(variant="operator-declared", ...)` when Resources are published into it. Config does
not depend on `agentworks.resources`.

- [ ] `cli/pyproject.toml`: add `tomlkit` dependency (latest stable).
- [ ] `agentworks/config.py`: switch the parse step from `tomllib` to `tomlkit`. Existing
      `load_config()` and `Config` keep their signatures; only the internal parser changes.
- [ ] `agentworks/config.py`: add a Config-layer `SourceLocation` dataclass (frozen, two fields:
      `file: Path`, `line: int`). Lives in `config.py` (or a sibling module if file size warrants).
      This is Config's own representation of where a Resource was declared; the framework's `Origin`
      is constructed from it later, when the Resource is published into a Registry.
- [ ] `agentworks/config.py`: capture `(file: Path, line: int)` for each top-level resource
      section's opening line and store it as a `SourceLocation` instance.
- [ ] `agentworks/config.py`: **enforce orphan rejection** (FRD R2). After parse, walk the `tomlkit`
      document and detect sub-sections like `[vm_templates.x.env]` whose parent `[vm_templates.x]`
      is not explicitly declared. Raise `ConfigError` pointing at the orphan with a "declare the
      parent or move the content" message. Singletons (`admin`, `secret_config`) are exempt.
- [ ] `agentworks/config.py`: add `declared_at: SourceLocation` field to each Resource type
      (`VMTemplate`, `WorkspaceTemplate`, `AgentTemplate`, `SessionTemplate`, `AdminConfig`,
      `SecretConfig`, `SecretBackendConfig`, `GitCredentialConfig`, `SecretDecl`). Composition sets
      `declared_at` from the section's `SourceLocation` at construction time.
- [ ] **Tests**: existing suite stays green. Add `cli/tests/test_config_line_capture.py` pinning
      that every operator-declared Resource carries a `declared_at: SourceLocation` after load, with
      the right file path and line, across all kinds today (`secrets`, `vm_templates`,
      `agent_templates`, `workspace_templates`, `session_templates`, `git_credentials`, admin,
      secret_config, secret_backends).
- [ ] **Tests**: add `cli/tests/test_config_orphan_rejection.py` covering: orphan
      `[vm_templates.x.env]` errors; admin singleton sub-tables (`[admin.env]`,
      `[admin.git_credentials]`) accepted without a root `[admin]`; secret_config singleton accepted
      without explicit `[secret_config]` parent if anyone writes `[secret_config.backends]`
      directly.
- [ ] **Tests**: add a comment-roundtrip / sample-config-parse test confirming the `tomlkit`
      migration doesn't regress operator-facing behavior on the existing sample-config.

Definition of done: `from agentworks.config import Config, load_config, SourceLocation` works; every
operator-declared Resource carries `declared_at: SourceLocation(file=..., line=...)`; orphan
sub-sections error at config-load; `tomlkit` is on `cli/pyproject.toml`; `agentworks.config` has no
import of `agentworks.resources`; CI green; reviewer-approved.

## Phase 1a: Framework foundations

Goal: stand up the `agentworks.resources` package with the core types and the new `Registry` class.
The Registry exposes a **publish / validate** API: starts empty, accepts Resources from any source
(Config in Phase 1; future plugins / manifests later), then `validate()` runs the framework pass
(walk requirements, dispatch miss policies, attach `usage`, detect cycles).
`Registry.from_config(config)` wraps the common Config-only path. Plus `SecretKind`. Resource
composition and orphan rejection already live at the Config layer per Phase 0; the Registry operates
on already-composed Resources. No producers of `SecretRequirement` wired yet beyond what tests
synthesize.

- [ ] `cli/agentworks/resources/__init__.py`: public surface re-exports (`ResourceRequirement`,
      `SecretRequirement`, `ResourceKind`, `Origin`, `UsageEntry`, `Registry`,
      `collect_secrets_for`). `Registry.from_config` is the convenience entry point; the
      `publish_to` / `validate` split is reachable via `Registry.empty()` + method calls.
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
  - `KIND_REGISTRY: dict[str, ResourceKind]`. Each `kinds/*.py` module self-registers into the dict
    at import; `kinds/__init__.py` imports all kinds. `agentworks/resources/__init__.py` imports
    `.kinds` so that a single `import agentworks.resources` (or any
    `from agentworks.resources import ...`) populates the registry. Phase 2 kinds slot in by adding
    new files under `kinds/` and importing them from `kinds/__init__.py`; no central manifest to
    edit.
- [ ] `cli/agentworks/resources/kinds/__init__.py` + `kinds/secret.py`:
  - `SecretKind(ResourceKind)`: `kind="secret"`, `miss_policy="auto-declare"`,
    `auto_declare_names=None` (any name). `synthesize` builds a `SecretDecl` with framework-set
    `usage` list (paired `UsageEntry`s) and `origin=auto-declared`.
- [ ] `cli/agentworks/resources/registry.py`:
  - `Registry` class: per-kind dicts of Resources (`secrets`, `vm_templates`, `agent_templates`,
    ...). Mutable during the publish phase; frozen after `validate()`. Lookup helpers
    (`lookup(kind, name)`, `iter_kind(kind)`) available once finalized.
  - `Registry.empty() -> Registry` -- class method returning a fresh empty Registry.
  - `Registry.add(kind, name, resource, *, declared_at: SourceLocation) -> None` -- the publish-
    side API. Translates Config's `declared_at` into `Origin(variant="operator-declared", ...)` and
    attaches it to the Resource at this point. Config's `publish_to(registry)` is a thin wrapper
    that iterates Config's resources and calls `registry.add(...)` for each. Other sources (future
    plugins / manifests) call `add` directly.
  - `Registry.validate() -> None` -- the framework pass. Walks requirements via
    `required_resources()`, groups by `(kind, name)`, dispatches per `KIND_REGISTRY` miss policy
    (auto-declare synthesizes a Resource with `Origin(variant="auto-declared", ...)`; error raises
    `ConfigError`), attaches `usage` to each Resource, runs cycle detection (DFS three-coloring),
    and freezes the Registry. Does NOT compose Resources -- that's Config's responsibility.
  - `Registry.from_config(config: Config) -> Registry` (classmethod): the convenience entry point.
    Equivalent to `r = cls.empty(); config.publish_to(r); r.validate(); return r`.
- [ ] **Add `origin: Origin | None` and `usage: list[UsageEntry]` fields to every Resource type**:
      `SecretDecl` in `agentworks/secrets/base.py`; `VMTemplate`, `WorkspaceTemplate`,
      `AgentTemplate`, `SessionTemplate`, `AdminConfig`, `SecretConfig`, `SecretBackendConfig`,
      `GitCredentialConfig` in `agentworks/config.py`. Both default to `None` / empty list at
      construction; the Registry populates them (origin at publish via `Registry.add` calling
      `with_origin(...)`; usage during validate via `with_usage(...)`). The Config-layer
      `declared_at: SourceLocation` (Phase 0) stays as Config's representation; the Registry-layer
      `origin: Origin` is the framework's representation. Both exist on the same Resource instance
      after publish. Resource types gain `with_origin` / `with_usage` copy methods (or
      `@dataclass(frozen=True)` + `dataclasses.replace` shapes; LLD picks).
- [ ] `cli/agentworks/config.py`: add `Config.publish_to(self, registry: Registry) -> None`.
      Iterates Config's per-kind dicts and calls
      `registry.add(kind, name, resource, declared_at=resource.declared_at)` for each. This is the
      only point at which `agentworks.config` imports from `agentworks.resources` (specifically
      `Registry` for type hinting). The import is allowed because `publish_to` is the explicit
      handoff between layers; the Config layer's data structures and parsing remain
      framework-ignorant.
- [ ] `cli/agentworks/errors.py`: confirm `ConfigError` carries `entity_kind`, `entity_name`,
      `source` (a `(kind, name)` pair) fields if not already; existing shape is preserved.
- [ ] **Tests**:
  - `cli/tests/resources/test_requirement.py`: dataclass invariants, base-vs-subclass shape.
  - `cli/tests/resources/test_origin.py`: variant invariants, immutability.
  - `cli/tests/resources/test_validation_pass.py`: walk + dispatch in `Registry.validate()`;
    auto-declare on miss for secrets; reserved-name restriction (Phase 1 stub for templates -- the
    dispatch behaves correctly even though no Phase 2 producers yet); error miss policy;
    first-matching origin rule; per-key + walk-order determinism. Includes a synthetic-publisher
    test that an operator-declared `SecretDecl` gets its `usage` list populated after publish +
    validate (the `with_usage` attachment path). Composition + orphan rejection are tested at the
    Config layer (Phase 0 tests); `Registry.validate()` operates on already-composed Resources.
  - `cli/tests/resources/test_registry_lifecycle.py`: empty Registry -> publish via `add` ->
    `validate` -> queryable; double-`validate` errors; `add` after `validate` errors;
    `Registry.from_config(config)` convenience equivalent to manual steps.
  - `cli/tests/resources/test_cycle_detection.py`: synthetic cycle producer for testing; clear error
    reporting.
  - `cli/tests/resources/test_kind_registry.py`: `KIND_REGISTRY["secret"]` lookup,
    `SecretKind.synthesize` shape.

Definition of done: the public surface (`ResourceRequirement`, `ResourceKind`, `Origin`, `Registry`,
`UsageEntry`, `collect_secrets_for`, etc.) is importable from `agentworks.resources`;
`Registry.from_config(config)` runs on every config load (no-op for current configs since no
producers wired yet); `publish_to` / `validate` lifecycle pinned by tests; CI green;
reviewer-approved.

## Phase 1b: Env-block secret-reference migration

Goal: env-block `{ secret = "..." }` references emit `SecretRequirement` via `required_resources()`.
Undeclared secrets auto-declare (per the new framework). The env-and-secrets SDD's strict "must
declare" error path is removed; visibility is preserved via `agw doctor` and `agw secret list`
showing every auto-declared secret with its origin source.

- [ ] `cli/agentworks/env/entry.py`: `EnvEntry`'s secret-ref form gains
      `required_resources(source: tuple[str, str]) -> list[ResourceRequirement]`. Returns one
      `SecretRequirement` per referenced secret. Usage text derived from the env-block context
      (e.g., `"the ANTHROPIC_API_KEY env var"`).
- [ ] `cli/agentworks/config.py`: env-block resources (admin, vm_template, workspace_template,
      agent_template, session_template -- each of which has an env block) implement
      `required_resources()` by iterating their `env` dict and aggregating per-entry requirements.
- [ ] Remove the env-and-secrets validation that errored on undeclared env-block secret refs
      (`render` raising `ConfigError` for unknown-secret refs in the env-block path).
- [ ] **Release-notes line**: this shifts the failure mode for env-block typos. Previously a typo in
      `{ secret = "anthropic-api-ky" }` errored at config load; now it auto-declares
      `anthropic-api-ky`, which surfaces at runtime as "no backend resolved the secret" (with
      `agw doctor` and `agw secret list` showing the unexpected auto-declared name). Intentional per
      FRD Migration notes, but operators upgrading should know to scan `agw secret list` for
      unexpected auto-declared names after the upgrade.
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

- [ ] `cli/agentworks/config.py` (VMTemplate parsed type): add
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
- [ ] Tailscale install runner gains a `*, auth_key: str` keyword-only argument. The exact function
      path is **pinned at Phase 1c start** by grepping for the current `tailscale up     --authkey`
      call site: pre-PR #130 this was `_install_tailscale` in `cli/agentworks/vms/initializer.py`;
      post-PR #130 it may have moved into the transports package or stayed in `vms/initializer.py`
      (mechanical move only). The kwarg-threading shape is the same either way. No `env=` injection.
      No profile-fragment writes for the auth key.
- [ ] Remove the legacy `AW_TAILSCALE_AUTH_KEY` env-var-or-prompt resolution path. The framework's
      resolver is the only source of the auth key.
- [ ] Update `cli/agentworks/sample-config.toml`: VM template stanza documents `tailscale_auth_key`
      and the default secret name; remove any legacy `AW_TAILSCALE_AUTH_KEY` mention.
- [ ] **Tests**:
  - `cli/tests/test_vm_create_tailscale_eager_resolve.py`: `vm create` resolves the Tailscale secret
    BEFORE any state mutation; the install runner receives the value as a kwarg; no `env=`
    injection.
  - `cli/tests/test_sample_config_tailscale.py`: the updated `sample-config.toml` parses cleanly
    through the new validation pass and the Tailscale secret auto-declares from the VM-template
    requirement.
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

- [ ] `cli/agentworks/config.py` (`GitCredentialConfig`): add a `token: str` field defaulting to
      `git-token-<name>`. The default is computed (in `__post_init__` or by the parser; a dataclass
      literal default can't interpolate the entry's name), not hard-coded. Validate: bare string.
      Same shape rule as `tailscale_auth_key`.
- [ ] `GitCredentialConfig.required_resources()` emits a `SecretRequirement` for the configured
      `token` with usage `"the auth token"` and source `("git_credentials", name)`.
- [ ] **Register `GitCredentialKind` in `KIND_REGISTRY`** with miss policy `error`. This lets the
      framework recognize `git_credentials:<name>` requirements emitted from admin / agent
      templates' `git_credentials = [...]` lists, look them up in the (already-populated) registry,
      and surface a clean error when the name is undeclared. The kind doesn't synthesize (no
      auto-decl); it just validates that the named credential exists.
- [ ] `AdminConfig.required_resources()` / `AgentTemplate.required_resources()`: when
      `git_credentials = ["name1", "name2"]` is set, emit a `ResourceRequirement` of kind
      `git_credentials` per name. `GitCredentialKind`'s error miss policy catches typos that
      previously went through bespoke validation.
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
  - `cli/tests/test_sample_config_git_credentials.py`: the updated `sample-config.toml` parses
    cleanly; `[git_credentials.<name>]` stanzas auto-declare their token secrets via the new
    framework.
  - `cli/tests/test_git_credentials_typo_errors.py`: a typo in
    `admin.git_credentials = ["githb-prod"]` (undeclared name) errors at config load via
    `GitCredentialKind`'s error miss policy, with the requirement source surfaced in the error
    message.
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

**Phase 1 ships at this point.** All six Phase-1 phases (0, 1a, 1b, 1c, 1d, 1e) land on
`feat/resource-registry-sdd` as **one PR per phase**, in order, each merged after its reviewer pass
returns "this is perfect". The branch stays open across the sequence; each phase's PR is either
rebased onto main after the prior phase merges, or stacked. The lockfile authored after Phase 1e's
reviewer pass covers the whole Phase 1 ship.

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

**Phase 2 ships at this point.** PR sequence on `feat/resource-registry-phase-2`, branched from
`main` **after Phase 1 merges** (not from the Phase 1 branch tip). Lockfile updated after Phase 2c's
reviewer pass.

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
- The exact shape of `with_origin` / `with_usage` on Resource types: shared mixin / base class with
  the framework-attached fields, or per-type `@dataclass(frozen=True)` + `dataclasses.replace`
  calls? Affects how invasive the Phase 1a edits to existing Resource types are.
