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
| (wired; empty post-5.5)   |   same loader, built-in origin
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
  providers.py        # code-side capability registry + descriptor publisher
  env_var.py          # unchanged behavior; registered in the capability registry
  prompt.py           # unchanged behavior; registered in the capability registry
cli/agentworks/cli/commands/resource.py  # gains `agw resource migrate` + `agw resource sample`
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
- Keeps (dual-path, revised 2026-07-03): the TOML resource sections and their loaders, deprecated
  but fully supported. End state (Phase 5): each present section warns at load with a pointer at
  `agw resource migrate` (at HEAD only `[secret_backends.*]` warns, and the migrate command arrives
  in Phase 4 -- the warning gains its pointer then). Removal (and the loader-ownership inversion
  that follows it) waits for a future major release.
- `[secret_config]` is pure config and is NEVER published (final ruling, reversing the interim
  secret-config-row experiment): settings that name resources -- the chain today, active plugins
  tomorrow -- are consumed by their owning subsystem in normal operation. The secrets subsystem
  validates the chain against the finalized registry at the composition boundary
  (`secrets.validate_chain`, run by `build_registry`), with config vocabulary in every error.

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
- **Registry layer**: unchanged (references, miss policies, reserved-default names, cycles).

## Bootstrap

`build_registry` gains the manifest publishers and keeps the config publisher (dual-path); it is a
PURE FUNCTION -- no memo -- called once at a command's composition root and threaded down. After
finalize it runs the config-consistency checks of subsystems whose settings name resources:

```python
# agentworks/bootstrap.py (as built)
def build_registry(config: Config, manifests: ManifestSet | None = None) -> Registry:
    # manifests=None auto-loads <config-dir>/resources/ and surfaces its warnings
    registry = Registry.empty()
    builtin_manifests.publish_to(registry)  # app-bundled resources (wired; empty post-5.5)
    catalog.publish_to(registry, config)    # built-in catalog + operator TOML catalog extensions
    git_credentials.publish_to(registry)    # git credential provider descriptors
    secrets.publish_to(registry)            # secret-backend capability descriptors
    config.publish_to(registry)             # operator TOML resource sections (dual-path)
    manifests.publish_to(registry)          # operator YAML documents
    registry.finalize()
    secrets.validate_chain(config, registry)  # config-vocabulary chain/reachability checks
    return registry
```

Publish order matters for the built-in override policy: built-ins publish first, operator documents
second. `Registry.add` today replaces duplicates silently by design; this SDD changes it to explicit
collision handling. A collision between an operator row and an existing built-in row consults the
kind's override flag: `allow` (catalog kinds; operator row replaces the built-in row, exactly
today's behavior) or `reserved` (`ConfigError`; post-5.5 defensively declared with zero reachable
members -- descriptor kinds are not declarable, which R3's envelope error enforces earlier -- and
retained for the plugin SDD's default exposed resources). A collision between two operator rows is
always a `ConfigError` citing both origins. The manifest loader already catches operator duplicates
within the manifest set; the publish-time check is what catches a resource declared in both TOML and
a manifest (a permanent dual-path condition).

## Dual source (permanent, revised 2026-07-03)

TOML resource sections and manifests coexist indefinitely -- different publishers, single registry,
as a shipped architecture rather than a development window. The governing rule: any config that
loads today keeps loading (with deprecation warnings on TOML resource sections). Concretely:

- Cross-source collisions (same `(kind, name)` from TOML and a manifest) error at `Registry.add` per
  the collision handling above.
- `git_credentials.<name>` TOML entries accept `provider` as an alias for `type` from Phase 3
  (`provider` wins when both are present); `type` keeps working until the TOML resource path is
  removed in a future major (Phase 6). Manifests accept only `provider`.
- `[secret_backends.<kind>]` TOML sections are warned deprecated no-ops as of Phase 3.6 (they were
  semantically empty; post-5.5 the kind is a capability descriptor and not declarable at all).

## Kind flags

`ResourceKind` gains two declarative flags consumed by the envelope layer and `Registry.add`:

- `manifest_declarable: bool`. True for every operator kind; False for descriptor kinds
  (`secret-backend`, `git-credential-provider`) and any future code-only kind.
- `builtin_override: Literal["allow", "reserved"]`. `allow` for catalog kinds; `reserved` elsewhere
  as the defensive default. Post-5.5 the `reserved` tier has zero REACHABLE members (its only live
  member was the declarable secret-backend kind, whose built-in rows an operator manifest could
  actually collide with) -- but the tier itself stays: ten kinds declare it defensively, and the
  draft plugin SDD's default exposed resources are its named future consumer. (This revises the
  first-draft answer to the design review's question 25, which said delete; implementation contact
  showed deletion would flip ten defensive declarations and force the plugin SDD to re-add the
  tier.) Template kinds are unaffected (their defaults are synthesized, not built-in rows, so no
  collision arises).

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

## Secret backend architecture (revised 2026-07-07: the capability collapse)

Today's `SecretSource` protocol (`would_attempt` / `get` / `batch_get` / `describe_lookup`) is
already the capability contract. The 2026-07-07 revision (plan sequencing note) removed the
instantiation layer that sat on top of it; the capability itself is the backend:

```text
SECRET_BACKEND_REGISTRY: dict[str, SecretBackend]  # the capabilities; built-ins env-var, prompt
  SecretBackend (protocol, stateless; a well-defined API, nothing more):
    name, interactive
    would_attempt(secret, mapping)
    describe_lookup(secret, mapping)
    batch_get(wants)

registry rows:
  secret-backend:<name>        # descriptor, built-in, error miss policy, not declarable

resolution (a loop in secrets/resolve.py; validate_chain already ran at build_registry):
  backends = active_backends(config, registry)   # [secret_config].backends -> capabilities, in order
  for backend in backends:
      wants = mappings_for_still_missing_where(backend.would_attempt)  # incl. the generic `false` opt-out
      resolved |= backend.batch_get(wants)
```

(The orchestration -- mapping lookup, the generic `false` opt-out, prompt-once batching -- lives in
the resolution loop, not on the protocol; the protocol is exactly the three methods above. Whether a
thin runtime wrapper object exists per chain entry is an implementation detail.)

- There is no `SecretBackendDecl`, no declarable `secret-backend` manifest kind, no bundled backends
  manifest, and no reserved-name machinery for backend names. The descriptor rows mirror the code
  registry directly; the previous duplicate rows (`secret-provider/env-var` AND
  `secret-backend/env-var`) collapse to one per capability.
- **Neither built-in backend accepts configuration**; `env_var_name_for` keeps its fixed
  `AW_SECRET_` convention. When the first config-bearing backend ships, its connection configuration
  is backend-scoped (one configured form per capability); a genuine multi-instance need graduates
  that backend to a declarable instance kind at that point (FRD R8).
- `backend_mappings` on secrets are keyed by backend (capability) name; per-secret structured
  mappings carry instance-flavored addressing (vault/item/field) where a store needs it.
  Default-convention display (`agw secret describe`, doctor) asks the capability's API.
- `[secret_backends.<name>]` TOML sections remain semantically empty, warned deprecated no-ops.
- The runtime semantics of runtime-model-lld.md are unchanged: resolution is a loop over
  `active_backends(config, registry)`; a command resolves once at its composition root and threads
  the VALUES to its `compose_env(values=...)` sites; no resolver object, no cache, no memos. The
  "door" METAPHOR is retired with the collapse (it earned its keep enforcing
  providers-only-via-backends; with no layer between callers and the capability, `SecretBackend` is
  simply the API that generalizes backend capabilities).

Git credentials keep their shape and illustrate the general pattern: `git-credential` resources
reference the `git-credential-provider` capability many-to-one via `spec.provider`, carrying
provider-owned config in `spec.provider_config`. The credential keeps a dedicated kind because a
credential is a real domain object (templates reference credentials by name; the token secret hangs
off it) -- not because the pattern requires an exposure layer.

## Migration tool

```text
agw resource migrate [SELECTOR]... [--layout per-kind|single|per-resource]
                     [--toml comment|delete] [--dry-run] [--yes]

config.toml --tomlkit parse--> selected resource sections per FRD R1 table / decode.KIND_SECTIONS
   selected sections --> resources/ YAML per --layout (multi-document, declaration order,
                         APPEND-ONLY: existing YAML is never parsed or rewritten)
                         + renames: type->provider; [secret_backends.<kind>] dropped with a note
   config.toml       --> migrated sections commented out (default, with a migrated-to marker)
                         or deleted; surviving sections' comments/format preserved; rewrite
                         atomic (backup to paths.backups taken before ANY write, manifests
                         included)
   post-run          --> registry rebuilt and verified row-for-row equivalent (rollback on
                         mismatch)
```

Recurring mover, not a one-time converter (revised 2026-07-05): selectors make it incremental,
append-only output makes repeats safe, and the per-run registry-equivalence verification makes every
run self-checking. Full semantics in `migration-tool-lld.md`.

- Lives in `cli/agentworks/migrate/` with a thin Typer command in `commands/resource.py`.
- Uses **tomlkit** for the comment-preserving rewrite (new dependency, migration path only; verify
  latest stable at implementation). The read side uses the same parse the legacy loader used, so
  field interpretation cannot drift from what the config actually meant.
- Emission is envelope-shaped: the kind-to-section mapping comes from `decode.KIND_SECTIONS` (shared
  with the loader), and field-level correctness is verified by round-tripping the migrator's emitted
  manifests through `load_manifests` itself (whose decoders call the same TOML loaders), so the
  loader and the migrator cannot disagree.
- The live config loader keeps reading TOML resource sections (dual-path, with deprecation
  warnings); the migration tool's own tomlkit read side is what a future major's retirement phase
  keeps when the live loaders drop the sections.
- Companion authoring command: `agw resource sample [KIND] [--write FILENAME]` prints bundled,
  loader-verified sample manifests (all kinds or one), or saves them into the resources directory
  (append-only, like the migrator). The samples are the YAML counterpart of `sample-config.toml`.

## Validation responsibilities (updated)

| Layer                      | Owns                                                                                                                 |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Config (TOML)              | config-section parse, field types, `secret_config` chain shape                                                       |
| Manifest loader (envelope) | YAML parse, apiVersion, kind known + declarable, metadata shape, operator duplicate detection                        |
| Manifest loader (spec)     | kind-specific field types / required fields / value validation (unchanged semantics)                                 |
| Capability (git-cred)      | `spec.provider_config` validation on git-credential resources                                                        |
| Registry publish           | built-in override policy per kind                                                                                    |
| Registry finalize          | references, miss policies, reserved-default names, cycles, description polish (unchanged)                            |
| Composition boundary       | `build_registry` runs `secrets.validate_chain(config, registry)`: chain names, operator-declared secret reachability |

All raise `ConfigError`; the layer determines the framing, as today.

## CLI surfaces

- `agw resource migrate`: new (see above). Completions updated; selector completion is a NEW
  cross-product completer (kind identifiers plus `kind/name` pairs read from the operator's TOML)
  built on the dynamic-completion plumbing -- the existing dynamic completers are flat per-parameter
  name lists, so this is new capability, not reuse.
- `agw resource sample`: new (see above); kind argument and `--write` complete statically.
- `agw config init / edit / sample`: unchanged, and NOT deprecated -- config-is-config makes the
  settings file permanent, so these commands are already in their final home. `config sample` stays
  the TOML sample; the YAML teaching surface is `resource sample`.
- `agw resource list/describe`, `agw secret list/describe`, `agw doctor`: display-only changes
  (origin strings, `--origin builtin`, `--kind git-credential`, backend descriptor rows showing the
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

### Migration output layouts, per-kind default

The loader is layout-agnostic, so the tool's file mapping is ergonomics, not contract. `--layout`
offers `per-kind` (default), `single`, and `per-resource` (revised 2026-07-05: the maintainer wants
resource-to-file mapping to be the operator's call). Per-kind stays the default because typical
configs are small and one multi-document file per kind reads best -- a directory of five-line files
is the worst shape for the common case, which is why it is an option rather than the default.

### Dual-path (revised from "hard cutover", 2026-07-03)

The maintainer reversed the original hard-cutover call: keeping both paths fully supported forces
the "different publishers, single registry" architecture to be real rather than transitional, and
frees operators to migrate on their own schedule. The original concern (double loader surface,
ambiguous precedence) is answered by what actually shipped: both sources decode through the same
loaders, and there is no precedence -- a cross-source duplicate is an error citing both locations.
TOML resource sections warn as deprecated; removal waits for a future major.

### Reserved built-in backend names, overridable catalog names

> Superseded by the Phase 5.5 capability collapse (2026-07-07): the declarable secret-backend kind
> -- the reserved tier's only member -- no longer exists, so this decision's backend half is moot
> and `builtin_override` keeps only `allow`. The catalog half stands. Recorded as decided:

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
- **Provider `config_schema` shape**: OVERTAKEN by the Phase 5.5 collapse (2026-07-07) -- the
  decode-time backend-config validation this question served was deleted with the declarable kind.
  The surviving instance of the question (git-credential `spec.provider_config`) is answered: the
  capability validates, producing `ConfigError`s naming the manifest location.
- **Sample manifest delivery**: RESOLVED 2026-07-05, differently than posed -- a new
  `agw resource sample [KIND] [--write FILENAME]` owns the YAML teaching surface (`config sample`
  stays TOML, per config-is-config); see `migration-tool-lld.md`.
- **Kind vocabulary sweep blast radius (Phase 0)**: enumerate the kind-string literals, completions
  entries, and naming-consistency test updates for the lower-kebab casing change plus the
  `git-credential` singularization.
