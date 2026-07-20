# Plan: dissolve the catalog abstraction

Implements the [FRD](./frd.md) per the [HLA](./hla.md). A data move plus a code reorganization, with
the consumption side already registry-sourced.

Each phase ends green: from `cli/`,
`uv run pytest -q && uv run ruff check . && uv run mypy agentworks`; then from repo root
`./scripts/lint-files.sh`. New behavior lands with tests in-phase. Phases 1 to 5 are delegated to
`agentworks-dev`, reviewed per step.

## Phase 0: parity oracle

- [x] Before touching anything, snapshot the 18 built-in entries' resolved payloads (per kind: name
      to payload fields) into a fixture/constants in a new parity test, captured from the current
      `load_builtin_catalog()`. This is the no-drift reference for later phases.

**Done when:** a test pins the current built-in payloads; suite green.

## Phase 1: built-in entries become bundled manifests

- [x] Author the bundled manifests under `cli/agentworks/manifests/builtin/` (`apt-sources.yaml`,
      `apt-packages.yaml`, `install-commands.yaml`, `---`-separated documents), faithfully
      transcribing all 18 `catalog.toml` entries (`kind` / `metadata.name` / `metadata.description`
      / `spec`, cross-refs in `spec`).
- [x] Remove the built-in half of `catalog.publish_to`; `builtin_manifests.publish_to` (already
      first in `bootstrap.py`) now supplies the built-in entries.
- [x] Parity: the Registry's built-in rows (via a Registry read on a no-operator config) match the
      Phase 0 oracle exactly, with `origin.variant == "built-in"` and a bundled-file source.
      Override test: an operator override (manifest and TOML) still wins.

**Done when:** built-in entries come from bundled manifests with identical payloads; `catalog.toml`
still present (deleted in Phase 4).

## Phase 2: `declared_at` threading

- [x] Give the per-entry loaders an optional `decls: _SectionLineMap` param (default synthesized
      shim); set `declared_at=decls.lookup(<section>, name)` per entry. Manifest decoders pass
      `_decls(doc.location)`; the operator TOML publisher passes the real map if handy, else
      default.
- [x] Test: a bundled built-in entry and an operator YAML entry both resolve with a real
      `declared_at`; describe output surfaces the location. Remove the stale `declared_at` follow-up
      comment.

**Done when:** manifest-loaded entries carry a real source location.

## Phase 3: regroup the kinds into `apt` and `install_commands`

- [ ] Create `agentworks/apt.py` (`AptSourceEntry`, `AptPackageEntry`, their kind strategies, the
      apt loaders, the apt cross-reference) and `agentworks/install_commands.py`
      (`SystemInstallCommandEntry`, `UserInstallCommandEntry`, their kind strategies, the
      install-command loaders, `_load_test_fields`). Move the surviving pieces out of `catalog.py`.
- [ ] Drop `CatalogError`; loaders raise `ConfigError` / `ExternalError` directly. Update the
      `resources/kinds` self-registration index and the manifest decoders' imports to the new
      modules.
- [ ] Move the operator-TOML publisher half into the new modules (small `publish_to` per module or
      one shared operator publisher); wire `bootstrap.py` to call it after the built-in publishers,
      preserving override ordering.

**Done when:** the kinds live in the two affinity modules and self-register; no `catalog` module
symbol is imported except the still-present bespoke code (deleted in Phase 4).

## Phase 4: dissolve `ResolvedCatalog` and delete the bespoke code

- [ ] Re-grep for any remaining consumer of `ResolvedCatalog` / `catalog_from_registry` /
      `load_catalog` / `load_builtin_catalog` / `catalog.toml` / `_parse_catalog` (expected: the
      initializers plus the functions themselves).
- [ ] Point the initializers at direct Registry reads (`kind_dict(registry, "apt-package")` etc.) in
      place of the `ResolvedCatalog` input; the selection / install logic is otherwise unchanged.
- [ ] Delete `ResolvedCatalog`, `catalog_from_registry`, `catalog.toml`, `_BUILTIN_CATALOG_PATH`,
      `load_builtin_catalog`, `_parse_catalog`, `load_catalog`, the TOML-file helpers used only by
      them, and the now-empty `catalog.py`.
- [ ] Migrate `test_catalog.py` and `test_config_resource_read_guard.py`: re-express parse-level
      assertions against a built Registry; delete tests that only exercised the removed parse
      functions; keep the Phase 0 parity assertion pointed at the Registry.

**Done when:** a grep for the retired symbols and the `catalog` module is empty; consumers read the
Registry; suite green.

## Phase 5: docs, close-out, lock

- [ ] Update docs referencing the built-in catalog as `catalog.toml` or the "catalog" concept (grep
      `docs/`, ADR 0016 if it names the mechanism, module docstrings).
- [ ] agentworks-reviewer round; then a fresh-eyes senior-dev (Sonnet) pass (Copilot quota-limited);
      address findings.
- [ ] Non-draft PR; on merge write `locked.md` (what shipped; permanent homes: `apt.py` /
      `install_commands.py` and the bundled manifests are self-documenting, the built-in-manifest
      mechanism is described in `manifests/builtin.py`), noting the operator-TOML surface for these
      kinds remains until the broader TOML retirement.

**Done when:** the catalog abstraction is gone, the kinds live in the two affinity modules,
built-ins are bundled manifests, consumers read the Registry, docs match HEAD, and the SDD is
locked.

## Notes

- No replacement umbrella (not "catalog," not "installers"): the four kinds are grouped by affinity
  (`apt`, install-commands) with no wrapping concept, per the FRD rationale.
- Registry-as-source-of-truth for consumption was already true; this removes the parallel
  `load_catalog` definition path and the `ResolvedCatalog` snapshot so nothing can drift.
- The operator-TOML surface for these kinds stays until the broader TOML-resource deprecation.
