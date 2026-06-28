# Resource registry: plan

**Status:** Draft **Repo:** `agentworks`

The plan is phased. Each phase ends at a green CI and a usable intermediate state. Order matches the
HLA's phasing section; refer to FRD / HLA for the design.

Reviewer pass with the `agentworks-reviewer` agent runs after each phase. The bar is "this is
perfect" (per the user's standing direction); iterate until findings are addressed before moving to
the next phase.

## Phase 0: `SourceLocation` + section-line scanner at Config

Goal: prepare the parser layer for the framework. No rename. `Config` stays in
`agentworks/config.py`; per the HLA's "Two layers", Config is the Resource-producing layer. Phase 0
makes that responsibility explicit by introducing `SourceLocation`, adding a small regex section-
line scanner that runs alongside the existing `tomllib` parse, and attaching `declared_at` to each
composed Resource. The framework's `Origin` type is a Registry-layer concept; the Registry
translates Config's `SourceLocation` into `Origin(variant="operator-declared", ...)` when Resources
are published into it. Config does not depend on `agentworks.resources`.

TOML's implicit-parent semantics are accepted as-is per the revised FRD R2: writing
`[vm_templates.x.env]` without a separate `[vm_templates.x]` header produces a valid (if minimal)
`vm_templates.x` Resource. No orphan-rejection check.

- [x] `agentworks/source_location.py` (new): frozen `SourceLocation` dataclass with two fields:
      `file: Path`, `line: int`. Lives outside `config.py` because `config.py` is already past the
      1000-line soft target and types like `SecretDecl` in `secrets/base.py` need to import
      `SourceLocation` too (a definition in `config.py` would create a circular import via the
      existing `agentworks.config` -> `agentworks.secrets` edge). The module is intentionally tiny.
- [x] `agentworks/config.py`: add a regex section-line scanner
      (`_scan_section_lines(text: str) ->     dict[tuple[str, ...], int]`) that walks the raw TOML
      text matching `[section]` and `[section.sub]` header lines and returns a map from dotted
      section paths to opening-line numbers. Handles bare and quoted key segments per the TOML
      grammar; ignores commented lines and inline-table syntax. `[[array.of.tables]]` headers are
      tolerated (Phase 0 has no kind that uses them, but the scanner doesn't have to special-case).
- [x] `agentworks/config.py`: call the scanner once inside `load_config` (after reading the raw
      text, before `tomllib.load`). Pass the resulting `section_lines` map through the loader so
      every `_load_*` helper can look up its declared-line.
- [x] `agentworks/config.py`: add `declared_at: SourceLocation` field to `VMTemplate`,
      `WorkspaceTemplate`, `AgentTemplate`, `SessionTemplate`, `AdminConfig`, `NamedConsoleConfig`,
      `GitCredentialConfig`. Composition sets `declared_at` from
      `section_lines[(<container>,     <name>)]` at construction time, or -- when the operator
      declared only a sub-section like `[vm_templates.x.env]` -- the first matching
      `(<container>, <name>, <sub>)` entry by line. For singletons where the operator may omit the
      section entirely (`admin` with no `[admin.*]`; `named_console` with no `[named_console]`),
      Config synthesizes the empty-defaults instance with a sentinel
      `declared_at = SourceLocation(file=<config-path>,     line=0)` so the field is always
      populated.
- [x] `agentworks/secrets/base.py`: add `declared_at: SourceLocation` field to `SecretDecl`,
      `SecretBackendConfig`, `SecretConfig`. Imports from `agentworks.source_location` (no cycle).
- [x] **Tests**: existing suite stays green. Add `cli/tests/test_config_line_capture.py` pinning
      that every operator-declared Resource carries a `declared_at: SourceLocation` after load, with
      the right file path and line, across all kinds today (`secrets`, `vm_templates`,
      `agent_templates`, `workspace_templates`, `session_templates`, `git_credentials`, admin,
      named_console, secret_config, secret_backends). Also covers the singleton-omitted case:
      configs with no `[admin.*]` / no `[named_console]` still produce default instances with the
      sentinel `declared_at`. Covers the implicit-parent case: `[vm_templates.x.env]` alone produces
      `vm_templates.x` with `declared_at` pointing at the env header line.
- [x] **Tests**: add `cli/tests/test_config_section_line_scanner.py` covering the regex scanner
      directly: top-level sections, dotted sub-sections, quoted-segment paths, commented headers
      ignored, array-of-tables headers tolerated, file with no sections.

Definition of done: `from agentworks.config import Config, load_config` works unchanged for
operators; `from agentworks.source_location import SourceLocation` is importable; every
operator-declared Resource (and every singleton-synthesized default) carries a
`declared_at: SourceLocation(file=..., line=...)`; `agentworks.config` has no import of
`agentworks.resources`; CI green; reviewer-approved.

## Phase 1a: Framework foundations

Goal: stand up the `agentworks.resources` package with the core types and the new `Registry` class.
The Registry exposes a **publish / finalize** API: starts empty, accepts Resources from any source
(Config in Phase 1; future plugins / manifests later), then `finalize()` runs the framework pass
(walk requirements, dispatch miss policies that may synthesize auto-declared Resources, attach
`usage`, detect cycles, freeze). A small `build_registry(config)` free function in
`agentworks/bootstrap.py` orchestrates the standard publishers. Plus `SecretKind`. Resource
composition already lives at the Config layer per Phase 0; the Registry operates on already-composed
Resources. No producers of `SecretRequirement` wired yet beyond what tests synthesize.

- [x] `cli/agentworks/resources/__init__.py`: public surface re-exports (`ResourceRequirement`,
      `SecretRequirement`, `ResourceKind`, `Origin`, `UsageEntry`, `Registry`,
      `collect_secrets_for`). The lower-level `Registry.empty()` + `add` + `finalize` triad is
      exposed for tests and multi-source orchestration; the convenience `build_registry(config)`
      lives in `agentworks/bootstrap.py` (next bullet) so the Registry stays publisher-agnostic.
- [x] `cli/agentworks/bootstrap.py` (new): `build_registry(config: Config) -> Registry` free
      function that imports `Registry`, the catalog publisher (Phase 2b -- stubbed to no-op in Phase
      1a), and any future standard publishers, and orchestrates them in order. The module is the
      application-level glue that holds the "standard set of publishers" knowledge; this knowledge
      isn't Config's (Config shouldn't know about catalog) and isn't Registry's (Registry shouldn't
      know about its publishers). Call sites use this for the common case; tests can either use this
      helper or assemble Registry by hand with `Registry.empty()` + explicit `publish_to` calls.
- [x] `cli/agentworks/resources/requirement.py`:
  - `ResourceRequirement` immutable dataclass (base): `name`, `kind`, `usage`, `source`.
  - `SecretRequirement(ResourceRequirement)` concrete subclass (no extra fields in Phase 1; the
    subclass exists so producers and the framework agree on the target kind without
    string-dispatch).
  - `UsageEntry(source, text)` immutable dataclass for per-resource usage list entries.
- [x] `cli/agentworks/resources/origin.py`:
  - `Origin` dataclass with
    `variant: Literal["operator-declared", "code-declared", "auto-declared"]` and variant-specific
    fields: `file: Path` + `line: int` for operator-declared; `source: str` (code-source identifier)
    for code-declared; `source: tuple[str, str]` (first matching requirement) for auto-declared.
    Factory classmethods: `Origin.operator_declared(file, line)`, `Origin.code_declared(source)`,
    `Origin.auto_declared(source)`. Set once at publish; never mutated. The `code-declared` variant
    supports Phase 2b's catalog publisher and any future code publishers (plugins, etc.).
- [x] `cli/agentworks/resources/kind.py`:
  - `ResourceKind` Protocol: `kind`, `miss_policy`, `auto_declare_names`,
    `synthesize(requirements) -> Resource`.
  - `KIND_REGISTRY: dict[str, ResourceKind]`. Each `kinds/*.py` module self-registers into the dict
    at import; `kinds/__init__.py` imports all kinds. `agentworks/resources/__init__.py` imports
    `.kinds` so that a single `import agentworks.resources` (or any
    `from agentworks.resources import ...`) populates the registry. Phase 2 kinds slot in by adding
    new files under `kinds/` and importing them from `kinds/__init__.py`; no central manifest to
    edit.
- [x] `cli/agentworks/resources/kinds/__init__.py` + `kinds/secret.py`:
  - `SecretKind(ResourceKind)`: `kind="secret"`, `miss_policy="auto-declare"`,
    `auto_declare_names=None` (any name). `synthesize` builds a `SecretDecl` with framework-set
    `usage` list (paired `UsageEntry`s) and `origin=auto-declared`.
- [x] `cli/agentworks/resources/kinds/admin_template.py`:
  - `AdminTemplateKind(ResourceKind)`: `kind="admin_template"`, `miss_policy="auto-declare"`,
    `auto_declare_names={"default"}`. `synthesize` builds an empty-defaults `AdminConfig` with
    `origin=auto-declared`. In practice Config always publishes `admin_template:default` (even when
    no `[admin.*]` sections exist), so the miss-policy path is a safety net; pinning auto-declare
    - reserved-name `default` keeps the framework-dispatch shape uniform with the other template
      kinds and prevents typo'd names like `admin_template:custom`.
- [x] `cli/agentworks/resources/kinds/named_console_template.py`:
  - `NamedConsoleTemplateKind(ResourceKind)`: `kind="named_console_template"`,
    `miss_policy="auto-declare"`, `auto_declare_names={"default"}`. `synthesize` builds an
    empty-defaults `NamedConsoleConfig` with `origin=auto-declared`. Same safety-net reasoning as
    `AdminTemplateKind`; Config always publishes `named_console_template:default`.
- [x] `cli/agentworks/resources/registry.py`:
  - `Registry` class: per-kind dicts of Resources (`secrets`, `vm_templates`, `agent_templates`,
    ...). Mutable during the publish phase; frozen after `finalize()`. Lookup helpers
    (`lookup(kind, name)`, `iter_kind(kind)`) available once finalized.
  - `Registry.empty() -> Registry` -- class method returning a fresh empty Registry.
  - `Registry.add(kind, name, resource, origin: Origin) -> None` -- the publish-side API. The
    publisher constructs the appropriate `Origin` variant and passes it in. Config's `publish_to`
    builds `Origin.operator_declared(file=..., line=...)` from each Resource's `declared_at`.
    Catalog's `publish_to` (Phase 2b) builds `Origin.code_declared(source="agentworks.catalog")`.
    Future publishers do the same with their own variants. The Registry stores; it doesn't care
    about publisher identity beyond the Origin carried.
  - `Registry.finalize() -> None` -- the framework pass. Walks requirements via
    `required_resources()`, groups by `(kind, name)`, dispatches per `KIND_REGISTRY` miss policy
    (auto-declare synthesizes a Resource with `Origin(variant="auto-declared", ...)`; error raises
    `ConfigError`), attaches `usage` to each Resource, runs cycle detection (DFS three-coloring),
    and freezes the Registry. The name covers the whole lifecycle terminator -- not just validation
    but also the auto-declaration synthesis. Does NOT compose Resources from raw parsed data --
    that's the source publisher's responsibility, established in Phase 0.
  - The convenience that wraps `Registry.empty()` + publishers + `finalize()` lives in
    `agentworks/bootstrap.py`'s `build_registry(config)` -- see the earlier bullet. The Registry
    class itself does not expose a `from_config` classmethod; doing so would make Registry import
    Config (the wrong direction; Registry should be publisher-agnostic).
- [x] **Add `origin: Origin | None` and `usage: list[UsageEntry]` fields to every Resource type**:
      `SecretDecl` in `agentworks/secrets/base.py`; `VMTemplate`, `WorkspaceTemplate`,
      `AgentTemplate`, `SessionTemplate`, `AdminConfig`, `NamedConsoleConfig`, `SecretConfig`,
      `SecretBackendConfig`, `GitCredentialConfig` in `agentworks/config.py`. Both default to `None`
      / empty list at construction; the Registry populates them (origin at publish via
      `Registry.add` calling `with_origin(...)`; usage during `finalize` via `with_usage(...)`). The
      Config-layer `declared_at: SourceLocation` (Phase 0) stays as Config's representation; the
      Registry-layer `origin: Origin` is the framework's representation. Both exist on the same
      Resource instance after publish. Resource types gain `with_origin` / `with_usage` copy methods
      (or `@dataclass(frozen=True)` + `dataclasses.replace` shapes; LLD picks).
- [x] `cli/agentworks/config.py`: add `Config.publish_to(self, registry: Registry) -> None`.
      Iterates Config's per-kind dicts. For each Resource, builds
      `origin = Origin.operator_declared(file=resource.declared_at.file, line=resource.declared_at.line)`
      and calls `registry.add(kind, name, resource, origin)`. **Singleton publishing**: also
      publishes the two singleton-backed kinds as one-row entries, each with its own origin derived
      from its own `declared_at` -- for `Config.admin`, builds
      `Origin.operator_declared(file=config.admin.declared_at.file, line=config.admin.declared_at.line)`
      then calls `registry.add("admin_template", "default", config.admin, admin_origin)`;
      analogously for `Config.named_console` ->
      `registry.add("named_console_template", "default", config.named_console, named_console_origin)`.
      Imports `Registry` and `Origin` from `agentworks.resources` -- the explicit layer handoff.
      Config's data structures (parsed Resources, `SourceLocation`, etc.) remain framework-ignorant;
      only this publish handoff crosses the boundary.
- [x] `cli/agentworks/errors.py`: confirm `ConfigError` carries `entity_kind`, `entity_name`,
      `source` (a `(kind, name)` pair) fields if not already; existing shape is preserved.
- [x] **Tests**:
  - `cli/tests/resources/test_requirement.py`: dataclass invariants, base-vs-subclass shape.
  - `cli/tests/resources/test_origin.py`: variant invariants and immutability across all three
    variants (`operator-declared`, `code-declared`, `auto-declared`); the `code-declared` factory is
    exercised here even though its first real producer (the catalog publisher) lands in Phase 2b --
    the framework type is defined in Phase 1a, so its invariants are pinned here.
  - `cli/tests/resources/test_finalize_pass.py`: walk + dispatch in `Registry.finalize()`;
    auto-declare on miss for secrets; reserved-name restriction (Phase 1 stub for templates -- the
    dispatch behaves correctly even though no Phase 2 producers yet); error miss policy;
    first-matching origin rule; per-key + walk-order determinism. Includes a synthetic-publisher
    test that an operator-declared `SecretDecl` gets its `usage` list populated after publish +
    finalize (the `with_usage` attachment path). Composition is tested at the Config layer (Phase 0
    tests); `Registry.finalize()` operates on already-composed Resources.
  - `cli/tests/resources/test_registry_lifecycle.py`: empty Registry -> publish via `add` ->
    `finalize` -> queryable; double-`finalize` errors; `add` after `finalize` errors;
    `build_registry(config)` convenience equivalent to manual steps. Includes a Phase-1-specific
    assertion that `build_registry` invokes only `config.publish_to` (no catalog publisher yet);
    Phase 2b updates this test to also assert `catalog.publish_to` runs first.
  - `cli/tests/resources/test_cycle_detection.py`: synthetic cycle producer for testing; clear error
    reporting.
  - `cli/tests/resources/test_kind_registry.py`: `KIND_REGISTRY["secret"]` /
    `KIND_REGISTRY["admin_template"]` / `KIND_REGISTRY["named_console_template"]` lookups; each
    kind's `synthesize` shape.
  - `cli/tests/resources/test_singleton_publishing.py`: after
    `build_registry(load_config(<config with no admin.*>))`, the Registry contains exactly one
    `admin_template:default` (operator-declared, with empty-defaults `AdminConfig`) and one
    `named_console_template:default` (same shape); after
    `build_registry(load_config(<config with [admin.env]>))`, the same single entry exists but its
    `env` block is populated.

Definition of done: the public surface (`ResourceRequirement`, `ResourceKind`, `Origin`, `Registry`,
`UsageEntry`, `collect_secrets_for`, etc.) is importable from `agentworks.resources`;
`build_registry(config)` from `agentworks.bootstrap` runs on every config load (no-op for current
configs since no producers wired yet); `publish_to` / `finalize` lifecycle pinned by tests; CI
green; reviewer-approved.

## Phase 1b: Env-block secret-reference migration

Goal: env-block `{ secret = "..." }` references emit `SecretRequirement` via `required_resources()`.
Undeclared secrets auto-declare (per the new framework). The env-and-secrets SDD's strict "must
declare" error path is removed; visibility is preserved via `agw doctor` and `agw secret list`
showing every auto-declared secret with its origin source.

- [x] `cli/agentworks/env/entry.py`: `EnvEntry`'s secret-ref form gains
      `required_resources(source: tuple[str, str]) -> list[ResourceRequirement]`. Returns one
      `SecretRequirement` per referenced secret. Usage text derived from the env-block context
      (e.g., `"the ANTHROPIC_API_KEY env var"`).
- [x] `cli/agentworks/config.py`: env-block resources implement `required_resources()` by iterating
      their `env` dict and aggregating per-entry requirements. Covers `AdminConfig` (source
      `("admin_template", "default")`), `NamedConsoleConfig` (source
      `("named_console_template", "default")`), and the four multi-named template types `VMTemplate`
      / `WorkspaceTemplate` / `AgentTemplate` / `SessionTemplate` (source `("<kind>", "<name>")` per
      their existing kind/name).
- [x] Remove the env-and-secrets validation that errored on undeclared env-block secret refs
      (`render` raising `ConfigError` for unknown-secret refs in the env-block path).
- [x] **Release-notes line**: this shifts the failure mode for env-block typos. Previously a typo in
      `{ secret = "anthropic-api-ky" }` errored at config load; now it auto-declares
      `anthropic-api-ky`, which surfaces at runtime as "no backend resolved the secret" (with
      `agw doctor` and `agw secret list` showing the unexpected auto-declared name). Intentional per
      FRD Migration notes, but operators upgrading should know to scan `agw secret list` for
      unexpected auto-declared names after the upgrade.
- [x] **Tests**:
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

- [x] `cli/agentworks/config.py` (VMTemplate parsed type): add
      `tailscale_auth_key: str = "tailscale-auth-key"` field. Validate: must be a bare string (no
      `{ secret = "..." }` polymorphism, no plaintext-literal heuristic). Sample config updated.
- [x] `cli/agentworks/vms/templates.py` (`ResolvedVMTemplate`): same field; threaded through
      template-inheritance resolution like other fields.
- [x] `ResolvedVMTemplate.required_resources()` emits a `SecretRequirement` for the configured
      `tailscale_auth_key` (default `"tailscale-auth-key"`) with usage `"the Tailscale auth key"`
      and source `("vm_template", template.name)`.
- [x] `cli/agentworks/vms/manager.py`: `create_vm` and `reinit_vm` walk the resolved VM template's
      requirement subgraph via `collect_secrets_for(registry, ("vm_template", <name>))`,
      eager-resolve the result via `resolve_for_command(extra_decls=...)`, and pass the resolved
      Tailscale auth key as a function argument into the Tailscale install runner.
- [x] Tailscale install runner gains a `*, auth_key: str` keyword-only argument. The exact function
      path is **pinned at Phase 1c start** by grepping for the current `tailscale up --authkey` call
      site: pre-PR #130 this was `_install_tailscale` in `cli/agentworks/vms/initializer.py`;
      post-PR #130 it may have moved into the transports package or stayed in `vms/initializer.py`
      (mechanical move only). The kwarg-threading shape is the same either way. No `env=` injection.
      No profile-fragment writes for the auth key.
- [x] Remove the legacy `AW_TAILSCALE_AUTH_KEY` env-var-or-prompt resolution path. The framework's
      resolver is the only source of the auth key.
- [x] Update `cli/agentworks/sample-config.toml`: VM template stanza documents `tailscale_auth_key`
      and the default secret name; remove any legacy `AW_TAILSCALE_AUTH_KEY` mention.
- [x] **Tests**:
  - `cli/tests/test_vm_create_tailscale_eager_resolve.py`: `vm create` resolves the Tailscale secret
    BEFORE any state mutation; the install runner receives the value as a kwarg; no `env=`
    injection.
  - `cli/tests/test_sample_config_tailscale.py`: the updated `sample-config.toml` parses cleanly
    through the new finalize pass and the Tailscale secret auto-declares from the VM-template
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

- [x] `cli/agentworks/config.py` (`GitCredentialConfig`): add a `token: str` field defaulting to
      `git-token-<name>`. The default is computed (in `__post_init__` or by the parser; a dataclass
      literal default can't interpolate the entry's name), not hard-coded. Validate: bare string.
      Same shape rule as `tailscale_auth_key`.
- [x] `GitCredentialConfig.required_resources()` emits a `SecretRequirement` for the configured
      `token` with usage `"the auth token"` and source `("git_credentials", name)`.
- [x] **Register `GitCredentialKind` in `KIND_REGISTRY`** with miss policy `error`. This lets the
      framework recognize `git_credentials:<name>` requirements emitted from admin / agent
      templates' `git_credentials = [...]` lists, look them up in the (already-populated) registry,
      and surface a clean error when the name is undeclared. The kind doesn't synthesize (no
      auto-decl); it just validates that the named credential exists.
- [x] `AdminConfig.required_resources()` / `AgentTemplate.required_resources()`: when
      `git_credentials = ["name1", "name2"]` is set, emit a `ResourceRequirement` of kind
      `git_credentials` per name. `GitCredentialKind`'s error miss policy catches typos that
      previously went through bespoke validation.
- [x] `cli/agentworks/agents/manager.py` / `cli/agentworks/vms/manager.py`: at `agent create` /
      `agent reinit` / `vm create` / `vm reinit`, walk the requirement subgraph transitively
      (admin/agent_template -> git_credentials -> secret) and eager-resolve.
- [x] The git-credentials install runner gains a `*, tokens: dict[str, str]` (name -> token)
      keyword-only argument; the function that writes `~/.git-credentials` reads from the passed
      dict, not from `obtain_token`.
- [x] Remove `agentworks.git_credentials.base.obtain_token` and any related env-var / prompt
      resolution code. Provider classes (`github.py`, `azdo.py`) keep their
      `credential_lines(token=...)` formatting methods.
- [x] Update `cli/agentworks/sample-config.toml`: `[git_credentials.<name>]` stanzas document the
      new `token` field and the default secret-name convention.
- [x] **Tests**:
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

- [x] `cli/agentworks/secrets/inspect.py`: add `describe_secret(registry, name)` that returns a
      structured `SecretDescription` (name, kind, origin with full detail, usage list with per-entry
      source + text, per-backend mapping status, resolution preview). No prompting, no value
      resolution.
- [x] `cli/agentworks/cli/commands/secret.py`: add the `describe` typer subcommand. Calls the
      service-layer function; renders the structured result using the existing table / panel
      helpers.
- [x] Renderer covers all four output sections (header, usages, backend mappings, resolution
      preview). Per FRD R10.
- [x] Update `cli/agentworks/completions/`: the shell-completion tree picks up the new subcommand
      automatically (or via the project's regen step).
- [x] **Tests**:
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
- [ ] **Generalize the auto-declared `description` polish.** The Phase 1 implementation in
      `Registry.finalize` checks `isinstance(resource, SecretDecl)`; loosen to a structural check
      (`hasattr(resource, "description")` + empty-string test) so any kind that carries a
      `description: str` field benefits automatically. The format
      (`"(auto) <usage> for <kind>:<name>" + " (and N more)"`) stays the same. Template kinds in
      this phase don't have a `description` field today, so this is a framework-level cleanup with
      no operator-visible change in Phase 2a itself; the change earns its keep when a future kind
      acquires a description (and avoids needing a second per-kind branch). See FRD R9 and HLA's
      Framework metadata attachment section for the generalized contract.
- [ ] **Sweep command entry points to hoist `build_registry(config)`.** Phase 1c/1d hoisted
      `build_registry` to the top of `create_vm`, `reinit_vm`, and `add_git_credential` so the
      framework's per-kind miss-policies (e.g. `GitCredentialKind`'s `error` policy) fire before any
      business logic runs and the operator gets a clean typo error instead of a downstream
      `NotFoundError`. Other manager entry points -- notably `create_session` (which post-merge with
      PR #146 owns ephemeral workspace/agent creation and the secret eager-resolve), plus
      `create_workspace`, `create_agent`, `reinit_agent`, and any new commands -- haven't been
      swept. With Phase 2a introducing `TemplateRequirement` and the kind handlers' default-only
      `auto_declare_names`, a typo on `inherits = ["defualt"]` should surface as a framework error
      here too. Audit each manager-entry function; hoist `build_registry(config)` to the top of any
      that touches resources whose miss-policy could throw. Trivial cost (one call that's already
      memoization-friendly inside `bootstrap.py`); high payoff in error-shape consistency across the
      surface.
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

## Phase 2b: Catalog publisher + catalog / provider / backend kinds

Goal: bring the catalog into the framework as first-class Resources via a code publisher, plus add
the remaining bespoke-validation kinds.

- [ ] `cli/agentworks/resources/kinds/catalog.py`: `CatalogKind` for catalog commands -- three
      sub-kinds (`apt_package`, `system_install_command`, `user_install_command`) registered in
      `KIND_REGISTRY`. All three use the **error miss policy** (unknown names referenced from
      `apt_packages = ["..."]` etc. error with the referencing scope cited).
- [ ] `cli/agentworks/catalog.py` (or wherever the code-defined catalog lives today): add a
      `publish_to(registry: Registry) -> None` function. Iterates the code-defined catalog entries
      and calls
      `registry.add(kind, name, resource, Origin.code_declared(source="agentworks.catalog"))` for
      each. Catalog Resources are now full Registry citizens:
      `agw resource list --kind apt_package,system_install_command,user_install_command` (Phase 2c)
      shows them with origin = `code-declared by agentworks.catalog`.
- [ ] `agentworks/bootstrap.py` `build_registry(config)` is updated to invoke
      `catalog.publish_to(registry)` before `config.publish_to(registry)` (so any operator-declared
      override of catalog entries -- not supported today, but the order keeps the door open -- is
      layered on top of the code-declared base). The Phase 1a stub of the catalog publisher becomes
      a real publisher here.
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
    source; known names resolve. Catalog publisher publishes the expected set.
  - `cli/tests/resources/test_catalog_publisher.py`: `catalog.publish_to(registry)` populates the
    right kinds; published Resources carry `Origin.code_declared(...)`.
  - `cli/tests/resources/test_git_credential_provider_kind.py`: unknown `type` value errors;
    `"github"` and `"azdo"` resolve.
  - `cli/tests/resources/test_secret_backend_kind.py`: unknown backend kind errors; declared kinds
    resolve.

Definition of done: every kind in the registry uses framework dispatch; catalog publishes as
first-class Resources via the catalog publisher; bespoke validation removed from the loader; CI
green; reviewer-approved.

## Phase 2c: `agw resource list` / `agw resource describe`

Goal: add the cross-kind inspection commands.

- [ ] `cli/agentworks/resources/inspect.py`: add
      `list_resources(registry, kinds=None, origin=None) -> list[ResourceSummary]` and
      `describe_resource(registry, kind, name) -> ResourceDescription`.
- [ ] `cli/agentworks/cli/commands/resource.py` (new): typer command group with `list` and
      `describe` subcommands. Two-positional `describe <kind> <name>` (FRD R12 rationale).
- [ ] Renderers cover columns per HLA: list shows kind, name, origin, usage count, description;
      describe shows kind, name, full origin detail, full usage list, description. **Stops at
      framework-uniform fields** -- no backend mappings, no inheritance chains, no resolved field
      lookups. Rendering those would require kind-specific knowledge that the cross-kind command
      intentionally doesn't carry; operators reach for `agw secret describe` etc. when they need it.
      The `description` column reads reliably across kinds because Phase 2a generalized the
      auto-declared description polish (see Phase 2a checkbox; FRD R9 / R12).
- [ ] Update `cli/agentworks/completions/`.
- [ ] **Tests**:
  - `cli/tests/test_resource_list.py`: kind filter (CSV), origin filter, header summary.
  - `cli/tests/test_resource_describe.py`: per-section rendering for each kind; two-positional
    parsing; useful error message when `<kind>` is unknown.

Definition of done: `agw resource list` and `agw resource describe <kind> <name>` work across all
kinds; CI green; reviewer-approved.

**Phase 2 ships at this point.** PR sequence on `feat/resource-registry-phase-2`, branched from
`main` **after Phase 1 merges** (not from the Phase 1 branch tip). A single `locked.md` lands at the
end of Phase 2c covering the whole SDD.

## Phase 1 follow-ups (deferred at ship; non-blocking)

Items the reviewer flagged as worth landing but explicitly safe to defer past Phase 1 ship. None
gates Phase 2 work; pick them off opportunistically alongside Phase 2 phases that touch the relevant
code.

- **Synthesize-on-the-fly duplication.** `SecretResolver.render` (Phase 1b),
  `_lookup_or_synthesize_secret` in `vms/manager.py` (Phase 1c), and `_collect_git_tokens`'s
  fallback (Phase 1d) all build a bare `SecretDecl(name=..., description="")` matching the
  `_SecretKind.synthesize` shape (sans `origin`). A `SecretDecl.auto_declared(name)` classmethod on
  `secrets/base.py` would single-source the shape so every fallback site converges by construction;
  lands cleanly alongside Phase 2a's `VMTemplateKind` (which closes most of the fallback sites'
  practical need anyway).
- **`_collect_git_tokens` placement.** Lives in `vms/manager.py` today, cross-imported by
  `agents/manager.py`. A neutral location (e.g. `agentworks/git_credentials/resolve.py`) reads more
  naturally; move when Phase 2 touches the helper.
- **`output.detail` vs `output.info` for nested sections.** `render_secret_description` uses
  `output.info` with hand-indented strings; the rest of the codebase uses `output.detail`. Pure
  style; do it when Phase 2c's `agw resource describe` lands the cross-kind renderer.
- **`SecretDescription.kind = "secret"` hard-coded.** Use the kind registry constant so a
  hypothetical rename can't drift the describe output silently.
- **FRD R10 dedupe wording is ambiguous.** "Duplicate usage text is collapsed" doesn't pin whether
  dedupe is by `(source, text)` (current implementation) or by `text` alone. Resolve in the FRD when
  Phase 2c's `agw resource describe` reuses the same `usage` rendering. The current read is
  `(source, text)`; the FRD edit can either confirm or flip and update the renderer.
- **Pre-existing `agents/manager.py:1337` `ExecTarget` reference.** Confirmed already fixed on main
  when PR #136 landed and was merged into this branch via `eb3724e`. No action.
- **Auto-declared `description` polish is secret-specific.** `Registry.finalize`'s
  `_polish_auto_declared_description` does `isinstance(resource, SecretDecl)`. Phase 2a generalizes
  it to a structural check so any kind with a `description: str` field benefits automatically (FRD
  R9, HLA Framework metadata attachment). The Phase 2a plan carries the checkbox; no Phase 1 action
  needed.

## Sequencing notes

- **Phase order**: 0 -> 1a -> 1b -> 1c -> 1d -> 1e -> ship Phase 1 -> 2a -> 2b -> 2c -> ship
  Phase 2. Each phase ends at a green CI and a usable intermediate state.
- **Why env-block migration before system secrets**: Phase 1b gives the framework a real producer of
  `SecretRequirement` exercised end-to-end before Phase 1c / 1d wire in the system-secret producers.
  Bugs in the finalize pass surface against the larger surface area first.
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
- **Lockfile**: a single `locked.md` lands once at the end of Phase 2c covering the whole SDD. The
  plan itself is the running log across Phase 1 + Phase 2 (checkboxes mark progress; the Phase 1
  follow-ups section above tracks items deferred at Phase 1 ship). No per-phase milestone files.
- **Out-of-scope reminders**: no plugin source (future SDD); no DB-backed resources (future manifest
  SDD); no namespaces; no per-source miss policies; no per-field merge.

## Open items for the LLD

The plan above stays at HLA fidelity; the LLD (or commit-by-commit notes during Phase 1a) should
pin:

- The exact regex grammar for the section-line scanner (bare keys are universal in agentworks
  configs today; quoted-segment support is a small extension worth getting right in the LLD).
- Per-kind error message templates (string format) for the framework's `ConfigError`.
- The exact subclass hierarchy of `ResourceRequirement` (frozen dataclasses with `kw_only`? what
  about hashability when used as dict keys?).
- The `UsageEntry` serialization shape for `agw secret describe` rendering.
- The exact shape of `with_origin` / `with_usage` on Resource types: shared mixin / base class with
  the framework-attached fields, or per-type `@dataclass(frozen=True)` + `dataclasses.replace`
  calls? Affects how invasive the Phase 1a edits to existing Resource types are.
