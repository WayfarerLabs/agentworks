# Resource registry: high-level architecture

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/resources/`

## Overview

The framework lands as a new `agentworks.resources` package that introduces a **second layer**
between the existing `agentworks.config` parser and the runtime / manager layers. Two layers,
distinct responsibilities:

- **`Config`** (existing, in `agentworks/config.py`): the parsed source. Composes Resources from
  on-disk TOML sections (top-level + sub-sections) and attaches a Config-layer
  `declared_at: SourceLocation` to each. Enforces orphan rejection at parse time. Today's parser
  plus the Phase 0 additions (tomlkit migration, file:line capture, composition).
- **`Registry`** (new, in `agentworks/resources/registry.py`): the framework's typed, queryable
  Resource store. **Exists independently of any particular source**; starts empty. Sources publish
  into it (`config.publish_to(registry)`); other sources can publish too (future plugins, manifests,
  etc.). After publishers finish, `registry.validate()` runs the framework pass: auto-declares
  missing references, attaches `origin` and `usage`, detects cycles. The convenience
  `Registry.from_config(config)` wraps the common Config-only case.

The framework operates on `Registry`. Manager-entry code consumes `Registry` where it needs
framework queries (e.g., requirement subgraph walks for eager-resolve); other call sites continue
taking `config: Config` and migrate gradually as needed.

Existing config types (`SecretDecl`, `VMTemplate`, `GitCredentialConfig`, ...) gain a
`declared_at: SourceLocation` field at the Config layer; the Registry's copies gain `origin`
(framework type, translated from `declared_at` at publish) and `usage` (populated during validate).
Each config type that references other resources by name implements a `required_resources()` method
emitting one `ResourceRequirement` per reference; the Registry consumes them during validate.

```text
+----------------------+         +----------------------------+
|  agentworks.config   | publish |  agentworks.resources      |
|  - parses TOML       |-------->|  - ResourceRequirement     |<--+
|  - composes Resources|         |  - ResourceKind / Origin   |   |
|  - declared_at:      |         |  - Registry (empty)        |   | per-kind logic
|    SourceLocation    |         |    * accepts publishes     |   | SecretKind, etc.
+----------------------+         |    * .validate() runs the  |   |
                                 |      framework pass:       |   |
   (future publishers:           |        - walks reqs        |   |
   plugins, manifests, ...)      |        - auto-declares     |---+
                                 |        - attaches usage    |
                                 |        - detects cycles    |
                                 +-------------+--------------+
                                               |
                                               v
                                 +-------------+--------------+
                                 |  Registry (validated):     |
                                 |  - secrets[name]           |
                                 |  - vm_templates[name]      |
                                 |  - git_credentials[name]   |
                                 |  ... each with origin+usage|
                                 +-------------+--------------+
                                               |
                              +----------------+----------------+
                              |                |                |
                              v                v                v
                      +---------------+ +---------------+ +-----------------+
                      | agw doctor    | | manager-entry | | eager-resolve   |
                      | agw secret    | | walks subgraph| | (existing       |
                      |   list/desc.  | | for command-  | |  orchestrator   |
                      | (Phase 1)     | | scoped secrets| |  + extra_decls) |
                      | agw resource  | +---------------+ +-----------------+
                      |   list/desc.  |
                      | (Phase 2)     |
                      +---------------+
```

The package is pure Python with no Typer dependency, consistent with the typer-isolation rule. The
CLI layer (commands) and manager layer call into it; manager-entry code uses the registry plus the
existing `agentworks.secrets.orchestration` to resolve secrets per command.

## Package layout

```text
cli/agentworks/resources/
  __init__.py            # public surface re-exports
  requirement.py         # ResourceRequirement, kind-specific subclasses
  origin.py              # Origin dataclass + factory helpers
  kind.py                # ResourceKind protocol; miss-policy machinery
  registry.py            # validation pass: walk, dispatch, cycle-detect, attach metadata
  kinds/
    __init__.py
    secret.py            # SecretKind (auto-declare any name)
    vm_template.py       # VMTemplateKind (Phase 2: auto-declare reserved 'default')
    # ... more in Phase 2
```

Why a new package rather than extending `agentworks.config`: the framework has its own lifecycle
(walk, dispatch, attach metadata, cycle-detect) and clear boundaries with the parser. Keeping it
separate keeps `config.py` focused on parsing, mirrors how `agentworks.env` and `agentworks.secrets`
extracted env/secret concerns into their own packages, and makes `Registry` substitutable when the
source format changes (see "Future: YAML manifests" below).

The `kinds/` subdirectory is slightly over-structured for Phase 1's single kind (`secret.py`) but
right-sized for Phase 2's four to six kinds. Starting with the subdirectory avoids a churn-rename
later.

## Two layers: Config and Registry

The framework introduces a deliberate split between **what the operator typed** (`Config`, parsing
layer) and **what the framework sees** (`Registry`, runtime layer).

### `Config` (parsing layer)

- Lives in `agentworks/config.py`. Unchanged location and class name. No mechanical rename.
- Parses TOML and **composes Resources** from top-level + sub-section pairs. Output is per-kind
  dicts of Resources, not raw TOML sections: `Config.secrets: dict[str, Secret]`,
  `Config.vm_templates: dict[str, VMTemplate]`, `Config.git_credentials: dict[str, GitCredential]`,
  plus singletons (`Config.admin`, `Config.secret_system`). The Resource types (today named
  `VMTemplate`, `AdminConfig`, `SecretConfig`, `SecretBackendConfig`, `GitCredentialConfig`) **are
  the Resources** -- they happen to live in `config.py` today; their naming is a minor cleanup item
  (see "Naming follow-up" below) but the framework treats them as Resources regardless.
- **Enforces orphan rejection** (FRD R2). If `[vm_templates.x.env]` is parsed without a parent
  `[vm_templates.x]`, Config errors at parse-time: it cannot publish a Resource that only has the
  env sub-section. Structural enforcement happens here because Config has the schema knowledge of
  which kinds support which sub-sections.
- Each composed Resource carries a Config-layer `declared_at: SourceLocation` field, where
  `SourceLocation = (file: Path, line: int)` is defined in `agentworks/config.py`. This is the
  Config layer's own representation of "where the operator declared this Resource" -- the
  framework's `Origin` type is a separate concept owned by the Registry layer (see below). Config
  does not depend on `agentworks.resources`.
- The existing `load_config()` function and `Config` class stay; all current imports continue to
  work.

### `Registry` (framework layer)

- Lives in `agentworks/resources/registry.py`. New class introduced by this SDD.
- **The Registry exists independently of any particular source.** It starts empty; publishers add
  Resources to it; after all publishers have contributed, the Registry validates itself. This
  decoupling leaves the door open for future sources (plugins, manifests, etc.) without changing the
  Registry's API.
- **Publish phase** (Config-side): Config (and any future publisher) provides Resources to the
  Registry via a `publish_to(registry)` call. The Registry accumulates them, translating Config's
  `declared_at: SourceLocation` into the framework's
  `Origin(variant="operator-declared", file=..., line=...)` at the time of publish. Multiple
  publishers can contribute; the Registry just keeps adding.
- **Validate phase** (Registry-side): after publishing completes, `registry.validate()` runs the
  framework pass: walks the requirement graph, dispatches miss policies for references that don't
  resolve to a published Resource, synthesizes auto-declared Resources (with
  `Origin(variant="auto-declared", source=...)` from the kind's `synthesize`), attaches the `usage`
  list to each Resource, and detects cycles. The Registry is mutable during publish; validate
  finalizes it.
- **Convenience entry point** for the common Config-only case:

  ```python
  def Registry.from_config(config: Config) -> Registry:
      registry = Registry.empty()
      config.publish_to(registry)
      registry.validate()
      return registry
  ```

  Most current call sites (the CLI's existing `load_config()`-style entry path) use this
  convenience. The lower-level `publish_to` / `validate` split is exposed for future multi-source
  scenarios.

- **Layering rule**: Config does not depend on `agentworks.resources`. The publish call is on the
  Registry's surface: `registry.publish_resources(kind, dict_of_resources)` (or similar), invoked by
  Config's `publish_to(registry)` method. The translation from `SourceLocation` to `Origin` happens
  in the Registry's accept-side, not in Config. Config remains framework-ignorant.
- Exposes per-kind queries: `registry.secrets`, `registry.vm_templates`, etc. Each per-kind view
  contains operator-declared Resources (from publishers) **plus** auto-declared Resources
  synthesized during validate. All Resources in `Registry` carry full `origin` (framework type) and
  `usage` metadata.
- The framework's lookup surface lives here: `registry.lookup(kind, name)`, iter helpers, subgraph
  walks for eager-resolve, the data backing `agw doctor` / `agw secret describe` /
  `agw resource list|describe`. Surface sufficient for `collect_secrets_for(registry, root)`:
  `lookup` resolves the root and each transitive target; each Resource's `required_resources()`
  provides the edges to walk.

**Copy, not mutate (across layers).** Resources in `Registry` are distinct objects from their
`Config` counterparts. The Registry's Resource has `origin: Origin` (framework type, translated from
Config's `declared_at` at publish time) and `usage: list[UsageEntry]` (populated during `validate`).
The `Config` layer's instances stay pristine and carry only `declared_at`. The two layers hold
distinct objects under the same name.

### Naming follow-up

The Resource types in `config.py` today are named for the "config" framing rather than the Resource
framing. After Phase 0 lands and the framework's Resource semantics are clear, a follow-up cleanup
PR can rename them to drop the `*Config` suffix where it's misleading: `AdminConfig` -> `Admin`,
`SecretConfig` -> `SecretSystem` (the secret-system singleton), `SecretBackendConfig` ->
`SecretBackend`, `GitCredentialConfig` -> `GitCredential`. `VMTemplate` / `WorkspaceTemplate` /
`AgentTemplate` / `SessionTemplate` already follow the Resource naming. The rename is optional and
orthogonal to the framework's behavior; deferred to keep this SDD's diff bounded.

### Validation responsibilities

Each layer owns a specific class of validation. Both raise `ConfigError`; the layer that catches the
issue determines the error's framing.

**Config-layer validation** (in `agentworks.config`, today's parse-time checks plus the new
composition / orphan-rejection / Origin-attachment from Phase 0):

- TOML parse errors (syntax, duplicate keys at the same path).
- Field types per the schema (`str`/`int`/`bool`/`list`/inline-table shapes).
- Required fields present per resource type.
- Name regex / kebab-case validation for operator-typed identifiers.
- Operator-typed value validation (e.g., URL formats, enum values like
  `git_credentials.type in {"github", "azdo"}`).
- **Resource composition**: top-level + sub-section pairs combined into single Resource instances
  per kind.
- **Orphan rejection** (FRD R2): a sub-section like `[vm_templates.x.env]` without
  `[vm_templates.x]` cannot produce a valid Resource, so Config errors. Lives here because Config
  has the schema knowledge of which kinds support which sub-sections.
- **`declared_at` attachment**: each operator-declared Resource gets
  `declared_at: SourceLocation(file=..., line=...)` set at construction time from the `tomlkit` line
  capture. `SourceLocation` is Config's own type; the framework's `Origin` is a separate
  Registry-layer concept constructed from `declared_at` when Resources are published into the
  Registry (in `Registry.add` / `publish_to`).

**Registry-layer validation** (in `Registry.validate`, new with this SDD; runs after all publishers
have contributed via `publish_to`):

- Requirement walks via each Resource's `required_resources()` (a Resource may declare it depends on
  others by `(kind, name)`).
- Miss policy dispatch (auto-declare with optional reserved-name restriction; error).
  Operator-declared Resources from publishers satisfy requirements directly; missing ones trigger
  the kind's miss policy.
- Reserved-name restrictions per kind (e.g., template kinds accept auto-decl only for `default`).
- **Origin attachment**: operator-declared Resources received
  `Origin(variant="operator-declared", file=..., line=...)` at publish time (Registry translated
  `declared_at` -> `Origin` then). Auto-declared Resources get
  `Origin(variant="auto-declared", source=...)` from the kind's `synthesize` during validate.
- Usage attachment: each Resource accumulates a `usage` list with one entry per matching
  requirement.
- Cycle detection across the requirement graph.
- All semantic / cross-resource checks.

`ConfigError` from the Config layer carries parse-time context (file/line, field name).
`ConfigError` from the Registry layer carries framework context (kind, name, requirement source).
Same exception type; consistent rendering at the CLI layer; the message body distinguishes.

### Why two layers, not a rename

The original draft renamed `Config` to `Registry`. That conflated two concepts (parsing-with-
composition vs. framework view) in one class, forced a 1300-line / 59-file rename, and tied the
framework's runtime API to the on-disk format. The layered split:

- Keeps parsing-with-composition and framework-view responsibilities separate; each layer has a
  focused test surface.
- Avoids the rename churn on the top-level container. The Resource sub-types in `config.py` keep
  their existing names through Phase 0/1; renaming them to drop the `*Config` suffix is an optional
  follow-up (see "Naming follow-up" above).
- Makes the source format (TOML vs. future YAML manifests) substitutable without changing the
  framework's API.

### Future: YAML manifests

When resources eventually move to per-resource YAML manifests, only the producer changes:

```text
N YAML manifests -> parse each -> manifest.publish_to(registry) -> registry.validate()
```

The `Config` layer fragments (one parsed object per manifest file, or merged by `(kind, name)`) or
is replaced by a thinner parsed-manifests aggregate; the `Registry` interface stays identical. The
framework consumes the `Registry`, not the producer. The validation pass -- orphan rejection (which
becomes structurally moot in YAML), miss policies, origin attachment, cycle detection -- is the same
regardless of source format.

`Origin` generalizes naturally: `Origin.file` is already a `Path` and works for any source. The
framework's API stays identical; `Origin` either keeps `line` (per-line manifests), makes it
optional (per-resource-per-file manifests where the whole file is the resource), or grows a new
variant. Implementation detail for a future SDD.

## Core types

### `ResourceRequirement`

A base immutable dataclass with kind-specific concrete subclasses. Producers (`required_resources()`
on each source type) return concrete subclasses (`SecretRequirement`, `TemplateRequirement`, ...);
the framework consumes them through the base class. Fields on the base:

- `name: str` -- target resource name (operator-overridable or fixed per the source's field).
- `kind: str` -- target resource kind identifier (`"secret"`, `"vm_template"`, ...). The same kind
  strings appear throughout the framework: `KIND_REGISTRY` keys, `Origin.source[0]`, error message
  kind labels. One canonical set.
- `usage: str` -- system-defined role per the FRD's sentence template. Frozen at requirement
  construction time.
- `source: tuple[str, str]` -- `(kind, name)` of the declaring resource. The `kind` matches the
  declaring resource's kind (`"vm_template"` for `vm_templates.azure-prod`, `"git_credentials"` for
  `git_credentials.github-prod`); the `name` is the declaring resource's name.

Concrete subclasses add kind-specific fields the registry's auto-declare logic may use. Phase 1's
`SecretRequirement` adds none; the subclass exists so producers and the framework agree on the
target kind without dispatch on the `kind` string. Phase 2 subclasses (`TemplateRequirement`, etc.)
carry per-kind defaults.

Producers emit a flat list per call; the framework concatenates the lists.

### `ResourceKind`

A protocol implemented per kind. One instance per kind, registered in a module-level dict the
validation pass consults:

- `kind: str` -- the kind identifier matching `ResourceRequirement.kind`.
- `miss_policy: Literal["auto-declare", "error"]` -- which branch the validation pass takes when a
  requirement points at a missing name.
- `auto_declare_names: AbstractSet[str] | None` -- when `miss_policy == "auto-declare"`, the set of
  names the kind accepts. `None` means "any name" (secrets). `{"default"}` means "only the reserved
  name `default`" (templates).
- `synthesize(requirements) -> Resource` -- called when a missing name is being auto-declared.
  Receives the full list of matching requirements (in config-load order). Produces the resource
  instance with whatever defaults the kind wants (empty `backend_mappings` for secrets, the kind's
  code-defined defaults for templates, ...).

The `kind.py` module exports a `KIND_REGISTRY` dict mapping `kind` strings to `ResourceKind`
instances. Kinds are registered as they migrate: Phase 1 registers only `SecretKind`; other kinds
keep their existing bespoke validation until Phase 2 brings them into the framework.

### `Origin`

Carried on every resource. One dataclass with a variant tag:

- `variant: Literal["operator-declared", "auto-declared"]`
- For `operator-declared`: `file: Path` and `line: int` for the declaration's opening line.
- For `auto-declared`: `source: tuple[str, str]` -- the first matching requirement's source, per
  R1's config-load walk order.

Set once when the resource is added to the registry; never mutated afterwards.

The full list of matching requirement sources (for `agw secret describe`'s "also required by ..."
display) is derived from the resource's `usage` list, not stored separately on `Origin`. Each usage
entry carries the source of the requirement that contributed it (see Terminology in the FRD), so
origin doesn't need to duplicate that data.

The loader is responsible for capturing `file` / `line` during TOML parsing. Python's stdlib
`tomllib` does not expose line info, so the loader switches to **`tomlkit`** (actively maintained,
line info exposed via the item position APIs). `tomlkit` raises on duplicate keys at the same path,
matching `tomllib` behavior, so the FRD R4 "duplicate operator declarations are TOML errors" rule
holds. The existing parsing surface in `config.py` doesn't change shape; only the parse step swaps
libraries.

## Resource composition (Config layer)

A Resource is the conceptual unit; TOML splits it across sections (a template's fields plus its
`.env` sub-section plus any future sub-tables). The Config layer parses TOML and composes Resources
from sections, publishing per-kind dicts of fully-composed Resources.

The composition step enforces the FRD R2 orphan-rejection rule at the Config layer (not in the
Registry's publish/validate flow):

```python
def _compose_resources(parsed_sections) -> Config:
    """Inside agentworks.config's load_config(). Walks the parsed TOML sections and
    builds per-kind dicts of composed Resources.

    For each multi-named kind whose sections support sub-tables (vm_templates,
    agent_templates, workspace_templates, session_templates, ...):
      - For each [<container>.<name>] section, creates the Resource with
        declared_at=SourceLocation(file, line) attached from the parse-time capture.
      - For each [<container>.<name>.<sub>] section, attaches the sub-section's
        fields to the existing Resource.
      - If a sub-section's parent [<container>.<name>] is not in parsed sections,
        raises ConfigError pointing at the orphan. Config cannot publish a Resource
        from just a sub-section.
    """
```

Singletons are exceptions: their root declaration is neither required nor accepted. Today these are
`admin` (`[admin.config]`, `[admin.env]`, `[admin.git_credentials]`, ...) and `secret_config`. Their
sub-tables are valid without any root.

Sub-section composition is additive: a Resource composed from `[vm_templates.x]` plus
`[vm_templates.x.env]` carries both sets of fields. No key collisions are possible by construction
(parent top-level fields are not under any sub-section).

For secrets, `backend_mappings` is typically dot-notation inside the `[secrets.<name>]` section
rather than a separate `[secrets.<name>.backend_mappings]` sub-section, so the orphan rule is
effectively a no-op for secrets in current practice. The rule still applies if someone writes the
sub-section form.

**Pipeline**: `load_config()` parses TOML, composes Resources, enforces orphan rejection, attaches
`declared_at: SourceLocation` to each composed Resource -> returns `Config` (a registry of
operator-declared Resources with parse-time location). The Registry starts empty;
`config.publish_to(registry)` contributes Config's Resources (translating `declared_at` into
`Origin` at the publish boundary). Future sources (plugins, manifests) would publish similarly.
`registry.validate()` adds auto-declared Resources with
`Origin(variant="auto-declared", source=...)`, attaches `usage` lists, detects cycles -> the
Registry is now queryable. The CLI's typical entry point is the `Registry.from_config(config)`
convenience, which wraps this pipeline.

## Publish and validate

The Registry's lifecycle has two phases. **Publish** accepts Resources from any source; the Registry
is mutable during this phase. **Validate** runs the framework pass and finalizes the Registry; once
`validate()` returns, the Registry is queryable but no longer mutable.

```python
# Config-side: publish operator-declared Resources to the Registry.
class Config:
    def publish_to(self, registry: Registry) -> None:
        """Publish each operator-declared Resource. Registry translates declared_at
        into Origin(variant="operator-declared", ...) on its side."""
        for kind, kind_dict in self.as_resource_dicts().items():
            for name, resource in kind_dict.items():
                registry.add(kind, name, resource, declared_at=resource.declared_at)

# Registry-side: accept publishes, then validate.
class Registry:
    def add(self, kind: str, name: str, resource: Resource, *,
            declared_at: SourceLocation) -> None:
        """Add an operator-declared Resource. Translates declared_at -> Origin here,
        so Config doesn't depend on agentworks.resources."""
        origin = Origin.operator_declared(
            file=declared_at.file, line=declared_at.line,
        )
        self._resources.setdefault(kind, {})[name] = resource.with_origin(origin)

    def validate(self) -> None:
        """Run the framework pass over already-published Resources. Auto-declares,
        attaches usage, detects cycles. After return, the Registry is queryable but
        no longer accepts publishes."""
        # 1. Collect all requirements across published resources.
        requirements: list[ResourceRequirement] = []
        for kind_dict in self._resources.values():
            for resource in kind_dict.values():
                requirements.extend(resource.required_resources())

        # 2. Group by (kind, name); preserve first-encountered ordering for origin
        # recording.
        by_target: dict[tuple[str, str], list[ResourceRequirement]] = {}
        for req in requirements:
            by_target.setdefault((req.kind, req.name), []).append(req)

        # 3. For each (kind, name): existing-in-registry? auto-decl? error?
        for (kind, name), reqs in by_target.items():
            kind_handler = KIND_REGISTRY[kind]
            existing = self._resources.get(kind, {}).get(name)
            if existing is not None:
                # Operator-declared (Origin already attached at publish). Attach usage.
                self._resources[kind][name] = existing.with_usage(_usage_list(reqs))
            else:
                # Missing: dispatch miss policy.
                match kind_handler.miss_policy:
                    case "auto-declare":
                        if (kind_handler.auto_declare_names is None
                                or name in kind_handler.auto_declare_names):
                            # synthesize attaches Origin(variant="auto-declared", ...).
                            self._resources.setdefault(kind, {})[name] = (
                                kind_handler.synthesize(reqs)
                            )
                        else:
                            raise ConfigError(...)  # reserved-name restriction violated
                    case "error":
                        raise ConfigError(...)

        # 4. Cycle detection across the now-complete requirement graph.
        _detect_cycles(by_target)
        self._frozen = True

# Convenience for the common Config-only case.
@classmethod
def from_config(cls, config: Config) -> Registry:
    registry = cls.empty()
    config.publish_to(registry)
    registry.validate()
    return registry
```

Walk order for `requirements` is config-load order: top-to-bottom in the TOML file, top-level
sections in declaration order. Within each source's `required_resources()` call, requirements come
back in whatever order the source returns them (typically the order of fields in the schema).

### Cycle detection

A directed graph where nodes are `(kind, name)` and edges are requirements (source -> target). DFS
three-coloring (white = unvisited, gray = on stack, black = finished). Encountering a gray node
mid-walk yields a cycle; the implementation collects the path and surfaces it in a single
`ConfigError`.

Phase 1 exercises no cycles (secrets don't reference secrets). The check ships in Phase 1 for
completeness; Phase 2's template inheritance is where it earns its keep.

### Errors

All errors raised during the validation pass are `ConfigError` (existing `agentworks.errors` type)
with the standard service-layer shape -- structured fields for kind/name/source, formatted by the
CLI layer. Examples:

- Unknown name in an error-policy kind:
  `ConfigError(kind="vm_template", entity="base", source=("vm_template", "azure-prod"))` rendered
  as: `vm_template "azure-prod" references unknown vm_template "base"`.
- Reserved-name restriction violated:
  `ConfigError(kind="vm_template", entity="custom-base", reason="reserved-name-restriction")`
  rendered as: `vm_template kind only auto-declares the reserved name "default"; got "custom-base"`.
- Cycle detected: rendered as a path
  (`vm_template:azure-prod -> vm_template:base -> vm_template:azure-prod`).

## Framework metadata attachment

There is no per-field merge between operator declarations and auto-declared defaults. A resource is
either operator-declared (use what the operator wrote, verbatim) or auto-declared (synthesized from
the kind's defaults). The framework's only job in either case is attaching framework metadata:

- **`origin`**: set at registration time (operator-declared with file:line, or auto-declared with
  first matching requirement source). Never mutated.
- **`usage`**: a list of `UsageEntry(source, text)` pairs populated from all matching requirements,
  accumulated by the validation pass. Each entry carries both the requirement's source
  `(kind, name)` and its usage text. Operator-declared resources get the same usage list attached as
  auto-declared ones; it's framework-collected, not operator-settable.

If an operator wants a partial override of a default template, they don't get it through field-level
merging on the `default` declaration. They declare a child template with `inherits = ["default"]`
and override fields there (existing template-inheritance mechanism).

Duplicate operator declarations of the same `(kind, name)` are TOML parse errors (duplicate keys at
the same path); the framework never sees them.

## Auto-declare details

### Secret kind

`SecretKind.synthesize(requirements)` builds a `SecretDecl` with:

- `name = requirements[0].name`
- `description = None` (operator-set field; auto-decl leaves blank)
- `hint = None`
- `backend_mappings = {}` (empty; the framework's default per-backend conventions (e.g.,
  `AW_SECRET_<NAME>`) apply at resolution time)
- `usage = [UsageEntry(source=r.source, text=r.usage) for r in requirements]` -- a list where each
  entry pairs the requirement's source with its usage text. Duplicate text from different sources is
  preserved (different sources are different rows in `agw secret describe`); dedup-by-text happens
  at render time only where summary display calls for it.
- `origin = Origin(variant="auto-declared", source=requirements[0].source)`

No reserved-name restriction; any name is accepted.

For operator-declared secrets, the parser produces a `SecretDecl` with empty `usage`; the validation
pass then populates `usage` from the matching requirements using the same `UsageEntry` shape.
`usage` is framework-set in both cases (operator declarations cannot specify it).

### Template kinds (Phase 2)

`VMTemplateKind.synthesize(requirements)` builds a `VMTemplate` with the kind's code-defined
defaults (the same defaults currently encoded in the resolver's "implicit default" fallback, hoisted
into one place). The `requirements[0].source` is recorded as origin.

`auto_declare_names = {"default"}` -- only the reserved name. Any other missing name from a
`TemplateRequirement` triggers an error at the validation pass.

Same shape applies for `WorkspaceTemplateKind`, `AgentTemplateKind`, `SessionTemplateKind`.

## Requirement sources

Each existing config type that references resources by name gets a `required_resources()` method.
Phase 1 sources:

- `SecretRefEnvEntry` (the `{ secret = "..." }` form of `EnvEntry`): emits one
  `SecretRequirement(name=<ref>, usage=<...>, source=(<scope>, <scope-name>))` per reference. The
  `usage` text is derived from the env-block context (e.g., `"the ANTHROPIC_API_KEY env var"`).
- `VMTemplate.tailscale_auth_key`: emits `SecretRequirement` with `source=("vm_template", <name>)`
  and `usage="the VM-provisioning auth key"`.
- `GitCredentialConfig.token`: emits `SecretRequirement` with `source=("git_credentials", <name>)`
  and `usage="the auth token"` (or similar; usage phrasing follows the Terminology sentence-template
  test).
- `AdminConfig.git_credentials` / `AgentTemplate.git_credentials` lists: each named credential is a
  reference; emits a `GitCredentialRequirement` (Phase 2-shaped; Phase 1 still uses bespoke
  validation for the list but the requirements are emitted so the orchestrator's transitive walk
  works).

Phase 2 adds:

- Template `inherits = [...]` references: emit `TemplateRequirement` per parent.
- `apt_packages` / `system_install_commands` / `user_install_commands` references: emit
  `CatalogRequirement` per entry.
- `git_credentials.*.type` references: emit `ProviderRequirement`.
- `[secret_backends.<kind>]` and `secret_config.backends` references: emit `BackendKindRequirement`.

## Per-command eager-resolve scope

Registry construction is universal (config load builds the whole registry). Eager-resolve scope is
per-command, driven by the requirement subgraph rooted at the resource being provisioned.

```text
manager-entry  -->  resource-being-provisioned  -->  transitively walk required_resources()
                                                     in the (already-built) registry
                                                  --> collect SecretDecls
                                                  --> pass as extra_decls to
                                                      orchestrator.resolve_for_command(...)
```

The orchestrator's `extra_decls` parameter was left in place by the env-and-secrets SDD as the
migration hook. This SDD wires it up.

### Transitive walk

A small helper in `resources/__init__.py`. Phase 1 ships the secret-specific form below; the
underlying walk is kind-agnostic (DFS over `required_resources()`, dedupe by `(kind, name)`), so
Phase 2 can add sibling helpers (or a generic `collect_resources_for(..., target_kind=...)`)
trivially. Choosing the more specific surface for Phase 1 keeps the call-site API obvious.

```python
def collect_secrets_for(
    registry: Registry,
    root: tuple[str, str],
) -> list[SecretDecl]:
    """Walk required_resources() depth-first from root; collect Secret resources."""
```

Example: rooted at `("vm_template", "azure-prod")`. Walks `tailscale_auth_key` to
`secret:tailscale-auth-key`. Walks `inherits` chain transitively (Phase 2). Walks each
`git_credentials` reference in `admin.config` -> `git_credentials:<name>` -> `secret:<token>`.
Returns the set of `SecretDecl`s encountered.

Deduplicates by `(kind, name)`. Walks each node once.

### Per-command map (Phase 1 state)

| Command                                               | Resource provisioned | Subgraph root for eager-resolve        |
| ----------------------------------------------------- | -------------------- | -------------------------------------- |
| `vm create` / `vm reinit`                             | VM, admin user       | resolved VM template + admin config    |
| `workspace create` / `reinit`                         | workspace            | resolved workspace template (empty P1) |
| `agent create` / `agent reinit`                       | agent                | resolved agent template                |
| Shell-opening (session, console, vm/agent shell/exec) | (no provisioning)    | -- (env-block eager-resolve unchanged) |

Manager-entry code at each provisioning command pulls the relevant resource(s), runs
`collect_secrets_for(...)` to get the secret list, and passes them to
`orchestrator.resolve_for_command(extra_decls=...)`. Resolved values come back in a
`Mapping[str, str]` indexed by secret name; the manager picks out the ones the provisioning runners
need and threads them as function arguments.

### Provisioning runner integration

The provisioning runners (`_install_tailscale` in `vms/initializer.py`, the git-credentials write
step in admin/agent setup) gain function-argument parameters for their resolved secrets:

```python
def _install_tailscale(ts_target, ..., *, auth_key: str) -> None:
    ts_target.run(f"sudo tailscale up --authkey={shlex.quote(auth_key)} ...")
```

No `env=` injection; no profile fragment writes for these values. Hermetic provisioning contract
from the env-and-secrets SDD is preserved end-to-end.

Resolved values live in the manager's local scope for the duration of the command. No caching or
persistence across commands; the next invocation re-resolves through the backend chain. This matches
the env-and-secrets SDD's "values never persisted by agentworks" guarantee.

## CLI surfaces

### Phase 1: `agw secret describe`

New command added to the existing `cli/agentworks/cli/commands/secret.py` (which already hosts
`agw secret list` from the env-and-secrets SDD). Service-layer logic lives in `agentworks.secrets`,
alongside the existing list-formatting helpers. No new shim package; the CLI module imports the
service-layer function directly.

Output sections (per FRD R10):

- Header: name, kind, origin, description.
- Origin detail: file path and line for operator-declared, requirement source for auto-declared.
- Usages: one row per matching requirement.
- Backend mappings: per-backend status table (operator-set value, backend convention default, or "no
  mapping; skipped"; no merging).
- Resolution preview: `would resolve via <backend>` or `would prompt`.

Does not prompt, does not resolve values.

### Phase 2: `agw resource list` / `agw resource describe`

New command group. Generic across kinds. Implementation pulls from the registry, filtered and
formatted by the framework's `Origin` and `usage` fields.

Argument shape:

```text
agw resource list [--kind <kind1,kind2,...>] [--origin operator|auto]
agw resource describe <kind> <name>
```

Two-positional describe (kind + name) because names are unique only within a kind (FRD R12). The
usual CLI convention has a single positional name with context flags; the two-positional shape is a
deliberate carve-out for this command.

## Tailscale and git-credential migration shapes

### Tailscale (Phase 1)

Schema change: VM template gains `tailscale_auth_key: str` (default `"tailscale-auth-key"`).
`required_resources()` on a resolved `VMTemplate` emits one `SecretRequirement`.

Flow at `vm create`:

1. Registry loads; the framework's validation pass auto-declares `secret:tailscale-auth-key` if no
   operator block exists.
2. Manager-entry walks the VM template subgraph; collects the `tailscale-auth-key` SecretDecl.
3. Orchestrator's `resolve_for_command(extra_decls=[<that SecretDecl>])` resolves the value through
   the backend chain (prompting if no backend yields).
4. Manager passes the resolved value as a kwarg to `_install_tailscale(...)`.

Existing `tailscale_auth_key` handling code (the legacy env-var-or-prompt resolution in
`vms/initializer.py`) is removed; the kwarg is the only path.

### Git credentials (Phase 1)

Schema change: `git_credentials.<name>` entries gain `token: str` (default
`"git-token-<credential-name>"`). `required_resources()` emits one `SecretRequirement` per entry.

Reference flow at `vm create`:

1. Validation pass: auto-declares any `git-token-<name>` secrets not operator-declared.
2. Manager-entry walks the admin config subgraph: `admin.config.git_credentials = ["github-prod"]`
   -> `git_credentials:github-prod` -> `secret:git-token-github-prod`. Same for agent template's
   git_credentials at `agent create`.
3. Orchestrator resolves the token secrets.
4. Manager passes resolved tokens as kwargs to the git-credentials install runner.

Existing `obtain_token` path in `agentworks.git_credentials.base` is removed in favor of the new
path. The framework's resolver is the only token producer.

## Origin tracking detail

### Operator-declared resources

The TOML loader captures `(file, line)` for each `[secrets.<name>]`, `[git_credentials.<name>]`,
`[vm_templates.<name>]`, etc. section's opening line. Stored on the resource's `origin` field.

### Auto-declared resources

After the validation pass synthesizes a resource, `Origin.source = requirements[0].source` -- the
first matching requirement's `(kind, name)` per config-load walk order. The "first matching" rule is
deterministic given that walk order.

The full set of matching requirement sources is not stored on `Origin`; the resource's `usage` list
(one entry per matching requirement) carries that data via its per-entry `source`. For default
templates that many things inherit from, the origin-display source is essentially
load-order-arbitrary within the referencing set; the per-requirement detail in `usage` provides the
complete picture for `agw secret describe`.

### Display

`agw doctor`'s Secrets group: per-secret origin string with relevant detail
(`operator-declared (config.toml:42)` or `auto-declared by vm_template:azure-prod`).

`agw secret list`: `Origin` column with the same shape.

`agw secret describe`: origin rendered with full detail; all sources listed in the Usages section.

## Phasing (for the plan)

The plan will phase the work; the full design above is the target. Anticipated shape:

1. **Phase 1a: Framework foundations.** `resources/` package with `ResourceRequirement`,
   `ResourceKind`, `Origin`, validation pass with cycle detection, kind registry. `SecretKind`
   implementation. Existing `SecretDecl` augmented with `origin` and `usage` fields. No consumer
   wiring yet.
2. **Phase 1b: Env-block migration.** `EnvEntry`'s secret-ref form emits `SecretRequirement` via
   `required_resources()`. Validation pass auto-declares missing secrets. Existing strict "must
   declare" error behavior is removed; doctor surfaces auto-declared secrets so the visibility
   intent is preserved. **Lands before Tailscale/git-creds** so the framework has a real producer of
   `SecretRequirement` exercised end-to-end before the system-secret migrations build on it.
3. **Phase 1c: Tailscale migration.** VM template schema gains `tailscale_auth_key`. Manager-entry
   at `vm create` / `vm reinit` walks the subgraph, resolves via the orchestrator, threads the value
   as a kwarg. Legacy resolution path removed.
4. **Phase 1d: Git-credentials migration.** `git_credentials.<name>.token` field; same flow as
   Tailscale, threaded into the git-credentials install runner. Legacy `obtain_token` path removed.
5. **Phase 1e: `agw secret describe`.** CLI command; service-layer logic in `agentworks.secrets`.
   Displays origin, usages, per-backend mapping status, resolution preview.
6. **Phase 2a: Template kinds.** `VMTemplateKind`, `WorkspaceTemplateKind`, `AgentTemplateKind`,
   `SessionTemplateKind` implementations. Inheritance moves into the framework. Built-in defaults
   migrate from the existing resolver fallback into `synthesize()`. **Operator-facing behavior is
   unchanged** (per FRD R12); error messages get the framework's consistent shape. Partial overrides
   continue to flow through template `inherits` (an existing mechanism), not through any framework
   field-level merge -- the framework doesn't do field-level merging at all.
7. **Phase 2b: Catalog and provider kinds.** `CatalogKind`, `GitCredentialProviderKind`,
   `SecretBackendKindKind` (yes, the redundant name -- a kind named `secret_backend_kind`). Existing
   bespoke validation removed in favor of framework dispatch.
8. **Phase 2c: `agw resource list` / `agw resource describe`.** CLI command group.

Each phase ends at a green CI and a usable intermediate state. Phase 0 through 1e ship as one PR per
phase on the `feat/resource-registry-sdd` branch, each merged after its reviewer pass; Phase 2 is a
follow-up PR sequence on a separate branch.

## Design decisions

### One package, one validation pass

`agentworks.resources` is a single package whose Registry exposes a publish/validate API (plus the
`Registry.from_config(config)` convenience). Alternatives considered:

- Distributing the dispatch logic across the existing config types (each type's `__post_init__`
  validates its own references). Rejected because it scatters validation logic and makes cycle
  detection hard. The framework's value is in centralizing reference-checking.
- Wrapping the existing per-type validation in adapters. Rejected because the existing validation
  differs by type in ways the framework wants to unify (auto-decl vs. error, cycle detection, error
  message shape).

The package owns dispatch; existing types own their fields and `required_resources()` method.

### Kind-as-strategy, registered in a module-level dict

Each kind's logic (miss policy, name restrictions, synthesize) lives in one implementation
registered in a `KIND_REGISTRY` dict. Adding a new kind is one new module under `kinds/`.
Alternatives considered:

- One class per resource type with abstract methods. Rejected as heavier; the strategy pattern is
  enough.
- Plugins / entrypoints for kinds. Rejected as premature -- agentworks doesn't have a plugin system
  yet; the dict can become a plugin registry later without changing the protocol.

### `tomlkit` for line tracking

The framework's `operator-declared` origin variant carries `(file, line)`, and the stdlib `tomllib`
does not expose line info. The loader switches to `tomlkit`, which exposes positions via its item
API and is actively maintained. Mechanical change to the parse step; rest of `config.py` doesn't
change shape.

### Origin is set once

`Origin` is immutable per resource. Reinit doesn't mutate origin -- a fresh registry is built on
every config load. This matches the FRD's "resource removal is automatic" property (the registry is
recomputed; stale resources don't persist).

### Transitive walk is per-command

The `collect_secrets_for(registry, root)` helper is called by manager-entry code per command, not by
the framework. The framework's job ends when the registry is built. Per-command scoping lives in the
manager layer because it's command-aware (knows which resource is being provisioned).

### Migration is a hard cutover

Per the FRD's non-goals: no deprecation warnings for `AW_TAILSCALE_AUTH_KEY` /
`AW_GIT_CREDENTIALS_*`. Legacy resolution paths are deleted, not gated. Operators upgrading follow
the migration notes once; release notes carry the change.

## Interaction with existing systems

### `agentworks.secrets`

The orchestrator's `resolve_for_command(extra_decls=...)` interface is the integration point; no
changes to the orchestrator itself. `SecretDecl` gains `origin: Origin` and `usage: list[str]`
fields. The resolver, backend chain, prompt logic are untouched.

### `agentworks.env`

`EnvEntry`'s secret-ref form emits a `SecretRequirement`. Existing env-block resolution logic (merge
across scopes, identity vars, SetEnv) is unchanged.

### Existing template inheritance resolution

Today's inheritance resolver in `agentworks.config` (`_resolve_template`-style helpers) stays for
Phase 1. Phase 2 hoists the inheritance walk into the framework via `TemplateRequirement`. The
per-type field-merging logic that today combines parent and child template fields stays at the
template-inheritance layer; the framework does no field-level merging. The resolver shrinks to a
walker that follows `inherits` chains validated by the framework and combines fields per the
existing rules.

### `git_credentials` legacy

`agentworks.git_credentials.base.obtain_token` is removed in Phase 1d (the token now comes from the
resolver). The `git_credentials` package keeps:

- Provider classes (`github.py`, `azdo.py`) and their `credential_lines(token=...)` formatting
  methods that produce the `https://x-access-token:<token>@github.com` lines written to
  `~/.git-credentials`.
- The `GitCredentialProvider` base class and the provider-name -> class registry that maps the
  `type` field on a `[git_credentials.<name>]` entry to an implementation.

Phase 2b folds the provider-name registry into the framework's `GitCredentialProviderKind`;
formatting stays per-provider.

### DB schema impact

None. The registry is config-load state, not DB state.

## Open questions / for LLD

- **Per-kind error message templates**: format strings live in each kind's module; LLD lays out the
  exact strings.
- **Phase 2 default-template `synthesize()` source-of-truth**: the existing built-in defaults live
  in the resolver. LLD picks the migration mechanism (verbatim port vs. cleanup).
- **`agw secret describe` resolution-preview cost**: the preview calls each backend's
  `would_attempt(secret)` (existing); negligible. Verify no I/O for 1Password / vault backends in
  the preview path.
