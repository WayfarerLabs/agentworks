# Mise Integration - Implementation Plan

## Definition of Done

- Mise is installed system-wide via apt during VM init and is always installed. The planned
  `install_mise` toggle was not introduced; adding it during closeout would be new feature work.
- Shell activation is configured per user through the managed `.agentworks-rc.sh` fragment.
- Per-user shims PATH is added to `~/.agentworks-path.sh`.
- `mise_packages` (optional list of `name@version` strings) is supported in both YAML
  `admin-template` and `agent-template` resources.
- `mise_lockfile` (optional source reference: local path or `git::` URL) is supported in both
  template kinds for providing a user-managed `mise.lock`.
- `mise_allow_unlocked` (default false) controls whether unlocked packages are installed with a
  warning or rejected.
- `mise_install_before` (default `"7d"`) filters out newly published versions.
- Dotfiles are synced before mise per-user setup so dotfiles-provided mise config/lockfiles work.
- No mise-specific catalog entries (the old `MisePackageEntry`, checksums, urls are removed).
- Agents default to nothing unless explicitly configured.
- Sample config documents all new settings.
- All changes pass ruff and mypy.

## Phase 1: Strip Catalog and Simplify Config

Remove the mise catalog machinery and simplify to the new model.

- [x] Remove `MisePackageEntry` dataclass from `catalog.py`.
- [x] Remove `_load_mise_packages()`, `VALID_MISE_PLATFORMS`, `VALID_MISE_BACKENDS`, `_CHECKSUM_RE`
      from `catalog.py`.
- [x] Remove `mise_packages` field from `ResolvedCatalog`.
- [x] Remove mise sections from `_parse_catalog()` and `load_catalog()`.
- [x] Remove mise validation from `validate_selections()`.
- [x] Remove `[mise_packages.*]` sections from `catalog.toml`.
- [x] Remove `mise_packages` raw dict from `Config` dataclass.
- [x] Remove `"mise_packages"` from `_load_catalog_sections()` and `EXPECTED_TOP_LEVEL_KEYS`.
- [x] Remove `"mise-package"` from installer CLI type choices, `_CONFIG_ATTR`, and the
      `installer list` / `installer describe` handlers.
- [x] Update `VMConfig`: change `mise_packages` from `list[str]` (catalog refs) to `list[str]`
      (`name@version` strings). Add `mise_lockfile` (optional str, source reference),
      `mise_allow_unlocked` (bool, default false), `mise_install_before` (str, default `"7d"`).
- [x] Update `AgentConfig`: add `mise_packages`, `mise_lockfile`, `mise_allow_unlocked`,
      `mise_install_before` with same types and defaults.
- [x] Update config TOML parsing for both sections.

Done when: config loads cleanly with the new fields, catalog has no mise concept.

## Phase 2: Source Reference Module

Implement the reusable source reference primitive.

- [x] Create `cli/agentworks/sources.py` with:
  - `SourceRef` dataclass (kind, path, subpath, ref).
  - `parse_source_ref()` function that parses `file::`, `git::`, or bare path strings.
  - `fetch_file()` function that resolves a `SourceRef` to a file on an `ExecTarget`:
    - File sources: copy via `ExecTarget.copy_to()`.
    - Git sources: shallow clone to temp dir on target, copy subpath file to dest, clean up.
  - Validation: git URLs must be https/git@, no `..` in subpath, ref is safe characters.
- [x] Add tests or at least manual verification of parse edge cases.

Done when: `parse_source_ref("git::https://example.com/repo.git//path/file?ref=main")` returns a
correct `SourceRef` and `fetch_file()` can resolve both local and git sources.

## Phase 3: Rework Installation Logic

Rewrite the mise per-user setup to use the new model.

- [x] Rewrite `_install_mise_packages()` in `initializer.py`:
  - Write `~/.config/mise/config.toml` from `mise_packages` list (simple `name = "version"` format)
    with `install_before` in `[settings]`. Skip if no `mise_packages` configured (dotfiles may
    provide the config).
  - Fetch `mise_lockfile` (via `sources.fetch_file()`) to `~/.config/mise/mise.lock` if configured.
    Supports local paths and `git::` URLs. Runs after git credentials.
  - Determine if a lockfile is present (from config path, repo, or dotfiles).
  - If lockfile present: run `mise install --locked`. On failure, check `mise_allow_unlocked`:
    - false: log warning with details.
    - true: warn about unlocked packages, re-run `mise install`.
  - If no lockfile: run `mise install`.
- [x] Remove `_detect_mise_platform()` and `_MISE_PLATFORM_MAP` (no longer needed).
- [x] Reorder `_phase_b_setup()`:
  - Mise config write happens before dotfiles.
  - Mise lockfile copy/clone and `mise install` happen after dotfiles and git credentials.
- [x] Rewrite `_run_agent_mise_setup()` in `agents/manager.py` with same logic.
- [x] Clean up unused imports.

Done when: `mise install` uses user-provided lockfiles, `mise_allow_unlocked` controls fallback
behavior, `mise_install_before` is written to mise settings.

## Phase 4: Sample Config and Docs

- [x] Update `sample-config.toml`: replace mise catalog sections with new per-user settings
      (`mise_packages`, `mise_lockfile`, `mise_allow_unlocked`, `mise_install_before`) in both
      `[vm.config]` and `[agent.config]`. Include examples of both local and `git::` lockfile
      sources.
- [x] Remove commented-out `[mise_packages.example]` section from sample config.
- [x] Update comments to clarify the distinction between VM-level and admin-user settings.
- [x] Add `"mise_lockfile"` to completions if it becomes a CLI argument (likely not needed since it
      is config-driven).

Done when: the shipped declarative resource samples show the new settings clearly. The original
`agentworks config sample` resource surface was superseded by YAML manifests.

## Phase 5: Verification

- [x] Run ruff and mypy across all changed files.
- [ ] Manual test: reinit with `mise_packages` and no lockfile (should install unlocked).
- [ ] Manual test: reinit with `mise_packages` and a lockfile (should install locked).
- [ ] Manual test: lockfile missing a package with `mise_allow_unlocked = false` (should fail).
- [ ] Manual test: lockfile missing a package with `mise_allow_unlocked = true` (should warn and
      install).
- [ ] Manual test: dotfiles providing mise config without `mise_packages` (should pick up dotfiles).
- [x] Verify agent creation respects agent-specific mise config.

Closeout disposition (2026-08-05): the five manual reinit checks above are waived for this effort,
not completed. The reconciliation added focused automated coverage for each configuration and
install-flow branch, but it did not provision a live VM. Keeping the boxes unchecked preserves that
verification limitation while making clear that they are not open implementation work.

## 2026-08-05 reconciliation record

The unchecked historical implementation items above were audited against the current source, tests,
samples, and permanent documentation. Checked items have direct evidence at HEAD. The original
implementation evolved in these ways:

- Declarative YAML `admin-template` and `agent-template` resources superseded classic TOML resource
  declarations. Classic TOML is accepted only by `agw resource migrate`, not by runtime loading.
- Mise became always-installed VM infrastructure. The proposed `install_mise` setting does not exist
  and is intentionally not being added during reconciliation.
- Shell activation moved from the proposed system-wide profile fragment to the managed per-user
  shell fragment, matching the admin and agent activation settings.
- The proposed mise catalog types and selectors are absent. Per-user `name@version` declarations are
  the only Agentworks-owned mise package surface.
- Implementation moved into focused initializer modules rather than retaining the file locations
  named by this early plan. The delivered behavior, not the obsolete module path, is what the
  checked items record.
- Focused automated tests now cover source-reference parsing and local/git fetch behavior, VM mise
  config rendering, locked and unlocked install branches, and agent-specific mise setup. Manifest
  and migration decoding reject malformed package, source-reference, and install-order values before
  provisioning.

No live VM reinit matrix was performed during this reconciliation. The five manual reinit items
remain unchecked and must not be inferred from the automated coverage.
