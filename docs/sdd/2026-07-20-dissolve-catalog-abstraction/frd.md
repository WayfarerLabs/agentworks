# FRD: dissolve the catalog abstraction

Start date: 2026-07-20.

## Summary

"Catalog" is no longer a meaningful concept: it has no CLI surface, and after the built-in entries
move to bundled manifests it has no bespoke machinery either. What remains are four ordinary
resource kinds (`apt-source`, `apt-package`, `system-install-command`, `user-install-command`) and a
logic-free bundle type wrapping them. This effort retires the "catalog" grouping entirely: define
the built-in entries as bundled manifests like every other resource, delete the bespoke definition
code, regroup the surviving kinds by natural affinity (`apt` and install-commands), and dissolve the
`ResolvedCatalog` bundle so consumers read the Registry directly.

There is no replacement umbrella. "Installers" was considered and rejected: it implies membership
for mise, template-driven apt, and dotfile setup, which are template mechanisms, not resource kinds,
so any umbrella name would either exclude things it names or become a magnet for "should X go here
too?" drift. The honest frame is a handful of related resource kinds, grouped in code by affinity,
under no concept.

## Background (verified)

- **Consumption is already registry-sourced.** Both initializers read the entries via
  `catalog_from_registry(registry)`, which is a plain read of four `kind_dict(registry, ...)` calls.
  `load_catalog(config)` is documented as a legacy parse-path. Templates, secrets, and
  git-credentials read the Registry directly (no `config.<dict>` reads, verified).
- **Operators can already declare these kinds three ways:** built-in `catalog.toml` (bespoke code);
  operator TOML (`[apt_sources.*]` etc., deprecated); and operator YAML manifests
  (`resources/*.yaml`, the recommended path, verified working end to end with shipped samples). Only
  the built-in path is bespoke; the operator YAML path already rides the normal manifest loader.
- **The `manifests/builtin/` mechanism already exists** (currently `vm-sites.yaml`): app-bundled
  YAML manifests published through the same loader with `Origin.built_in` and a shipped-file source.
- **`ResolvedCatalog` is a logic-free 4-dict frozen bundle**; the apt cross-reference resolution and
  install selection live in the initializer, not the bundle.

## Functional requirements

- **R1 Built-in entries are bundled manifests.** The built-in apt-sources, apt-packages, and
  system/user install commands ship as YAML manifests under `manifests/builtin/`, published by the
  existing bundled-manifest publisher with `Origin.built_in` and a shipped-file source, exactly like
  `vm-sites.yaml`.
- **R2 One definition path, no bespoke catalog code.** `catalog.toml`, `load_builtin_catalog`,
  `_parse_catalog`, `load_catalog`, and the built-in half of the catalog publisher are deleted.
  Entries reach the Registry through the manifest loader (built-in and operator YAML) or the
  operator TOML publisher (deprecated surface, retired separately).
- **R3 The kinds are regrouped by affinity; the "catalog" grouping is gone.** The four kinds live in
  two modules: `apt` (`apt-source` + `apt-package`, which cross-reference) and install-commands
  (`system-install-command` + `user-install-command`, identical shape differing only in execution
  scope). The `catalog` module name, `ResolvedCatalog`, and `CatalogError` are removed. The kind
  _names_ are unchanged (they already stand on their own).
- **R4 Consumers read the Registry directly.** `ResolvedCatalog` / `catalog_from_registry` are
  removed; the initializers query the specific kinds they need from the Registry
  (`kind_dict(registry, "apt-package")` etc.). No re-bundled snapshot type survives.
- **R5 Catalog entries gain a real `declared_at`.** The manifest decoders thread the document
  location, so every manifest-loaded entry (built-in and operator) carries a `declared_at` pointing
  at its shipped or operator file.
- **R6 No operator-visible behavior change.** The same entries resolve with the same payloads;
  `agw resource list` / `describe`, apt resolution, and install commands behave identically, except
  built-in rows now show a source location and manifest-sourced descriptions.

## Out of scope

- Retiring the operator **TOML** surface for these kinds (`[apt_sources.*]` etc.). Rides the broader
  TOML-resource deprecation (ADR 0016). The operator TOML publisher half stays (moved to the new
  modules, but not deleted).
- Any change to the kinds' payload shapes, the apt cross-reference model, or the initializer's
  install logic beyond swapping its input from a bundle to direct Registry reads.
- Renaming the kinds themselves, or merging the two install-command kinds (their execution-scope
  difference is real; they stay two kinds in one module).
- Templates, secrets, git-credentials (already registry-sourced; untouched).

## Acceptance criteria

- `agw resource describe apt-source/github-cli` on a fresh install shows a source location (bundled
  manifest) and description, `origin` = built-in; all 18 built-in entries resolve from the Registry
  with payloads identical to before (pinned by a parity oracle).
- An operator override (same-named entry via manifest or TOML) still wins.
- A grep for `catalog.toml`, `load_builtin_catalog`, `_parse_catalog`, `load_catalog`,
  `ResolvedCatalog`, `catalog_from_registry`, `CatalogError`, and the `catalog` module returns
  nothing in the package; the kinds live in the `apt` and install-command modules and self-register.
- VM/agent init resolve and apply the correct apt sources, packages, and install commands, reading
  the kinds from the Registry directly (existing initializer behavior tests pass).
