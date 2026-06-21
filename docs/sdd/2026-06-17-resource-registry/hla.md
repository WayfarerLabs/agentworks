# Resource registry: high-level architecture

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/resources/`

## Overview

The framework lands as a new `agentworks.resources` package that sits beneath the existing config
loader and beside `agentworks.secrets` / `agentworks.env`. It introduces three core types --
`ResourceRequirement`, `ResourceKind`, and an `Origin` record on every resource -- plus a validation
pass that runs at the end of `config.load_config()` to walk requirements, dispatch miss policies,
build the registry, and detect cycles.

Existing config types (`SecretDecl`, `VMTemplate`, `GitCredentialEntry`, ...) gain an `origin` field
and the secret-bearing ones gain a `usage` list. Each config type that references other resources by
name implements a `required_resources()` method emitting one `ResourceRequirement` per reference;
the validation pass consumes them.

```text
+----------------------+     +----------------------------+     +----------------------+
|  config.py loader    |---->|  agentworks.resources      |<----|  per-kind logic      |
|  - parses TOML       |     |  - ResourceRequirement     |     |  - SecretKind        |
|  - emits raw types   |     |  - ResourceKind protocol   |     |  - VMTemplateKind    |
|  - calls validate()  |     |  - Origin                  |     |  - GitCredentialsKind|
+----------------------+     |  - validation pass         |     |  ...                 |
                             |    * walks requirements    |     +----------------------+
                             |    * applies miss policies |
                             |    * detects cycles        |
                             |    * sets Origin           |
                             +-------------+--------------+
                                           |
                                           v
                             +-------------+--------------+
                             |  Config (the registry):    |
                             |  - secrets[name]           |
                             |  - vm_templates[name]      |
                             |  - git_credentials[name]   |
                             |  ... each with origin set  |
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
  registry.py            # validation pass: walk, dispatch, cycle-detect, merge
  kinds/
    __init__.py
    secret.py            # SecretKind (auto-declare any name)
    vm_template.py       # VMTemplateKind (Phase 2: auto-declare reserved 'default')
    # ... more in Phase 2
```

Why a new package rather than extending `agentworks.config`: the registry framework has its own
lifecycle (walk, dispatch, merge, cycle-detect) and clear boundaries with the loader. Keeping it
separate makes the validation pass a self-contained step the loader invokes, mirrors how
`agentworks.env` and `agentworks.secrets` extracted env/secret concerns from the loader, and keeps
`config.py` thin.

## Core types

### `ResourceRequirement`

The structural protocol:

- `name: str` -- target resource name (operator-overridable or fixed per the source's field).
- `kind: str` -- target resource kind. The string identifier (`"secret"`, `"vm_template"`, ...);
  matches the `ResourceKind.kind` attribute. Used as the dispatch key during the validation pass.
- `usage: str` -- system-defined role per the FRD's sentence template. Frozen at requirement
  construction time.
- `source: tuple[str, str]` -- `(kind, name)` of the declaring resource. The `kind` matches the
  declaring resource's kind (`"vm_template"` for `vm_templates.azure-prod`, `"git_credentials"` for
  `git_credentials.github-prod`); the `name` is the declaring resource's name.
- Kind-specific extras: subclasses (`SecretRequirement`, `TemplateRequirement`, ...) carry
  additional fields the registry's auto-declare logic may use. Phase 1 has no `SecretRequirement`
  extras; the slot is reserved for Phase 2 kinds.

Stored as immutable dataclasses. Producers (the `required_resources()` method on each source type)
emit a flat list per call; the framework concatenates the lists.

### `ResourceKind`

A protocol implemented per kind. One instance per kind, registered in a module-level dict the
validation pass consults:

- `kind: str` -- the kind identifier matching `ResourceRequirement.kind`.
- `miss_policy: Literal["auto-declare", "error"]` -- which branch the validation pass takes when a
  requirement points at a missing name.
- `auto_declare_names: AbstractSet[str] | None` -- when `miss_policy == "auto-declare"`, the set of
  names the kind accepts. `None` means "any name" (secrets). `{"default"}` means "only the reserved
  name `default`" (templates).
- `synthesize(requirement) -> Resource` -- called when a missing name is being auto-declared.
  Produces the resource instance with whatever defaults the kind wants (empty backend_mappings for
  secrets, the kind's code-defined defaults for templates, ...). Receives the first matching
  requirement.
- `merge_operator(operator_resource, requirement) -> Resource` -- called when both an
  operator-declared resource and one or more requirements exist for the same name. Implements
  per-field merge (R3): operator fields win; unspecified fields fall back to what `synthesize` would
  have produced; the `usage` list accumulates across all matching requirements.

The `kind.py` module exports a registry-of-kinds dict that maps `kind` strings to `ResourceKind`
instances. Phase 1 ships `SecretKind` (and stub registrations for the other kinds whose Phase 2
behavior is "use existing bespoke validation; framework dispatch is a passthrough" so that the
dispatch table is complete).

### `Origin`

Carried on every resource. One dataclass with a variant tag:

- `variant: Literal["operator-declared", "auto-declared"]`
- For `operator-declared`: `file: Path` and `line: int` for the declaration's opening line.
- For `auto-declared`: `source: tuple[str, str]` -- the first matching requirement's source, per
  R1's config-load walk order. Also captured: the list of all matching requirement sources, surfaced
  in describe output but not used for origin identity.

Set once when the resource is added to the registry; never mutated afterwards.

The loader is responsible for capturing `file` / `line` during TOML parsing. Python's stdlib
`tomllib` does not expose line info; the implementation either switches to `tomlkit` (which does) or
uses `tomli`-w-positions equivalent. This is an HLA-level decision; LLD picks the exact library.

## Validation pass

The validation pass runs as the last step of `config.load_config()`, after all TOML sections are
parsed into raw types but before the `Config` object is returned. Conceptually:

```python
def _run_validation_pass(config: Config) -> None:
    # 1. Collect all requirements
    requirements: list[ResourceRequirement] = []
    for resource in config.iter_all_resources():
        requirements.extend(resource.required_resources())

    # 2. Group by (kind, name); preserve first-encountered ordering for origin recording
    by_target: dict[tuple[str, str], list[ResourceRequirement]] = {}
    for req in requirements:
        by_target.setdefault((req.kind, req.name), []).append(req)

    # 3. For each (kind, name): existing-in-registry? auto-decl? error?
    for (kind, name), reqs in by_target.items():
        kind_handler = KIND_REGISTRY[kind]
        existing = config.lookup_resource(kind, name)
        if existing is not None:
            # Operator-declared: per-field merge across requirements
            merged = kind_handler.merge_operator(existing, reqs)
            config.replace_resource(kind, name, merged)
        else:
            # Missing: dispatch miss policy
            match kind_handler.miss_policy:
                case "auto-declare":
                    if kind_handler.auto_declare_names is None or name in kind_handler.auto_declare_names:
                        synthesized = kind_handler.synthesize(reqs)
                        config.add_resource(kind, name, synthesized)
                    else:
                        raise ConfigError(...)  # reserved-name restriction violated
                case "error":
                    raise ConfigError(...)

    # 4. Cycle detection across the now-complete requirement graph
    _detect_cycles(by_target)
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
  `ConfigError(kind="vm_template", entity="azure-prod", source=("agent_template", "claude-exp"))`
  rendered as: `agent_template "claude-exp" references unknown vm_template "azure-prod"`.
- Reserved-name restriction violated:
  `ConfigError(kind="vm_template", entity="custom-base", reason="reserved-name-restriction")`
  rendered as: `vm_template kind only auto-declares the reserved name "default"; got "custom-base"`.
- Cycle detected: rendered as a path
  (`vm_template:azure-prod -> vm_template:base -> vm_template:azure-prod`).

## Per-field merge

When both an operator declaration and one or more requirements exist for the same `(kind, name)`:

1. Operator's resource is the starting point.
2. Origin stays `operator-declared` (with file:line preserved).
3. `usage` list is populated from all matching requirements (system-collected, not operator-set).
4. For operator-settable fields (`description`, `hint`, `backend_mappings`, kind-specific): the
   merge is per-field. Operator-specified fields win. Unspecified fields fall back to whatever the
   kind's `synthesize()` would have produced for the first matching requirement.
5. For `backend_mappings` specifically, the merge is per-key: operator-set keys win; unspecified
   keys fall back to the synthesized defaults. Per-key provenance (`operator-set` vs.
   `framework-default` vs. `backend-default-convention`) is recorded and surfaced via
   `agw secret describe`.

The per-field merge logic lives in each kind's `merge_operator()` implementation, not in the
framework's validation pass directly. This keeps kind-specific field knowledge inside the kind.

## Auto-declare details

### Secret kind

`SecretKind.synthesize(requirements)` builds a `SecretDecl` with:

- `name = requirements[0].name`
- `description = None` (operator-set field; auto-decl leaves blank)
- `hint = None`
- `backend_mappings = {}` (empty; the framework's default per-backend conventions (e.g.,
  `AW_SECRET_<NAME>`) apply at resolution time)
- `usage = [req.usage for req in requirements]` (deduplicated for display)
- `origin = Origin(variant="auto-declared", ...)` carrying `requirements[0].source` and the full
  `all_sources` list

No reserved-name restriction; any name is accepted.

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
- `GitCredentialEntry.token`: emits `SecretRequirement` with `source=("git_credentials", <name>)`
  and `usage="the auth token"` (or similar per the FRD's R7 sentence-template test).
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

A small helper in `resources/__init__.py`:

```python
def collect_secrets_for(
    registry: Config,
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

## CLI surfaces

### Phase 1: `agw secret describe`

New command. Service-layer logic in `agentworks.secrets` (or a thin `agentworks.resources.cli`
shim); CLI layer in `cli/agentworks/cli/secret/describe.py` following the existing pattern.

Output sections (per FRD R9):

- Header: name, kind, origin, description.
- Origin detail: file path and line for operator-declared, requirement source for auto-declared.
- Usages: one row per matching requirement.
- Backend mappings: merged table with per-key provenance.
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

Two-positional describe (kind + name) because names are unique only within a kind.

## Tailscale and git-credential migration shapes

### Tailscale (Phase 1)

Schema change: VM template gains `tailscale_auth_key: str` (default `"tailscale-auth-key"`).
`required_resources()` on a resolved `VMTemplate` emits one `SecretRequirement`.

Flow at `vm create`:

1. Config loads; the framework's validation pass auto-declares `secret:tailscale-auth-key` if no
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

After the validation pass synthesizes a resource, it records:

- `source = requirements[0].source` -- the first matching requirement's `(kind, name)`, per
  config-load walk order.
- `all_sources = [r.source for r in requirements]` -- complete list of matching requirements;
  surfaced in describe output (R9), not in single-line origin display.

The "first matching" rule is deterministic given the config-load walk order. For default templates
that many things inherit from, the recorded source is essentially load-order-arbitrary within the
set of all templates referencing `default`; the full list in `all_sources` provides the complete
picture for inspection.

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
   intent is preserved.
3. **Phase 1c: Tailscale migration.** VM template schema gains `tailscale_auth_key`. Manager-entry
   at `vm create` / `vm reinit` walks the subgraph, resolves via the orchestrator, threads the value
   as a kwarg. Legacy resolution path removed.
4. **Phase 1d: Git-credentials migration.** `git_credentials.<name>.token` field; same flow as
   Tailscale, threaded into the git-credentials install runner. Legacy `obtain_token` path removed.
5. **Phase 1e: `agw secret describe`.** CLI command; service-layer logic in `agentworks.secrets`.
   Displays origin, usages, backend mapping provenance, resolution preview.
6. **Phase 2a: Template kinds.** `VMTemplateKind`, `WorkspaceTemplateKind`, `AgentTemplateKind`,
   `SessionTemplateKind` implementations. Inheritance moves into the framework. Built-in defaults
   migrate from the existing resolver fallback into `synthesize()`. Per-field merge from the
   framework replaces the existing inheritance-resolver code.
7. **Phase 2b: Catalog and provider kinds.** `CatalogKind`, `GitCredentialProviderKind`,
   `SecretBackendKindKind` (yes, the redundant name -- a kind named `secret_backend_kind`). Existing
   bespoke validation removed in favor of framework dispatch.
8. **Phase 2c: `agw resource list` / `agw resource describe`.** CLI command group.

Each phase ends at a green CI and a usable intermediate state. Phase 1a-1e ship as one PR sequence
on the `feat/resource-registry-sdd` branch; Phase 2 is a follow-up PR/branch.

## Design decisions

### One package, one validation pass

`agentworks.resources` is a single package with a single entry point (`validate_pass(config)`)
called from the loader. Alternatives considered:

- Distributing the dispatch logic across the existing config types (each type's `__post_init__`
  validates its own references). Rejected because it scatters validation logic and makes cycle
  detection hard. The framework's value is in centralizing reference-checking.
- Wrapping the existing per-type validation in adapters. Rejected because the existing validation
  differs by type in ways the framework wants to unify (auto-decl vs. error, cycle detection, error
  message shape).

The package owns dispatch; existing types own their fields and `required_resources()` method.

### Kind-as-strategy, registered in a module-level dict

Each kind's logic (miss policy, name restrictions, synthesize, merge_operator) lives in one
implementation registered in a `KIND_REGISTRY` dict. Adding a new kind is one new module under
`kinds/`. Alternatives considered:

- One class per resource type with abstract methods. Rejected as heavier; the strategy pattern is
  enough.
- Plugins / entrypoints for kinds. Rejected as premature -- agentworks doesn't have a plugin system
  yet; the dict can become a plugin registry later without changing the protocol.

### `tomlkit` (or equivalent) for line tracking

Python's stdlib `tomllib` does not expose line numbers. The framework needs `(file, line)` for
operator-declared origin. The LLD will pin the exact library; `tomlkit` is the strong candidate
(actively maintained, line info exposed). Switching the loader is mechanical; the existing
`config.py` parsing surface doesn't change shape.

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
Phase 1. Phase 2 hoists the inheritance walk into the framework via `TemplateRequirement`; the
per-type resolver shrinks to a thin shim over `KIND_REGISTRY["vm_template"].merge_operator`.

### `git_credentials` legacy

`agentworks.git_credentials.base.obtain_token` is removed in Phase 1d. Providers (`github.py`,
`azdo.py`) keep their `credential_lines(token=...)` formatting methods; the token comes from the
resolver. The `git_credentials` package shrinks but doesn't disappear.

### DB schema impact

None. The registry is config-load state, not DB state.

## Open questions / for LLD

- **Library for TOML line tracking**: `tomlkit` vs. alternatives. LLD decides.
- **Per-kind error message templates**: format strings live in each kind's module; LLD lays out the
  exact strings.
- **Phase 2 default-template `synthesize()` source-of-truth**: the existing built-in defaults live
  in the resolver. LLD picks the migration mechanism (verbatim port vs. cleanup).
- **`agw secret describe` resolution-preview cost**: the preview calls each backend's
  `would_attempt(secret)` (existing); negligible. Verify no I/O for 1Password / vault backends in
  the preview path.
