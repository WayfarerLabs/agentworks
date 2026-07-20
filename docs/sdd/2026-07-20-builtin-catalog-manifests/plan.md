# Plan: built-in catalog as bundled manifests

Implements the [FRD](./frd.md) per the [HLA](./hla.md). Brief effort: a data move plus code
deletion, with the consumption side already registry-sourced.

Each phase ends green: from `cli/`,
`uv run pytest -q && uv run ruff check . && uv run mypy agentworks`; then from repo root
`./scripts/lint-files.sh`. New behavior lands with tests in-phase.

## Phase 0: parity oracle

- [ ] Snapshot the 18 built-in catalog entries' resolved payloads as a test oracle BEFORE touching
      anything: capture `load_builtin_catalog()`'s output (per kind: name to payload fields) into a
      fixture/constants in a new parity test. This is the "no drift" reference the later phases
      prove against.

**Done when:** a test pins the current built-in catalog payloads; suite green.

## Phase 1: bundled manifests replace the built-in code path

- [ ] Author the bundled catalog manifests under `cli/agentworks/manifests/builtin/` (grouped by
      kind: `apt-sources.yaml`, `apt-packages.yaml`, `install-commands.yaml`, `---`-separated
      documents). Each document: `kind`, `metadata.name`, `metadata.description`, `spec` (payload +
      any cross-refs). Faithfully transcribe all 18 `catalog.toml` entries.
- [ ] Drop the built-in half of `catalog.publish_to` (the four `builtin.<kind>` loops); it now
      publishes only the operator TOML extensions. `builtin_manifests.publish_to` (already called
      first in `bootstrap.py`) picks up the new files. Move `catalog.publish_to` next to
      `Config.publish_to` in `bootstrap.py` if it reads cleaner as an operator publisher; verify the
      built-in-before-operator ordering invariant still holds.
- [ ] Parity: assert the Registry's built-in catalog rows (via `catalog_from_registry` on a
      no-operator config) match the Phase 0 oracle exactly, and carry `origin.variant == "built-in"`
      with a source pointing at the bundled file.
- [ ] Override test: an operator override of a built-in catalog entry (manifest and TOML) still
      wins.

**Done when:** built-in catalog comes from the bundled manifests with identical payloads, and
overrides still work, while the old `catalog.toml` still exists (deleted in Phase 3).

## Phase 2: catalog entries gain `declared_at`

- [ ] Give the four catalog `_load_*` helpers an optional `decls: _SectionLineMap` param (default a
      synthesized-returning shim) and set `declared_at=decls.lookup(<section>, name)` per entry.
- [ ] Catalog manifest decoders pass `_decls(doc.location)`; the operator TOML publisher passes the
      real `_SectionLineMap` if readily available, else the default.
- [ ] Test: a bundled built-in entry and an operator manifest entry both resolve with a real
      `declared_at` (bundled file for built-ins, operator file for operator manifests); the
      resource-describe output surfaces the location.
- [ ] Update the stale `catalog.py` comment that framed `declared_at` as an open follow-up.

**Done when:** manifest-loaded catalog entries carry a real source location.

## Phase 3: delete the bespoke code and migrate its tests

- [ ] Re-grep for any remaining production consumer of `load_catalog` / `load_builtin_catalog` /
      `catalog.toml` / `_parse_catalog` / `_BUILTIN_CATALOG_PATH` (expected: none outside tests).
- [ ] Delete `catalog.toml`, `_BUILTIN_CATALOG_PATH`, `load_builtin_catalog`, `_parse_catalog`,
      `load_catalog`, and the TOML-section helpers used only by them. Keep the per-entry `_load_*`
      loaders (still used by the operator TOML publisher and the manifest decoders),
      `catalog_from_registry`, and `ResolvedCatalog`.
- [ ] Migrate `test_catalog.py` and `test_config_resource_read_guard.py`: re-express parse-level
      assertions against a built Registry via `catalog_from_registry` / the bundled-manifest output;
      delete tests that only exercised the deleted parse functions.

**Done when:** the bespoke built-in-catalog path is gone, a grep for the deleted symbols is empty,
and the suite is green.

## Phase 4: docs, close-out, lock

- [ ] Update any doc that describes the built-in catalog as `catalog.toml` (grep docs/ and the
      catalog module docstring); ADR 0016 note if it references the catalog's definition mechanism.
- [ ] `agw resource sample apt-source` etc. unaffected (samples are separate); confirm.
- [ ] agentworks-reviewer round; then a fresh-eyes senior-dev (Sonnet) pass since Copilot is
      quota-limited; address findings.
- [ ] Non-draft PR; on merge write `locked.md` (what shipped, permanent homes: the bundled manifests
      and `catalog_from_registry` are self-documenting; the built-in-manifest mechanism is described
      in `manifests/builtin.py`), noting the operator TOML catalog surface remains until the broader
      TOML retirement.

**Done when:** built-in catalog is bundled manifests, the bespoke path is deleted, docs match HEAD,
and the SDD is locked.

## Notes

- Registry-as-source-of-truth for consumption was already true (`catalog_from_registry`); this
  effort removes the parallel `load_catalog` definition path so it cannot drift, and brings the
  built-in catalog definition into the one manifest path every other resource already uses.
- Operator TOML catalog (`[apt_sources.*]` etc.) stays until the broader TOML-resource deprecation;
  it is not this effort's to retire.
