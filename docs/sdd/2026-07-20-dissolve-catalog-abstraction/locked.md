# Dissolve the catalog abstraction: lockfile

## 2026-07-20

The SDD is complete and locked as of this date. All plan phases are done (every checkbox in
`plan.md` is checked). The "catalog" abstraction is gone: there is no `catalog` module, no
`ResolvedCatalog` bundle, and no bespoke built-in-catalog code. What used to be "the catalog" is now
four ordinary resource kinds grouped by affinity, with the built-in entries shipping as bundled
manifests like every other built-in resource, and consumers reading them from the Registry.

### What shipped

- **Built-in entries are bundled manifests.** The 18 built-in entries moved from the hand-parsed
  `catalog.toml` to `cli/agentworks/manifests/builtin/` as `apt-sources.yaml`, `apt-packages.yaml`,
  and `install-commands.yaml`, published through the existing bundled-manifest loader
  (`manifests/builtin.py`) with `Origin.built_in` and a shipped-file source, exactly like
  `vm-sites.yaml`. A parity oracle (`test_builtin_entries_parity.py`) pins their resolved payloads.
- **No bespoke definition code.** `catalog.toml`, `load_builtin_catalog`, `_parse_catalog`,
  `load_catalog`, and the built-in half of the old catalog publisher were deleted.
- **The kinds regrouped by affinity, no umbrella.** `cli/agentworks/apt.py` (`apt-source` +
  `apt-package`, which cross-reference) and `cli/agentworks/install_commands.py`
  (`system-install-command` + `user-install-command`, same shape differing only in execution scope).
  Shared loader helpers live in `cli/agentworks/resource_loading.py`; the shared error-miss-policy
  synthesize body is `synthesize_no_default` in `resources/kind.py`. The `catalog` module,
  `ResolvedCatalog`, and `CatalogError` are gone. No replacement concept was introduced:
  "installers" was considered and rejected (it would imply mise, template apt, and dotfile setup
  belong, which are template mechanisms, not resource kinds).
- **Consumers read the Registry directly.** The VM and agent initializers read the kinds via
  `kind_dict(registry, "apt-package")` etc. rather than a re-bundled snapshot.
  `catalog_from_registry` and the `ResolvedCatalog` bundle were deleted.
- **Catalog entries gained a real `declared_at`.** The manifest decoders thread the document
  location, so manifest-loaded entries (built-in and operator YAML) carry a `declared_at` pointing
  at their shipped or operator file.
- **Vocabulary sweep.** Residual "catalog" naming and stale `agentworks.catalog` references were
  removed (`_load_catalog_sections` to `_load_apt_and_install_sections`, `_run_catalog_commands` to
  `_run_install_commands`, docstrings, test file names, docs). The operator-facing TOML surface for
  these kinds is unchanged.

### Permanent homes (SDD-not-permanent)

Nothing under this directory is load-bearing after merge; the directory is deletable.

- **The two affinity modules** (`apt.py`, `install_commands.py`) and the **bundled manifests** under
  `manifests/builtin/` are self-documenting. The bundled-manifest mechanism is described in
  `manifests/builtin.py` and its `README.md`.
- **`cli/README.md` / `docs/guides/resources.md`**: the operator story for these kinds (declared via
  manifest, or the deprecated TOML sections) reflects HEAD.

### Deliberately out of scope (unchanged)

- The operator **TOML** surface for these kinds (`[apt_sources.*]` etc.) stays until the broader
  TOML-resource deprecation (ADR 0016), served by the per-module operator publishers.
- The kinds' payload shapes, the apt cross-reference model, and the initializer install logic.

### Review history

Phases 0-2 (built-in to manifests + `declared_at`) and Phase 3-4 (the reorg) each went through an
`agentworks-reviewer` round; the code-heavy reorg additionally got a fresh-eyes senior-dev (Sonnet)
pass since Copilot was quota-limited. Both reviews confirmed the reorg is behavior-preserving
(parity proven both directions, the initializer rewire verified exact, no import cycles). Minor doc
findings were folded into the Phase 5 sweep. The FRD, HLA, and plan are accurate as-built and are
now locked.

### Also cleaned up

- Removed the dead `HasDescription` Protocol from `cli/agentworks/cli/_helpers.py` (no call sites;
  orphaned once `DeclaredResource` unified `description` and the display path moved to generic
  `getattr`). Surfaced incidentally during the vocabulary sweep and pulled into this PR as a
  leave-it-better cleanup.
