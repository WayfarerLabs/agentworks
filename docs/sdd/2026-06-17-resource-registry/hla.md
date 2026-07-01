# Resource registry: high-level architecture

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/resources/`

## Overview

The framework lands as a new `agentworks.resources` package that introduces a **second layer**
between the existing `agentworks.config` parser and the runtime / manager layers. Two layers,
distinct responsibilities:

- **`Config`** (existing, in `agentworks/config.py`): the parsed source. Composes Resources from
  on-disk TOML sections (top-level + sub-sections) and attaches a Config-layer
  `declared_at: SourceLocation` to each. Today's `tomllib`-based parser plus the Phase 0 additions
  (a regex section-line scanner over the raw text for `(file, line)` capture, composition).
- **`Registry`** (new, in `agentworks/resources/registry.py`): the framework's typed, queryable
  Resource store. **Exists independently of any particular source**; starts empty. Multiple
  publishers contribute: `config.publish_to(registry)` for operator-declared Resources from TOML,
  `catalog.publish_to(registry)` (Phase 2b) for code-declared catalog commands, and future
  publishers for plugins / manifests. After publishers finish, `registry.finalize()` runs the
  framework pass: auto-declares missing references, attaches `usage`, detects cycles, and freezes
  the Registry. A small application-level `build_registry(config)` free function (in
  `agentworks/bootstrap.py`) orchestrates the standard publishers so most call sites get a finalized
  Registry in one call.

The framework operates on `Registry`. Manager-entry code consumes `Registry` where it needs
framework queries (e.g., reference subgraph walks for eager-resolve); other call sites continue
taking `config: Config` and migrate gradually as needed.

Existing config types (`SecretDecl`, `VMTemplate`, `GitCredentialConfig`, ...) gain a
`declared_at: SourceLocation` field at the Config layer; the Registry's copies gain `origin`
(framework type, translated from `declared_at` at publish) and `references` (populated during
finalize). Each config type that references other resources by name implements a
`referenced_resources()` method emitting one `ResourceReference` per reference; the Registry
consumes them during finalize.

```text
+----------------------+          +-----------------------------+
|  agentworks.config   |  publish |  agentworks.resources       |
|  (operator-declared) |--------->|  - ResourceReference        |<-+
|  - parses TOML       |          |  - ResourceKind / Origin    |  |
|  - composes Resources|          |  - Registry (empty)         |  | per-kind logic
|  - declared_at       |          |    * .add(.., origin)       |  | SecretKind etc.
+----------------------+          |    * .finalize() runs the   |  |
+----------------------+ publish  |      framework pass:        |--+
|  agentworks.catalog  |--------->|        - walks refs         |
|  (code-declared,     |          |        - auto-declares      |
|   Phase 2b)          |          |        - attaches references     |
+----------------------+          |        - detects cycles     |
   (future publishers:            +--------------+--------------+
   plugins, manifests, ...)                      |
                                                 v
                                  +--------------+--------------+
                                  |  Registry (finalized):      |
                                  |  - secrets[name]            |
                                  |  - vm_templates[name]       |
                                  |  - git_credentials[name]    |
                                  |  - admin_template[default]  |
                                  |  - named_console_template[..|
                                  |  - apt_packages[name]       |
                                  |  - system_install_commands  |
                                  |  - user_install_commands    |
                                  |  ... each with origin+references |
                                  +--------------+--------------+
                                                 |
                              +------------------+------------------+
                              |                  |                  |
                              v                  v                  v
                      +---------------+   +---------------+   +-----------------+
                      | agw doctor    |   | manager-entry |   | eager-resolve   |
                      | agw secret    |   | walks subgraph|   | (existing       |
                      |   list/desc.  |   | for command-  |   |  orchestrator   |
                      | (Phase 1)     |   | scoped secrets|   |  + extra_decls) |
                      | agw resource  |   +---------------+   +-----------------+
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
  reference.py           # ResourceReference, kind-specific subclasses
  origin.py              # Origin dataclass + factory helpers
  kind.py                # ResourceKind protocol; miss-policy machinery
  registry.py            # finalize pass: walk, dispatch, cycle-detect, attach metadata
  kinds/
    __init__.py
    secret.py                  # SecretKind (auto-declare any name) -- Phase 1a
    admin_template.py          # AdminTemplateKind (singleton; reserved 'default') -- Phase 1a
    named_console_template.py  # NamedConsoleTemplateKind (singleton; reserved 'default') -- 1a
    vm_template.py             # VMTemplateKind (reserved 'default') -- Phase 2a
    # ... more in Phase 2
```

Why a new package rather than extending `agentworks.config`: the framework has its own lifecycle
(walk, dispatch, attach metadata, cycle-detect) and clear boundaries with the parser. Keeping it
separate keeps `config.py` focused on parsing, mirrors how `agentworks.env` and `agentworks.secrets`
extracted env/secret concerns into their own packages, and makes `Registry` substitutable when the
source format changes (see "Future: YAML manifests" below).

The `kinds/` subdirectory holds three kinds in Phase 1a (`secret`, `admin_template`,
`named_console_template`) and grows to roughly a dozen in Phase 2 as template inheritance, catalog
commands, git-credential providers, and secret backends migrate into the framework. Starting with
the subdirectory avoids a churn-rename later.

## Two layers: Config and Registry

The framework introduces a deliberate split between **what the operator typed** (`Config`, parsing
layer) and **what the framework sees** (`Registry`, runtime layer).

### `Config` (parsing layer)

- Lives in `agentworks/config.py`. Unchanged location and class name. No mechanical rename.
- Parses TOML and **composes Resources** from top-level + sub-section pairs. Output is per-kind
  dicts of Resources, not raw TOML sections: `Config.secrets: dict[str, Secret]`,
  `Config.vm_templates: dict[str, VMTemplate]`, `Config.git_credentials: dict[str, GitCredential]`,
  plus singletons `Config.admin` and `Config.named_console` and `Config.secret_config` (the last is
  the rename target's `Config.secret_system`; see "Naming follow-up" below -- the rename is
  deferred). The Resource types (today named `VMTemplate`, `AdminConfig`, `NamedConsoleConfig`,
  `SecretConfig`, `SecretBackendConfig`, `GitCredentialConfig`) **are the Resources** -- they happen
  to live in `config.py` today; their naming is a minor cleanup item (see "Naming follow-up" below)
  but the framework treats them as Resources regardless.
- **Singletons publish as one-row kinds.** Two of the Config-layer singletons -- `Config.admin` and
  `Config.named_console` -- translate into one-row Registry kinds at publish: `admin_template` with
  the reserved name `default`, and `named_console_template` with the reserved name `default`. (The
  third singleton, `Config.secret_config`, is the secret-system policy config consumed directly by
  `agentworks.secrets`; its individual `secret_backends` entries are full Registry Resources from
  Phase 2b on, but the `secret_config` envelope itself doesn't publish as a kind.) Config schema is
  unchanged (`[admin.env]`, `[admin.git_credentials]`, `[named_console]` work exactly as today);
  only the Registry-side modeling treats `admin` / `named_console` as first-class Resources so they
  appear in `agw resource list`, can be the source of auto-declared secrets (e.g.,
  `[admin.env] = { MY_VAR = { secret = "..." } }` emits
  `SecretReference(source=("admin_template", "default"))`), and route through framework dispatch
  uniformly. If a config omits all `[admin.*]` sections, Config still publishes an empty-defaults
  `admin_template:default`; same for `named_console_template:default`. There is no auto-decl OF
  admin or named_console themselves -- only their referenced secrets can auto-declare.
- **Accepts TOML's implicit-parent semantics** (FRD R2). A sub-section like `[vm_templates.x.env]`
  declared without a separate `[vm_templates.x]` header still composes into a valid (if minimal)
  `vm_templates.x` Resource with only the env field populated. No orphan-rejection check; the
  framework treats every operator-typed `(<container>, <name>)` pair as one Resource regardless of
  which TOML header line introduced it.
- Each composed Resource carries a Config-layer `declared_at: SourceLocation` field, where
  `SourceLocation = (file: Path, line: int)` is defined in `agentworks/source_location.py` (a
  lightweight sibling module to `config.py`; `config.py` is already past the project's 1000-line
  soft target and types like `SecretDecl` that live outside `config.py` need to import
  `SourceLocation` too, which would create a circular import if it lived in `config.py`). This is
  the Config layer's own representation of "where the operator declared this Resource" -- the
  framework's `Origin` type is a separate concept owned by the Registry layer (see below). For a
  Resource composed from multiple TOML sections, `declared_at` points at the earliest section header
  (by line) that contributed to the Resource -- typically the `[<container>.<name>]` header, or the
  first sub-section header if no root header exists. Config does not depend on
  `agentworks.resources`.
- The existing `load_config()` function and `Config` class stay; all current imports continue to
  work.

### `Registry` (framework layer)

- Lives in `agentworks/resources/registry.py`. New class introduced by this SDD.
- **The Registry exists independently of any particular source.** It starts empty; publishers add
  Resources to it; after all publishers have contributed, the Registry validates itself. Resources
  arriving in the Registry carry one of three Origin variants depending on the publisher:
  `operator-declared` (Config, future operator-typed publishers), `code-declared` (catalog, future
  code publishers like plugins), or `auto-declared` (synthesized inside `finalize()` by a kind's
  miss policy). Multiple publishers contribute independently:
  - **Config publisher** (`config.publish_to(registry)`): publishes operator-declared Resources
    parsed from TOML. Constructs `Origin.operator_declared(file=..., line=...)` from each Resource's
    `declared_at: SourceLocation`.
  - **Catalog publisher** (`catalog.publish_to(registry)`, Phase 2b): publishes code-defined catalog
    commands (apt packages, system / user install commands). Constructs
    `Origin.code_declared(source="agentworks.catalog")` for each entry.
  - **Future publishers** (plugins, manifests, ...): use the same `Registry.add(...)` API with their
    own Origin variant. The Registry doesn't care about publisher identity beyond the Origin carried
    by each Resource.

- **Publish API**: `Registry.add(kind: str, name: str, resource: Resource, origin: Origin)`.
  Per-Resource, not per-kind-dict. Each publisher constructs the right `Origin` variant and passes
  it in; the Registry just stores. This keeps the Registry agnostic of publisher internals.
- **Finalize phase**: after all publishers have contributed, `registry.finalize()` runs the
  framework pass: walks the reference graph, dispatches miss policies for references that don't
  resolve to a published Resource, synthesizes auto-declared Resources (with
  `Origin.auto_declared(source=...)` from the kind's `synthesize`), attaches the `usage` list to
  each Resource, detects cycles, and freezes the Registry. The Registry is mutable during publish;
  `finalize` makes it immutable. The name captures the whole lifecycle terminator -- not just
  validation but also the auto-declaration synthesis that the framework's miss policies trigger.
- **Convenience entry point** for the common Config + catalog case: a free function
  `build_registry(config)` in `agentworks/bootstrap.py` (the application-level glue module that
  knows the standard set of publishers; the Registry itself stays publisher-agnostic):

  ```python
  # agentworks/bootstrap.py
  from agentworks import catalog
  from agentworks.config import Config
  from agentworks.resources import Registry

  def build_registry(config: Config) -> Registry:
      registry = Registry.empty()
      catalog.publish_to(registry)   # code-declared catalog commands (Phase 2b)
      config.publish_to(registry)    # operator-declared Resources from TOML
      # future: plugins.publish_to(registry), manifests.publish_to(registry), ...
      registry.finalize()            # auto-declares, validates, freezes
      return registry
  ```

  Most call sites use this convenience. The lower-level `Registry.empty()` + `publish_to` +
  `finalize` triad is exposed for multi-source scenarios and tests that need to swap publishers.

- **Layering rule**: Config's parsing-and-composition logic doesn't depend on
  `agentworks.resources`. The one exception is `Config.publish_to(registry: Registry)` (Phase 1a),
  which is the explicit layer handoff and imports `Registry` (for the type hint) and `Origin` (to
  construct `Origin.operator_declared(...)`). Config's data structures themselves remain
  framework-ignorant; only the explicit publish handoff crosses the boundary.
- Exposes per-kind queries: `registry.secrets`, `registry.vm_templates`, etc. Each per-kind view
  contains operator-declared Resources (from publishers) **plus** auto-declared Resources
  synthesized during finalize. All Resources in `Registry` carry full `origin` (framework type) and
  `usage` metadata.
- The framework's lookup surface lives here: `registry.lookup(kind, name)`, iter helpers, subgraph
  walks for eager-resolve, the data backing `agw doctor` / `agw secret describe` /
  `agw resource list|describe`. Surface sufficient for `collect_secrets_for(registry, root)`:
  `lookup` resolves the root and each transitive target; each Resource's `referenced_resources()`
  provides the edges to walk.

**Copy, not mutate (across layers).** Resources in `Registry` are distinct objects from their
`Config` counterparts. The Registry's Resource has `origin: Origin` (framework type, translated from
Config's `declared_at` at publish time) and `usage: list[ReferenceEntry]` (populated during
`finalize`). The `Config` layer's instances stay pristine and carry only `declared_at`. The two
layers hold distinct objects under the same name.

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
composition / `declared_at`-attachment from Phase 0):

- TOML parse errors (syntax, duplicate keys at the same path).
- Field types per the schema (`str`/`int`/`bool`/`list`/inline-table shapes).
- Required fields present per resource type.
- Name regex / kebab-case validation for operator-typed identifiers.
- Operator-typed value validation (e.g., URL formats, enum values like
  `git_credentials.type in {"github", "azdo"}`).
- **Resource composition**: top-level + sub-section pairs combined into single Resource instances
  per kind. TOML's implicit-parent semantics apply unchanged (a sub-section alone is a valid
  Resource with default body fields).
- **`declared_at` attachment**: each operator-declared Resource gets
  `declared_at: SourceLocation(file=..., line=...)` set at construction time from a regex pass over
  the raw TOML text (the stdlib `tomllib` parser doesn't surface line info, and Phase 0 adds a small
  section-line scanner alongside it). `SourceLocation` is Config's own type; the framework's
  `Origin` is a Registry-layer concept constructed by each publisher (in `Config.publish_to`, the
  operator-declared `Origin` is built from `declared_at` before calling `registry.add(...)`).

**Registry-layer validation** (in `Registry.finalize`, new with this SDD; runs after all publishers
have contributed via `publish_to`):

- Requirement walks via each Resource's `referenced_resources()` (a Resource may declare it depends
  on others by `(kind, name)`).
- Miss policy dispatch (auto-declare with optional reserved-name restriction; error).
  Operator-declared Resources from publishers satisfy references directly; missing ones trigger the
  kind's miss policy.
- Reserved-name restrictions per kind (e.g., template kinds accept auto-decl only for `default`).
- **Origin attachment**: operator-declared Resources received
  `Origin(variant="operator-declared", file=..., line=...)` at publish time (Registry translated
  `declared_at` -> `Origin` then). Auto-declared Resources get
  `Origin(variant="auto-declared", source=...)` from the kind's `synthesize` during finalize.
- Usage attachment: each Resource accumulates a `usage` list with one entry per matching reference.
- Cycle detection across the reference graph.
- All semantic / cross-resource checks.

`ConfigError` from the Config layer carries parse-time context (file/line, field name).
`ConfigError` from the Registry layer carries framework context (kind, name, reference source). Same
exception type; consistent rendering at the CLI layer; the message body distinguishes.

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
N YAML manifests -> parse each -> manifest.publish_to(registry) -> registry.finalize()
```

The `Config` layer fragments (one parsed object per manifest file, or merged by `(kind, name)`) or
is replaced by a thinner parsed-manifests aggregate; the `Registry` interface stays identical. The
framework consumes the `Registry`, not the producer. The finalize pass -- miss policies, origin
attachment, cycle detection -- is the same regardless of source format.

`Origin` generalizes naturally: `Origin.file` is already a `Path` and works for any source. The
framework's API stays identical; `Origin` either keeps `line` (per-line manifests), makes it
optional (per-resource-per-file manifests where the whole file is the resource), or grows a new
variant. Implementation detail for a future SDD.

## Core types

### `ResourceReference`

A base immutable dataclass with kind-specific concrete subclasses. Producers
(`referenced_resources()` on each source type) return concrete subclasses (`SecretReference`,
`TemplateReference`, ...); the framework consumes them through the base class. Fields on the base:

- `name: str` -- target resource name (operator-overridable or fixed per the source's field).
- `kind: str` -- target resource kind identifier (`"secret"`, `"vm_template"`, ...). The same kind
  strings appear throughout the framework: `KIND_REGISTRY` keys, `Origin.source[0]`, error message
  kind labels. One canonical set.
- `usage: str` -- system-defined role per the FRD's sentence template. Frozen at reference
  construction time.
- `source: tuple[str, str]` -- `(kind, name)` of the declaring resource. The `kind` matches the
  declaring resource's kind (`"vm_template"` for `vm_templates.azure-prod`, `"git_credentials"` for
  `git_credentials.github-prod`); the `name` is the declaring resource's name.

Concrete subclasses add kind-specific fields the registry's auto-declare logic may use. Phase 1's
`SecretReference` adds none; the subclass exists so producers and the framework agree on the target
kind without dispatch on the `kind` string. Phase 2 subclasses (`TemplateReference`, etc.) carry
per-kind defaults.

Producers emit a flat list per call; the framework concatenates the lists.

### `ResourceKind`

A protocol implemented per kind. One instance per kind, registered in a module-level dict the
finalize pass consults:

- `kind: str` -- the kind identifier matching `ResourceReference.kind`.
- `miss_policy: Literal["auto-declare", "error"]` -- which branch the finalize pass takes when a
  reference points at a missing name.
- `auto_declare_names: AbstractSet[str] | None` -- when `miss_policy == "auto-declare"`, the set of
  names the kind accepts. `None` means "any name" (secrets). `{"default"}` means "only the reserved
  name `default`" (templates).
- `synthesize(references) -> Resource` -- called when a missing name is being auto-declared.
  Receives the full list of matching references (in config-load order). Produces the resource
  instance with whatever defaults the kind wants (empty `backend_mappings` for secrets, the kind's
  code-defined defaults for templates, ...).

The `kind.py` module exports a `KIND_REGISTRY` dict mapping `kind` strings to `ResourceKind`
instances. Kinds are registered as they migrate: Phase 1 registers only `SecretKind`; other kinds
keep their existing bespoke validation until Phase 2 brings them into the framework.

### `Origin`

Carried on every Resource. One dataclass with a variant tag matching the publisher type:

- `variant: Literal["operator-declared", "code-declared", "auto-declared"]`
- For `operator-declared` (Config or future operator-typed publishers): `file: Path` and `line: int`
  for the declaration's opening line.
- For `code-declared` (catalog, future code publishers like plugins): `source: str` -- a code-
  source identifier like `"agentworks.catalog"`. Catalog commands and similar code-published
  Resources eagerly exist regardless of operator config; they're published by a code source. The
  string shape is sufficient for Phase 2b; future plugin sources may warrant a structured
  `(package, version)` form, deferred to that SDD.
- For `auto-declared`: `source: tuple[str, str]` -- the first matching reference's source, per R1's
  config-load walk order.

Set once when the Resource is published into (or synthesized inside) the Registry; never mutated
afterwards. Each publisher constructs the right variant; the Registry stores it.

The full list of matching reference sources (for `agw secret describe`'s "also required by ..."
display) is derived from the resource's `usage` list, not stored separately on `Origin`. Each usage
entry carries the source of the reference that contributed it (see Terminology in the FRD), so
origin doesn't need to duplicate that data.

The loader is responsible for capturing `file` / `line` during TOML parsing. Python's stdlib
`tomllib` doesn't expose line info, and no add-on parser library (including `tomlkit`) surfaces
section-opening positions on the parsed objects either. The loader keeps `tomllib` for the actual
parse and adds a small regex pre-pass over the raw text that scans `[section]` / `[section.sub]`
headers and builds `dict[tuple[str, ...], int]` mapping each section's dotted path to its opening
line. Composition consults that map to attach `declared_at` to each Resource. The existing parsing
surface in `config.py` doesn't change shape; the regex scanner is additive.

## Resource composition (Config layer)

A Resource is the conceptual unit; TOML splits it across sections (a template's fields plus its
`.env` sub-section plus any future sub-tables). The Config layer parses TOML and composes Resources
from sections, publishing per-kind dicts of fully-composed Resources.

```python
def _compose_resources(parsed_sections, section_lines) -> Config:
    """Inside agentworks.config's load_config(). Walks the parsed TOML sections
    and builds per-kind dicts of composed Resources. `section_lines` is the
    regex scanner's output: dict[tuple[str, ...], int] mapping dotted section
    paths to opening-line numbers.

    For each multi-named kind whose sections support sub-tables (vm_templates,
    agent_templates, workspace_templates, session_templates, ...):
      - The parsed-TOML side yields every operator-typed (<container>, <name>)
        as one composed Resource, regardless of whether the operator wrote a
        [<container>.<name>] header explicitly or only sub-section headers
        underneath. TOML's implicit-parent semantics already produce the
        composed dict; we just consume it.
      - declared_at points at the earliest section-header line that contributed
        to the Resource. That is the line of [<container>.<name>] when present;
        otherwise the line of the first encountered [<container>.<name>.<sub>].
        The section_lines map answers both questions.
    """
```

Singletons are exceptions: their root declaration is neither required nor accepted. Today these are
`admin` (`[admin.config]`, `[admin.env]`, `[admin.git_credentials]`, ...), `named_console`
(`[named_console]` itself, no sub-tables today), and `secret_config` (`[secret_config.backends]` and
the like). Their sub-tables (or, for `named_console`, the single section itself) are valid without
any root.

Sub-section composition is additive: a Resource composed from `[vm_templates.x]` plus
`[vm_templates.x.env]` carries both sets of fields. No key collisions are possible by construction
(parent top-level fields are not under any sub-section).

For secrets, `backend_mappings` is typically dot-notation inside the `[secrets.<name>]` section
rather than a separate `[secrets.<name>.backend_mappings]` sub-section, so the composition step is
effectively a no-op for secrets in current practice.

**Pipeline**: `load_config()` parses TOML with `tomllib`, runs the regex scanner over the raw text
to build `section_lines`, composes Resources, attaches `declared_at: SourceLocation` to each ->
returns `Config` (a registry of operator-declared Resources with parse-time location). The Registry
starts empty; multiple publishers contribute: `catalog.publish_to(registry)` adds code-declared
catalog commands (Phase 2b); `config.publish_to(registry)` adds operator-declared Resources
(translating `declared_at` into `Origin.operator_declared(...)` before each `registry.add`); future
publishers (plugins, manifests) follow the same shape. `registry.finalize()` then adds auto-declared
Resources with `Origin.auto_declared(source=...)`, attaches `usage` lists, detects cycles, and
freezes the Registry -> queryable. `build_registry(config)` in `agentworks/bootstrap.py`
orchestrates the standard publishers; the lower-level `Registry.empty()` + `add` + `finalize` triad
is exposed for custom orchestration.

## Publish and finalize

The Registry's lifecycle has two phases. **Publish** accepts Resources from any source; the Registry
is mutable during this phase. **Finalize** runs the framework pass and locks the Registry; once
`finalize()` returns, the Registry is queryable but no longer mutable.

```python
# Registry-side: accept publishes (with each publisher's Origin), then finalize.
class Registry:
    def add(self, kind: str, name: str, resource: Resource, origin: Origin) -> None:
        """Add a Resource from any publisher. The publisher constructs the Origin
        variant appropriate to its source type and passes it in."""
        if self._frozen:
            raise RuntimeError("registry is frozen; add must precede finalize")
        self._resources.setdefault(kind, {})[name] = resource.with_origin(origin)

    def finalize(self) -> None:
        """Run the framework pass over already-published Resources. Materializes
        reserved-default names, walks the reference graph (iteratively, since
        synthesized resources can themselves emit references), dispatches miss
        policies, attaches references, detects cycles, and locks the Registry. After
        return, the Registry is queryable but no longer accepts publishes."""
        # 0. Materialize reserved-default names. Kinds whose ``auto_declare_names``
        # is a non-None set guarantee those names exist in the registry after
        # finalize, regardless of whether anything referenced them. Synthesizes
        # with ``references=()`` so the kind builds its code-defined default.
        # Closes the gap where an unreferenced default would otherwise crash at
        # command time. Kinds with ``auto_declare_names = None`` (secrets) are
        # untouched -- their resources remain reference-driven. Done before
        # the worklist loop so the first pass walks these resources alongside
        # the operator-published ones.
        for kind, kind_handler in KIND_REGISTRY.items():
            if kind_handler.auto_declare_names is None:
                continue
            for name in kind_handler.auto_declare_names:
                if name in self._resources.get(kind, {}):
                    continue
                self._resources.setdefault(kind, {})[name] = (
                    kind_handler.synthesize(references=())
                )

        # 1. Worklist loop. Walk referenced_resources() on every resource not yet
        # visited; for each newly-discovered (kind, name) target not in the
        # registry, dispatch the kind's miss policy. The miss handler may
        # synthesize a new resource whose own referenced_resources() the next
        # iteration walks. Loop until a pass adds no new resources -- a single
        # pass would silently drop synthesized resources' unresolved edges.
        # The accumulated reference map is preserved across iterations so
        # the post-loop usage-attachment pass (step 2) sees the complete graph.
        all_reqs: dict[tuple[str, str], list[ResourceReference]] = {}
        walked: set[tuple[str, str]] = set()
        while True:
            new_walks = self._collect_new_references(all_reqs, walked)
            if not new_walks:
                break
            # Dispatch miss policies for any targets not yet in the registry.
            for target, reqs in list(all_reqs.items()):
                target_kind, target_name = target
                if target_name in self._resources.get(target_kind, {}):
                    continue
                kind_handler = KIND_REGISTRY[target_kind]
                if kind_handler.miss_policy == "auto-declare":
                    allowed = kind_handler.auto_declare_names
                    if allowed is not None and target_name not in allowed:
                        raise ConfigError(...)  # reserved-name restriction
                    self._resources.setdefault(target_kind, {})[target_name] = (
                        kind_handler.synthesize(references=reqs)
                    )
                else:  # miss_policy == "error"
                    raise ConfigError(...)  # unknown name under error policy

        # 2. Usage attachment. Every resource with incoming references gets
        # its usage tuple set; the description-polish step (see Framework
        # metadata attachment) also runs here so auto-declared resources get
        # their synthesized descriptions before freeze.
        for (kind, name), reqs in all_reqs.items():
            existing = self._resources[kind][name]
            polished = existing.with_usage(_usage_list(reqs))
            polished = _polish_auto_declared_description(polished, kind)
            self._resources[kind][name] = polished

        # 3. Cycle detection across the now-complete reference graph.
        _detect_cycles(self._resources)
        self._frozen = True

    def _collect_new_references(
        self,
        all_reqs: dict[tuple[str, str], list[ResourceReference]],
        walked: set[tuple[str, str]],
    ) -> bool:
        """Walk referenced_resources() on every resource not yet in ``walked``,
        appending discovered references into ``all_reqs`` (preserving first-
        encountered order). Returns True if any resource was walked this pass.
        """
        any_walked = False
        snapshot = [
            ((kind, name), resource)
            for kind, kind_dict in self._resources.items()
            for name, resource in kind_dict.items()
            if (kind, name) not in walked
        ]
        for key, resource in snapshot:
            walked.add(key)
            any_walked = True
            for req in resource.referenced_resources():
                all_reqs.setdefault((req.kind, req.name), []).append(req)
        return any_walked

# Config-side: publish operator-declared Resources. Imports Origin to build the
# operator-declared variant; this is the explicit layer handoff.
class Config:
    def publish_to(self, registry: Registry) -> None:
        for kind, kind_dict in self.as_resource_dicts().items():
            for name, resource in kind_dict.items():
                origin = Origin.operator_declared(
                    file=resource.declared_at.file,
                    line=resource.declared_at.line,
                )
                registry.add(kind, name, resource, origin)

# Catalog-side (Phase 2b): publish code-declared catalog commands.
# `publish_to` lives in the agentworks.catalog module; import as `from agentworks
# import catalog` and call `catalog.publish_to(registry)`. Mirrors Config.publish_to.
def publish_to(registry: Registry) -> None:
    for kind, kind_dict in CODE_DEFINED_CATALOG.items():
        for name, resource in kind_dict.items():
            origin = Origin.code_declared(source="agentworks.catalog")
            registry.add(kind, name, resource, origin)

# Application-level convenience: lives in agentworks/bootstrap.py because the
# "standard set of publishers" is application knowledge, not Registry knowledge.
# Registry stays publisher-agnostic; Config stays unaware of catalog.
def build_registry(config: Config) -> Registry:
    registry = Registry.empty()
    catalog.publish_to(registry)  # code-declared catalog commands (Phase 2b)
    config.publish_to(registry)   # operator-declared Resources
    registry.finalize()
    return registry
```

Walk order for `references` is config-load order: top-to-bottom in the TOML file, top-level sections
in declaration order. Within each source's `referenced_resources()` call, references come back in
whatever order the source returns them (typically the order of fields in the schema).

### Cycle detection

A directed graph where nodes are `(kind, name)` and edges are references (source -> target). DFS
three-coloring (white = unvisited, gray = on stack, black = finished). Encountering a gray node
mid-walk yields a cycle; the implementation collects the path and surfaces it in a single
`ConfigError`.

Phase 1 exercises no cycles (secrets don't reference secrets). The check ships in Phase 1 for
completeness; Phase 2's template inheritance is where it earns its keep.

### Errors

All errors raised during the finalize pass are `ConfigError` (existing `agentworks.errors` type)
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
  first matching reference source). Never mutated.
- **`references`**: a list of `ReferenceEntry(source, usage)` pairs populated from all matching
  references, accumulated by the finalize pass. Each entry carries both the reference's source
  `(kind, name)` and its usage text. Operator-declared resources get the same references list
  attached as auto-declared ones; it's framework-collected, not operator-settable.
- **`description`** (auto-declared only, kinds with a `description: str` field only): when the field
  is falsy after the publish phase, the finalize pass sets it. Format details (usage-driven vs
  empty-usage cases) are pinned in FRD R9; the HLA's responsibility is the dispatch shape: empty
  `usage` -> empty-usage fallback, non-empty `usage` -> usage-driven format. The polish runs after
  `usage` attachment so it sees the complete reference set (or knows it's empty). Operator-declared
  descriptions are honored verbatim. Kinds without a `description` field skip the polish (no-op).
  The framework checks structurally (`hasattr(resource, "description")` + falsy test), not by kind,
  so any future kind that acquires a `description` field benefits automatically. This is the
  mechanism that makes `agw resource list`'s `description` column reliably populated across kinds
  (FRD R9 / R12). Producers carry the responsibility of writing good `usage` strings -- they're
  well-positioned to, because they know what the reference will be used for.

If an operator wants a partial override of a default template, they don't get it through field-level
merging on the `default` declaration. They declare a child template with `inherits = ["default"]`
and override fields there (existing template-inheritance mechanism).

Duplicate operator declarations of the same `(kind, name)` are TOML parse errors (duplicate keys at
the same path); the framework never sees them.

## Auto-declare details

### Secret kind

`SecretKind.synthesize(references)` builds a `SecretDecl` with:

- `name = references[0].name`
- `description = ""` initially; the finalize pass's description-polish step (see Framework metadata
  attachment + FRD R9) populates it from the first reference's \`(usage, source)\` before the
  registry freezes
- `hint = None`
- `backend_mappings = {}` (empty; the framework's default per-backend conventions (e.g.,
  `AW_SECRET_<NAME>`) apply at resolution time)
- `references = [ReferenceEntry(source=r.source, usage=r.usage) for r in references]` -- a list
  where each entry pairs the reference's source with its usage text. Duplicate text from different
  sources is preserved (different sources are different rows in `agw secret describe`);
  dedup-by-text happens at render time only where summary display calls for it.
- `origin = Origin(variant="auto-declared", source=references[0].source)`

`auto_declare_names = None` (any name accepted). Because `auto_declare_names` is None, secrets do
**not** participate in finalize's always-materialize pre-step -- a secret only exists if something
references it or the operator declared it. `SecretKind.synthesize` is therefore always called with
`references` non-empty; the `references[0].name` / `references[0].source` accesses are safe.

For operator-declared secrets, the parser produces a `SecretDecl` with empty `usage`; the finalize
pass then populates `usage` from the matching references using the same `ReferenceEntry` shape.
`usage` is framework-set in both cases (operator declarations cannot specify it).

### Template kinds (Phase 2)

`VMTemplateKind.synthesize(references)` builds a `VMTemplate` with the kind's code-defined defaults
(the same defaults currently encoded in the resolver's "implicit default" fallback, hoisted into one
place). When called with `references` non-empty (the incoming-reference path), the first reference.s
source is recorded as origin. When called with `references=()` (the always-materialize pre-step for
`default`), origin is `auto-declared` with the reserved synthetic source
`("framework", "always-materialize")` so the breadcrumb shows where the row came from. The resulting
`usage = ()`. The framework's description-polish (Framework metadata attachment + FRD R9) sets the
empty-usage description on this row.

`auto_declare_names = {"default"}` -- only the reserved name. The always-materialize pre-step in
finalize uses this set to determine which names to guarantee; any other missing name surfaces from a
`TemplateReference` and triggers an error.

Same shape applies for `WorkspaceTemplateKind`, `AgentTemplateKind`, `SessionTemplateKind`, plus
`AdminTemplateKind` (plurified from singleton to named-multi-instance in Phase 2a per FRD R12).
`NamedConsoleTemplateKind` stays singleton in Phase 2a; plurification waits for a future SDD if and
when there's an operator need. The always-materialize rule subsumes Config's synthesize-on-omit for
admin and named_console; Config-side cleanup to retire those paths is an optional follow-up (not
required for behavior; the synthesize-on-omit path becomes a no-op once the framework materializes
the default itself).

## Requirement sources

Each existing config type that references resources by name gets a `referenced_resources()` method.
Phase 1 sources:

- `SecretRefEnvEntry` (the `{ secret = "..." }` form of `EnvEntry`): emits one
  `SecretReference(name=<ref>, usage=<...>, source=(<scope>, <scope-name>))` per reference. The
  scope kind for env blocks on singleton-backed Resources is the singleton's kind name:
  `("admin_template", "default")` for `[admin.env]`, `("named_console_template", "default")` for
  `[named_console]` env entries. Multi-named templates use their natural source:
  `("vm_template", "<name>")`, `("agent_template", "<name>")`, etc. The `usage` text is derived from
  the env-block context (e.g., `"the ANTHROPIC_API_KEY env var"`).
- `VMTemplate.tailscale_auth_key`: emits `SecretReference` with `source=("vm_template", <name>)` and
  `usage="the Tailscale auth key"`.
- `GitCredentialConfig.token`: emits `SecretReference` with `source=("git_credentials", <name>)` and
  `usage="the auth token"` (or similar; usage phrasing follows the Terminology sentence-template
  test).
- `AdminConfig.git_credentials` / `AgentTemplate.git_credentials` lists: each named credential is a
  reference; emits a `GitCredentialRequirement` (Phase 2-shaped; Phase 1 still uses bespoke
  validation for the list but the references are emitted so the orchestrator's transitive walk
  works).

Phase 2 adds:

- Template `inherits = [...]` references: emit `TemplateReference` per parent.
- `apt_packages` / `system_install_commands` / `user_install_commands` references: emit
  `CatalogRequirement` per entry.
- `git_credentials.*.type` references: emit `ProviderRequirement`.
- `[secret_backends.<kind>]` and `secret_config.backends` references: emit `BackendKindRequirement`.

## Per-command eager-resolve scope

Registry construction is universal (config load builds the whole registry). Eager-resolve scope is
per-command, driven by the reference subgraph rooted at the resource being provisioned.

```text
manager-entry  -->  resource-being-provisioned  -->  transitively walk referenced_resources()
                                                     in the (already-built) registry
                                                  --> collect SecretDecls
                                                  --> pass as extra_decls to
                                                      orchestrator.resolve_for_command(...)
```

The orchestrator's `extra_decls` parameter was left in place by the env-and-secrets SDD as the
migration hook. This SDD wires it up.

### Transitive walk

A small helper in `resources/__init__.py`. Phase 1 ships the secret-specific form below; the
underlying walk is kind-agnostic (DFS over `referenced_resources()`, dedupe by `(kind, name)`), so
Phase 2 can add sibling helpers (or a generic `collect_resources_for(..., target_kind=...)`)
trivially. Choosing the more specific surface for Phase 1 keeps the call-site API obvious.

```python
def collect_secrets_for(
    registry: Registry,
    root: tuple[str, str],
) -> list[SecretDecl]:
    """Walk referenced_resources() depth-first from root; collect Secret resources."""
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
- Origin detail: file path and line for operator-declared, reference source for auto-declared.
- Referenced by: one row per matching reference.
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
`referenced_resources()` on a resolved `VMTemplate` emits one `SecretReference`.

Flow at `vm create`:

1. Registry loads; the framework's finalize pass auto-declares `secret:tailscale-auth-key` if no
   operator block exists.
2. Manager-entry walks the VM template subgraph; collects the `tailscale-auth-key` SecretDecl.
3. Orchestrator's `resolve_for_command(extra_decls=[<that SecretDecl>])` resolves the value through
   the backend chain (prompting if no backend yields).
4. Manager passes the resolved value as a kwarg to `_install_tailscale(...)`.

Existing `tailscale_auth_key` handling code (the legacy env-var-or-prompt resolution in
`vms/initializer.py`) is removed; the kwarg is the only path.

### Git credentials (Phase 1)

Schema change: `git_credentials.<name>` entries gain `token: str` (default
`"git-token-<credential-name>"`). `referenced_resources()` emits one `SecretReference` per entry.

Reference flow at `vm create`:

1. Finalize pass: auto-declares any `git-token-<name>` secrets not operator-declared.
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

After the finalize pass synthesizes a resource, `Origin.source = references[0].source` -- the first
matching reference's `(kind, name)` per config-load walk order. The "first matching" rule is
deterministic given that walk order.

The full set of matching reference sources is not stored on `Origin`; the resource's `usage` list
(one entry per matching reference) carries that data via its per-entry `source`. For default
templates that many things inherit from, the origin-display source is essentially
load-order-arbitrary within the referencing set; the per-reference detail in `usage` provides the
complete picture for `agw secret describe`.

### Display

`agw doctor`'s Secrets group: per-secret origin string with relevant detail
(`operator-declared (config.toml:42)` or `auto-declared by vm_template:azure-prod`).

`agw secret list`: `Origin` column with the same shape.

`agw secret describe`: origin rendered with full detail; all sources listed in the Usages section.

## Phasing (for the plan)

The plan will phase the work; the full design above is the target. Anticipated shape:

1. **Phase 1a: Framework foundations.** `resources/` package with `ResourceReference`,
   `ResourceKind`, `Origin`, finalize pass with cycle detection, kind registry. `SecretKind`,
   `AdminTemplateKind`, `NamedConsoleTemplateKind` implementations -- the two singleton-backed kinds
   ship in 1a because Phase 1b (env-block secret refs from `admin.env`) needs the
   `admin_template:default` Resource present in the Registry to walk references from it. Config's
   `publish_to` translates each singleton into a one-row Registry entry (`(admin_template, default)`
   and `(named_console_template, default)`). Existing `SecretDecl`, `AdminConfig`,
   `NamedConsoleConfig` augmented with `origin` and `usage` fields. No env-block / system-secret
   producers of `SecretReference` wired yet (those land in Phase 1b/1c/1d).
2. **Phase 1b: Env-block migration.** `EnvEntry`'s secret-ref form emits `SecretReference` via
   `referenced_resources()`. Finalize pass auto-declares missing secrets. Existing strict "must
   declare" error behavior is removed; doctor surfaces auto-declared secrets so the visibility
   intent is preserved. **Lands before Tailscale/git-creds** so the framework has a real producer of
   `SecretReference` exercised end-to-end before the system-secret migrations build on it.
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

### One package, one finalize pass

`agentworks.resources` is a single package whose Registry exposes a publish/finalize API. The
standard-set orchestrator `build_registry(config)` lives in `agentworks/bootstrap.py` (not on
Registry) so the Registry stays publisher-agnostic. Alternatives considered:

- Distributing the dispatch logic across the existing config types (each type's `__post_init__`
  validates its own references). Rejected because it scatters validation logic and makes cycle
  detection hard. The framework's value is in centralizing reference-checking.
- Wrapping the existing per-type validation in adapters. Rejected because the existing validation
  differs by type in ways the framework wants to unify (auto-decl vs. error, cycle detection, error
  message shape).

The package owns dispatch; existing types own their fields and `referenced_resources()` method.

### Kind-as-strategy, registered in a module-level dict

Each kind's logic (miss policy, name restrictions, synthesize) lives in one implementation
registered in a `KIND_REGISTRY` dict. Adding a new kind is one new module under `kinds/`.
Alternatives considered:

- One class per resource type with abstract methods. Rejected as heavier; the strategy pattern is
  enough.
- Plugins / entrypoints for kinds. Rejected as premature -- agentworks doesn't have a plugin system
  yet; the dict can become a plugin registry later without changing the protocol.

### Regex section-line scanner instead of a parser swap

The framework's `operator-declared` origin variant carries `(file, line)`. The stdlib `tomllib` does
not expose line info; the leading add-on libraries (`tomlkit`, `tomli_w`, etc.) don't either --
`tomlkit.items.Table.trivia` only carries whitespace and comments, with no source position. Rather
than take on a dep purely for an API tomlkit doesn't actually have, Phase 0 keeps `tomllib` and adds
a ~30-line regex pre-pass over the raw text that builds `dict[tuple[str, ...], int]` mapping each
`[section]` / `[section.sub]` header's dotted path to its opening line. Composition then attaches
`declared_at` from that map. The scanner uses standard TOML header grammar (bare keys plus quoted
segments); agentworks configs in practice use only bare keys.

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

`EnvEntry`'s secret-ref form emits a `SecretReference`. Existing env-block resolution logic (merge
across scopes, identity vars, SetEnv) is unchanged.

### Existing template inheritance resolution

Today's inheritance resolver in `agentworks.config` (`_resolve_template`-style helpers) stays for
Phase 1. Phase 2 hoists the inheritance walk into the framework via `TemplateReference`. The
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
