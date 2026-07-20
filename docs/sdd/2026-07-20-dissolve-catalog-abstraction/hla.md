# HLA: dissolve the catalog abstraction

Implements the [FRD](./frd.md). Two threads: a data move (built-in entries to bundled manifests) and
a code reorganization (regroup the kinds, delete the bespoke and bundle machinery). The consumption
side already reads the Registry, which is what makes the reorganization safe.

## Current state (verified)

- `bootstrap.build_registry` publishes: capability rows, `builtin_manifests.publish_to` (bundled
  `manifests/builtin/vm-sites.yaml`), then `catalog.publish_to(registry, config)` (built-in catalog
  from `catalog.toml` AND operator TOML entries), then git-credential / harness / secrets, then
  operator sources.
- `catalog.py` holds: `CatalogError`; the four entry dataclasses; the four kind strategies
  (`_AptSourceKind` etc.); `ResolvedCatalog` (a logic-free 4-dict frozen bundle) + its builder
  `catalog_from_registry`; the per-entry loaders (`_load_apt_sources` etc.); and the bespoke
  built-in/parse code (`catalog.toml`, `load_builtin_catalog`, `_parse_catalog`, `load_catalog`).
- Consumers: the VM initializer and agent initializer take a `ResolvedCatalog` (built by
  `catalog_from_registry`) and read `catalog.apt_packages.get(name)` etc. The manifest decoders
  (`_decode_apt_source` and siblings) call the per-entry loaders but drop `doc.location`.

## Target state

The four kinds live in two affinity modules; there is no `catalog` module, no `ResolvedCatalog`, and
no bespoke definition code. Built-in entries are bundled manifests; consumers read the Registry.

## Components

### 1. Two affinity modules

- `agentworks/apt.py`: `AptSourceEntry`, `AptPackageEntry`, `_AptSourceKind`, `_AptPackageKind`, the
  apt per-entry loaders, and the apt cross-reference (`AptPackageEntry.referenced_resources`).
- `agentworks/install_commands.py`: `SystemInstallCommandEntry`, `UserInstallCommandEntry`, their
  kind strategies, the install-command loaders, and the shared `_load_test_fields` helper.

`CatalogError` is dropped; loaders raise the framework's existing `ConfigError` / `ExternalError`
(the shape they already surface through `CatalogError`, which subclasses `ExternalError`). The
`resources/kinds` self-registration index imports the two new modules instead of `catalog`.

### 2. Built-in entries as bundled manifests

Convert the 18 `catalog.toml` entries to YAML manifest documents under `manifests/builtin/` (grouped
`apt-sources.yaml`, `apt-packages.yaml`, `install-commands.yaml`, `---`-separated), each a
`kind`/`metadata`/`spec` document. `builtin_manifests.publish_to` already globs the directory and
publishes with `Origin.built_in(source="agentworks.manifests.builtin/<file>")`, so no publisher code
changes for the built-in half. The publisher asserts the bundle is issue-free, so they must
validate; a parity oracle pins the resolved payloads.

### 3. Publisher split

The built-in half of `catalog.publish_to` is deleted (built-ins now flow via
`builtin_manifests.publish_to`). The operator-TOML half moves into the two new modules as small
`publish_to` functions (or one shared publisher that reads `config.apt_sources` etc.), keeping the
deprecated operator-TOML surface. `bootstrap.py` calls the new operator publisher(s) after the
built-in publishers so the built-in-before-operator override ordering holds.

### 4. Dissolve `ResolvedCatalog`

Delete `ResolvedCatalog` and `catalog_from_registry`. The initializers read the kinds they need
directly from the Registry via `kind_dict(registry, "apt-package")` etc. The apt-source-selection
and install-command logic is unchanged; only its input changes from `catalog.<dict>` to a direct
Registry read (the functions already receive or can receive `registry`). Where a function today
takes `catalog: ResolvedCatalog`, it takes the specific dict(s) it uses, resolved from the Registry
at the caller.

### 5. `declared_at` threading

Give the per-entry loaders an optional `decls: _SectionLineMap` param (default a
synthesized-returning shim) and set `declared_at=decls.lookup(<section>, name)` per entry. The
manifest decoders pass `_decls(doc.location)`; the operator TOML publisher passes the real map if
readily available, else the default. Fixes the location gap on the operator-YAML path (which
operators already use) as well as the migrated built-ins.

### 6. Delete the bespoke code and migrate tests

Delete `catalog.toml`, `_BUILTIN_CATALOG_PATH`, `load_builtin_catalog`, `_parse_catalog`,
`load_catalog`, and the TOML-file helpers used only by them. Migrate `test_catalog.py` and
`test_config_resource_read_guard.py`: re-express parse-level assertions against a built Registry
(via `kind_dict`) and the bundled-manifest output; delete tests that only exercised the removed
parse functions. A parity test asserts the 18 built-in entries resolve from the Registry with the
pre-change payloads (oracle snapshotted before deletion).

## Sequencing rationale

Snapshot the oracle first, then move built-ins to manifests (behavior held), then reorganize the
code (module split, publisher split, `ResolvedCatalog` dissolution) on top of the now-uniform
definition path, then delete the dead bespoke code and migrate tests. Each step keeps the suite
green; the reorg never runs against the still-bespoke built-in path.

## Risks and mitigations

- **Payload drift in the TOML to YAML conversion.** The parity oracle pins resolved payloads; the
  bundled-manifest issue-free assertion catches malformed YAML.
- **Import churn from the module split** (decode, bootstrap, initializers, the kinds index). Mypy
  plus the full suite catch a missed import; the split is mechanical, one symbol per new home.
- **Override ordering regressions** after the publisher split. An explicit operator-override test
  runs after the split.
- **A missed `ResolvedCatalog` / `catalog_from_registry` consumer.** Grep is clean today
  (initializers only); re-grep before deletion, and mypy flags a dangling reference.

## What does not change

- The four kind names, their payload shapes, the apt cross-reference model, the initializer install
  logic, and the operator-TOML surface (deprecated, retired separately).
- Templates, secrets, git-credentials (already registry-sourced).
