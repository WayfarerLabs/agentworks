# HLA: built-in catalog as bundled manifests

Implements the [FRD](./frd.md). This is a contained refactor plus a data move; the consumption side
already reads the Registry, so the work is concentrated in how built-in catalog entries are defined
and published.

## Current state (verified)

- `bootstrap.build_registry` publishes in order: capability rows, `builtin_manifests.publish_to`
  (the bundled `manifests/builtin/vm-sites.yaml`), then `catalog.publish_to(registry, config)`
  (which publishes BOTH the built-in catalog from `catalog.toml` AND the operator TOML catalog
  extensions), then git-credential / harness / secrets, then operator sources (`Config.publish_to`
  for TOML, then operator YAML manifests).
- Built-in catalog: `catalog.toml` (18 entries: 5 apt-source, 5 apt-package, 1 system-install, 7
  user-install) parsed by `load_builtin_catalog` / `_parse_catalog`.
- Consumers: `catalog_from_registry(registry)` builds the merged `ResolvedCatalog` from registry
  rows (`kind_dict(registry, "apt-source")` etc.); the VM initializer and agent initializer both use
  it.
- `load_catalog(config)` is a legacy parse-level merge (built-in + operator TOML) kept for the
  catalog-parsing tests; its docstring says production reads `catalog_from_registry` instead.
- The catalog manifest decoders (`_decode_apt_source` and the three siblings) call the shared
  per-entry loaders (`_load_apt_sources` etc.) but drop `doc.location`, so manifest catalog entries
  get a synthesized `declared_at`.

## Target state

Built-in catalog entries are bundled YAML manifests under `manifests/builtin/`, published by the
existing `builtin_manifests.publish_to`. The bespoke built-in path is deleted. Everything reaches
the Registry through the manifest loader (built-in and operator YAML) or the operator TOML publisher
(deprecated surface). Consumers keep reading `catalog_from_registry`.

## Components

### 1. Bundled catalog manifests (`manifests/builtin/`)

Convert the 18 `catalog.toml` entries into YAML manifest documents, one `kind`/`metadata`/`spec`
document per entry, grouped into a few files by kind for readability (e.g. `apt-sources.yaml`,
`apt-packages.yaml`, `install-commands.yaml`), each using `---` document separators. `metadata.name`
carries the entry name; `metadata.description` carries the human text; `spec` carries the payload
(key_url, apt, command, etc.). Cross-references (an apt-package's `apt_sources`) stay in `spec` and
resolve through the framework's reference pass exactly as they do from TOML. The bundled-manifest
publisher asserts the bundle is issue-free, so these must validate cleanly (the plan includes a
parity check against the pre-change resolved payloads).

### 2. Publish path

`builtin_manifests.publish_to` already globs the whole `manifests/builtin/` directory and publishes
each entry with `Origin.built_in(source="agentworks.manifests.builtin/<file>")`. Adding the catalog
manifest files is picked up automatically; no publisher code changes for the built-in half.

`catalog.publish_to` loses its built-in half (the four `builtin.<kind>` loops) and keeps only the
operator TOML half (`_load_apt_sources(config.apt_sources)` etc. into registry rows). Since it is
now purely an operator publisher, it may move out of the "built-in publishers first" block in
`bootstrap.py` down next to `Config.publish_to`; either way the invariant holds because built-in
catalog now publishes via `builtin_manifests.publish_to` (which already runs first), so operator
entries still land after and override per `builtin_override = "allow"`.

### 3. Deletions

- `catalog.toml` and `_BUILTIN_CATALOG_PATH`.
- `load_builtin_catalog`, `_parse_catalog`, and the TOML-section helpers used only by them
  (`_get_section`, `_load_toml` if unused elsewhere).
- `load_catalog(config)` and its `@cache`, once its remaining callers are migrated (see 5).

Retained: the per-entry loaders (`_load_apt_sources` / `_load_apt_packages` /
`_load_system_commands` / `_load_user_commands`) are still used by the operator TOML publisher half
and by the manifest decoders, so they stay. `catalog_from_registry` and `ResolvedCatalog` stay (the
consumer view).

### 4. `declared_at` threading (R4)

Give the four catalog `_load_*` helpers an optional `decls: _SectionLineMap` parameter and set
`declared_at=decls.lookup(<section>, name)` per entry, mirroring every other config loader. The
manifest decoders pass `_decls(doc.location)` (the shim the other manifest decoders already use), so
manifest catalog entries (built-in via the bundled files, operator via their YAML) get a real
location. The operator TOML publisher passes the config `_SectionLineMap` if readily available, else
the synthesized default (this is the deprecated surface; a real line there is a nice-to-have, not
required by R4). Default the param to a synthesized-returning shim so nothing breaks.

### 5. Test migration

`test_catalog.py` (the "catalog parsing tests") and `test_config_resource_read_guard.py` reference
the bespoke path. Migrate the parse-level assertions to build a Registry and assert via
`catalog_from_registry` (or the bundled-manifest publisher output) so they cover the real path.
Delete tests that only exercised `load_catalog` / `load_builtin_catalog` as parse functions once
their intent is re-expressed against the Registry. Add a parity test: the 18 built-in entries
resolve from the Registry with the same payloads the old `load_builtin_catalog` produced (snapshot
the old values into the test as the oracle before deleting the code).

## Risks and mitigations

- **Payload drift during the TOML to YAML conversion.** Mitigation: the parity test in 5 pins the
  resolved payloads to the pre-change values; the bundled-manifest publisher's issue-free assertion
  catches malformed YAML at load.
- **Publish-order / override regressions.** Mitigation: an explicit test that an operator override
  of a built-in catalog entry still wins, run after the publisher split.
- **A hidden consumer of `load_catalog` / `load_builtin_catalog`.** Mitigation: grep is clean for
  production (only tests + the two functions themselves); the plan re-greps before deletion.

## What does not change

- `catalog_from_registry`, `ResolvedCatalog`, the initializer install logic, the cross-reference
  model, and the operator TOML catalog surface (deprecated, retired separately).
- Templates, secrets, git-credentials (already registry-sourced).
