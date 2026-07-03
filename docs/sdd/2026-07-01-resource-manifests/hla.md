# Resource manifests: high-level architecture

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

## Overview

The resource-registry SDD built the Registry to be publisher-agnostic precisely so the source format
could change without touching the framework. This SDD exercises that seam: the operator publisher's
source swaps from TOML sections to YAML manifest documents, `Config` shrinks to config-only, and the
Registry, kinds, finalize pass, reference machinery, and inspection surfaces carry over unchanged.

```text
+---------------------------+           +-----------------------------+
| agentworks.config         |           | agentworks.resources        |
| (config only, TOML)       |           | (framework, UNCHANGED)      |
| operator/paths/defaults/  |           |  - Registry publish/finalize|
| azure/proxmox/session/    |           |  - kinds, miss policies     |
| secret_config             |           |  - references, cycles       |
+---------------------------+           +--------------^--------------+
                                                       | publish
+---------------------------+   parse    +-------------+-------------+
| resources dir             |----------->| agentworks.manifests      |
| ~/.config/agentworks/     |            | (NEW: loader + publisher) |
|   resources/**/*.yaml     |            |  envelope -> Resource +   |
+---------------------------+            |  SourceLocation           |
                                         +---------------------------+
+---------------------------+                          ^
| app-bundled built-ins     |--------------------------+
| (built-in backends, ...)  |   same loader, built-in origin
+---------------------------+

+---------------------------+
| code publishers           |------------> registry.add(origin=built_in(...))
| catalog, provider         |
| descriptors               |
+---------------------------+
```

The one structural consequence that is not a source swap: resources no longer live on `Config`, so
every call site that still reads `config.secrets` / `config.vm_templates` / `config.admin` must be
repointed to the Registry. The prior SDD left that migration "gradual as needed"; this SDD completes
it as a dedicated preparatory phase, before the loader lands, so the source swap itself is small.

## Package layout

```text
cli/agentworks/manifests/
  __init__.py         # public surface: load_manifests, publish_to
  loader.py           # directory walk, YAML parse, line capture, duplicate detection
  envelope.py         # envelope schema: apiVersion / kind / metadata / spec validation
  decode.py           # per-kind spec -> existing Resource dataclass construction
  builtin.py          # app-bundled built-in manifests (importlib.resources discovery)

cli/agentworks/resources/   # unchanged framework; Origin variant rename only
cli/agentworks/secrets/
  providers.py        # NEW: code-side provider registry + descriptor publisher
  env_var.py          # unchanged behavior; instantiated via provider registry
  prompt.py           # unchanged behavior; instantiated via provider registry
cli/agentworks/cli/commands/config.py   # gains `agw config migrate`
cli/agentworks/migrate/                  # NEW: TOML -> manifests migration tool
```

The loader is pure Python with no Typer dependency, consistent with the typer-isolation rule.
`decode.py` constructs the same Resource dataclasses the TOML parser constructs today (`SecretDecl`,
`VMTemplate`, `AdminConfig`, ...); the types do not move or change shape. The optional
`*Config`-suffix rename deferred by the prior HLA (its "Naming follow-up" section) remains optional
and is not required by this design.

## Layer changes

### `Config` (parsing layer): shrinks to config-only

- Keeps: `[operator]`, `[paths]`, `[defaults]`, `[azure]`, `[proxmox]`, `[session.config]`,
  `[secret_config]`. Parsing, validation, and types for these are untouched.
- Loses: all resource sections, the resource-composition step, the TOML section-line regex scanner
  (the YAML loader captures locations natively), and `Config.publish_to`.
- Post-cutover, `load_config()` raises `ConfigError` if any resource section is present, listing the
  sections and pointing at `agw config migrate`.
- `[secret_config].backends` chain names are validated against the finalized registry (a lookup, not
  a published reference; the chain is policy, not a resource).

### `agentworks.manifests` (new): the operator publisher

- `load_manifests(resources_dir) -> ManifestSet`: walks `**/*.{yaml,yml}` (sorted relative paths,
  dotfiles skipped), parses each file as a YAML stream, validates each document's envelope, decodes
  `spec` into the kind's Resource dataclass, and attaches `declared_at: SourceLocation(file, line)`
  from the document's start mark. Empty documents are skipped. `ManifestSet` preserves load order.
- **Duplicate detection** lives here: two operator documents with the same `(kind, name)` raise
  `ConfigError` citing both `file:line` locations. (Built-in override policy is enforced at publish,
  where both origins are visible; see below.)
- `publish_to(registry)`: mirrors today's `Config.publish_to`, constructing
  `Origin.operator_declared(file=..., line=...)` per document.
- **YAML library**: requires document/node start positions and safe loading. PyYAML's `SafeLoader`
  exposes start marks per node and is the minimal-footprint choice; `ruamel.yaml` is the fallback if
  implementation finds mark plumbing awkward. Decision and exact pinned version at
  LLD/implementation time (verify latest stable then; this is the project's first YAML dependency).
- `SourceLocation` is reused as-is; `Origin` rendering already generalizes (`file:line` works for
  any text format).

### Envelope validation split

- **Envelope layer** (`envelope.py`): `apiVersion == "agentworks/v1"`, `kind` in the
  manifest-declarable set, `metadata.name` present and identifier-valid, unknown top-level keys
  rejected. Manifest-declarability is a new per-kind flag (see "Kind flags" below); descriptor kinds
  reject with the capability-specific message.
- **Spec layer** (`decode.py`): field types, required fields, and value validation exactly as the
  TOML parser does today, reusing the existing per-kind construction and validation logic.
  Unknown-key strictness matches current TOML behavior per kind (LLD confirms the current behavior
  kind by kind and pins it).
- **Registry layer**: unchanged (references, miss policies, reserved names, cycles).

## Bootstrap

`build_registry` gains the manifest publishers and loses the config publisher:

```python
# agentworks/bootstrap.py
def build_registry(config: Config, manifests: ManifestSet) -> Registry:
    registry = Registry.empty()
    catalog.publish_to(registry)            # built-in catalog entries (code publisher)
    git_credentials.publish_to(registry)    # git credential provider descriptors
    secret_providers.publish_to(registry)   # NEW: secret provider descriptors
    builtin_manifests.publish_to(registry)  # NEW: app-bundled resources (built-in backends)
    manifests.publish_to(registry)          # operator documents (was: config.publish_to)
    registry.finalize()
    return registry
```

Publish order matters for the built-in override policy: built-ins publish first, operator documents
second. `Registry.add` today replaces duplicates silently by design; this SDD changes it to explicit
collision handling. A collision between an operator row and an existing built-in row consults the
kind's override flag: allowed (catalog kinds; operator row replaces the built-in row, exactly
today's behavior) or reserved (`secret-backend`; `ConfigError` naming the reserved built-in). A
collision between two operator rows is always a `ConfigError` citing both origins. The manifest
loader already catches operator duplicates within the manifest set, so in the released system the
publish-time check is a backstop; during the development-window dual-source phases it is what
catches a resource declared in both TOML and a manifest.

## Development-window dual source (Phases 2 through 4)

Between the loader landing (Phase 2) and the cutover (Phase 5), TOML resource sections and manifests
coexist at HEAD. The governing rule: TOML resource semantics stay exactly today's until Phase 5, so
any config that loads today loads at every intermediate phase. Concretely:

- Cross-source collisions (same `(kind, name)` from TOML and a manifest) error at `Registry.add` per
  the collision handling above.
- `git_credentials.<name>` TOML entries accept `provider` as an alias for `type` from Phase 3
  (`provider` wins when both are present); `type` keeps working until the TOML resource path is
  removed at Phase 5. Manifests accept only `provider`.
- TOML-published `[secret_backends.<kind>]` rows keep today's shape, today's override-allowed
  publish (they may replace the built-in backend rows), and the legacy resolver construction path.
  The reserved-name policy applies to manifest-declared backends only until Phase 5.

The window exists only between merged phases; no release ships it.

## Kind flags

`ResourceKind` gains two declarative flags consumed by the envelope layer and `Registry.add`:

- `manifest_declarable: bool`. True for every operator kind; False for descriptor kinds
  (`secret-provider`, `git-credential-provider`) and any future code-only kind.
- `builtin_override: Literal["allow", "reserved"]`. `allow` for catalog kinds; `reserved` for
  `secret-backend` (and `secret-provider` / `git-credential-provider` trivially, since they are not
  declarable at all). Template kinds are unaffected (their defaults are synthesized, not built-in
  rows, so no collision arises).

Both are static per-kind declarations in the same place the miss policy lives; no new dispatch
machinery.

## Origin cleanup

`Origin.variant` becomes `Literal["operator-declared", "built-in", "auto-declared"]` with
`"system-plugin"` and `"external-plugin"` documented as reserved (added to the Literal and given
factory classmethods by the plugin SDD, not this one). `code_declared(source=...)` is renamed
`built_in(source=...)`; field shapes are otherwise unchanged. Built-in rows published from bundled
manifests may additionally carry the bundled `file`/`line` for debugging, but render as
`built-in (agentworks.secrets)` style strings; the source identifier is the operator-facing detail.

Display updates ride along: `agw doctor`, `agw secret list/describe`, `agw resource list/describe`
render `built-in (...)`; the `--origin` filter vocabulary becomes `operator | builtin | auto`.

## Secret provider architecture

Today's `SecretSource` protocol (`would_attempt` / `get` / `batch_get` / `describe_lookup`) is
already the capability contract. The split adds instantiation-by-name with config:

```text
PROVIDER_REGISTRY: dict[str, SecretProvider]     # code-side; built-ins env-var, prompt
  SecretProvider (protocol):
    name: str
    config_schema        # validates + defaults a backend document's spec (minus `provider`)
    instantiate(backend_name, config) -> SecretSource

registry rows:
  secret-provider:<name>       # descriptor, built-in, error miss policy, not declarable
  secret-backend:<name>        # resource; spec.provider references secret-provider

resolver construction (orchestration):
  for name in secret_config.backends:            # chain, config-side
      backend = registry.lookup("secret-backend", name)     # miss -> ConfigError
      provider = PROVIDER_REGISTRY[backend.provider]        # ref already validated
      sources.append(provider.instantiate(name, backend.config))
```

- `SecretBackendDecl` (the `secret-backend` Resource) carries `name`, `description`, `provider`, and
  the provider-specific config mapping. Its `referenced_resources()` emits one reference to
  `("secret-provider", provider)`.
- **Neither built-in provider accepts configuration** (non-empty backend config is a validation
  error from each schema); `env_var_name_for` keeps its fixed `AW_SECRET_` convention. The
  `config_schema` / `instantiate(config)` contract is exercised end to end by a test-only provider
  registered only in the test suite, so the plumbing is verified without shipping artificial
  operator surface on the built-ins.
- **Built-in backends** `env-var` and `prompt` ship as an app-bundled manifest
  (`agentworks/manifests/builtin/secret-backends.yaml`), exercising the built-in-manifest path end
  to end. Their names are reserved via `builtin_override = "reserved"`.
- `backend_mappings` on secrets stay keyed by backend name. Default-convention display
  (`agw secret describe`, doctor) asks the instantiated source, so a future config-bearing
  provider's conventions show through with no display-layer changes.
- The existing `SecretBackendConfig` dataclass (per-backend config parsed from TOML today) is
  subsumed by `SecretBackendDecl` at the cutover; the orchestrator's construction path swaps from
  "kind-keyed config sections" to "chain names looked up in the registry". During the development
  window the orchestrator supports both row shapes (`SecretBackendDecl` via provider instantiation,
  legacy TOML rows via the existing path); the legacy path is retired in Phase 5 with the rest of
  the TOML resource surface.

Git credentials need no structural change: `GitCredentialConfig.provider` (renamed from `type`)
keeps referencing the `git-credential-provider` descriptor kind; the provider-name-to-class registry
in `agentworks.git_credentials` is already the code-side capability registry. The two sides now tell
the same story, which is the point of R9.

## Migration tool

```text
agw config migrate [--yes] [--force] [--dry-run]

config.toml --tomlkit parse--> section split per FRD R1 table
   config sections  --> config.toml rewritten in place (comments/format preserved,
                        resource sections deleted; original backed up to paths.backups)
   resource sections --> resources/<kind-kebab>.yaml (multi-document, declaration order)
                        + renames: type->provider, [secret_backends.<kind>] -> secret-backend
                          documents (empty env-var/prompt sections dropped)
```

- Lives in `cli/agentworks/migrate/` with a thin Typer command in `commands/config.py`.
- Uses **tomlkit** for the comment-preserving rewrite (new dependency, migration path only; verify
  latest stable at implementation). The read side uses the same parse the legacy loader used, so
  field interpretation cannot drift from what the config actually meant.
- Emission is envelope-shaped: the kind-to-section mapping comes from `decode.KIND_SECTIONS` (shared
  with the loader), and field-level correctness is verified by round-tripping the migrator's emitted
  manifests through `load_manifests` itself (whose decoders call the same TOML loaders), so the
  loader and the migrator cannot disagree.
- The pre-cutover TOML resource-section parser survives only inside the migration tool (it needs to
  read old configs); the live config loader rejects resource sections outright.

## Validation responsibilities (updated)

| Layer                      | Owns                                                                                          |
| -------------------------- | --------------------------------------------------------------------------------------------- |
| Config (TOML)              | config-section parse, field types, `secret_config` chain shape                                |
| Manifest loader (envelope) | YAML parse, apiVersion, kind known + declarable, metadata shape, operator duplicate detection |
| Manifest loader (spec)     | kind-specific field types / required fields / value validation (unchanged semantics)          |
| Provider capability        | provider-specific backend config (schema + defaults)                                          |
| Registry publish           | built-in override policy per kind                                                             |
| Registry finalize          | references, miss policies, reserved names, cycles, description polish (all unchanged)         |
| Post-finalize lookups      | `secret_config.backends` chain names                                                          |

All raise `ConfigError`; the layer determines the framing, as today.

## CLI surfaces

- `agw config migrate`: new (see above). Completions updated for the new subcommand.
- `agw config sample`: emits the config-only TOML sample; gains a way to emit sample manifests
  (exact flag shape at LLD; the sample-manifest content ships with the app like `sample-config.toml`
  does).
- `agw resource list/describe`, `agw secret list/describe`, `agw doctor`: display-only changes
  (origin strings, `--origin builtin`, `--kind git-credential`, backend rows showing provider and
  effective convention).
- No new top-level command groups.

## Design decisions

### Auto-load, not apply

The registry is already rebuilt from scratch on every invocation with no persisted registry state;
`apply` semantics only pay off when a server reconciles against held state. Auto-load keeps the
mental model identical to today's config loading. The drift-tracking future reserved by the
resource-registry lockfile is where reconciliation thinking would land, unchanged by this choice.

### Kubernetes envelope, agentworks vocabulary

`apiVersion` / `kind` / `metadata` / `spec` verbatim for familiarity (including `apiVersion`'s
camelCase). Inside the envelope, agentworks keeps its own conventions: `kind` values are the
registry kind identifiers, lower-kebab per FRD R9 (`vm-template`, not the PascalCase `VmTemplate` of
Kubernetes), matching every other operator-typed value in the project. The vocabulary stays one
canonical set across manifests, CLI, origins, and error messages; PascalCase in manifests would
force a permanent mapping layer with no functional payoff. `metadata` carries exactly the
framework-uniform fields (`name`, `description`), mirroring the framework-uniform vs kind-specific
split the registry already draws.

### By-kind migration output

One file per kind, multi-document. Typical configs are small; per-resource files would produce a
directory of five-line files. The loader is layout-agnostic, so operators can split further at will.
The tool's grouping is a default, not a contract.

### Hard cutover

Consistent with the project's migration precedent (env-and-secrets, resource-registry). The
migration tool plus a load error that names the offending sections and the command to run makes the
cutover a one-command event. A dual-source window would double the loader surface and create
ambiguous-precedence questions for no lasting benefit.

### Reserved built-in backend names, overridable catalog names

Deliberately per-kind rather than uniform. For catalog kinds, the name is the interface (templates
reference `gh` by name), so same-name override is the only way to customize what a name installs;
that behavior exists and is documented today. For backends, the name is not load-bearing (the chain
selects backends): identifier customization is per-secret `backend_mappings` today, and once
config-bearing providers exist, a sibling backend plus the chain covers instance-level
customization. Reserving the names keeps built-in behavior trustworthy.

### Consumer repoint before source swap

Completing the Config-to-Registry consumer migration first (as its own phase, behavior unchanged,
TOML still the source) means the risky part of the SDD is a pure refactor under existing tests, and
the actual source swap touches only the loader and bootstrap. It also flushes out any consumer that
silently depended on Config-layer quirks before the format changes underneath it.

## Open questions / for LLD

- **YAML library and mark plumbing**: PyYAML SafeLoader subclass vs ruamel.yaml; how document start
  lines surface through the stream API. Pin the library and version at implementation (latest stable
  rule).
- **Per-kind unknown-key strictness**: confirm what the TOML parser does per kind today and pin it
  in the spec-layer LLD (the manifest loader must not silently become stricter or looser).
- **Eager template resolution home**: `load_config` currently triggers eager template resolution;
  with templates sourced from manifests, that moves to `build_registry` callers. The consumer
  repoint phase's LLD maps the exact call sites.
- **Provider `config_schema` shape**: a small dataclass-based schema vs plain validate-callable.
  Whatever is chosen must produce field-level `ConfigError`s naming the manifest location.
- **Sample manifest delivery**: flag shape on `agw config sample` and packaging of the sample files.
- **Kind vocabulary sweep blast radius (Phase 0)**: enumerate the kind-string literals, completions
  entries, and naming-consistency test updates for the lower-kebab casing change plus the
  `git-credential` singularization.
