# Mise Integration - Implementation Plan

## Definition of Done

- Mise is installed system-wide via apt during VM init, hardcoded in the initializer. Controllable
  via `install_mise` config flag, default true.
- Shell activation is system-wide via `/etc/profile.d/mise.sh`.
- Per-user shims PATH is added to `~/.agentworks-path.sh`.
- `mise_packages` (optional list of `name@version` strings) is supported in both `vm.config` (admin)
  and `agent.config`.
- `mise_lockfile` (optional source reference: local path or `git::` URL) is supported in both
  configs for providing a user-managed `mise.lock`.
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

- [ ] Remove `MisePackageEntry` dataclass from `catalog.py`.
- [ ] Remove `_load_mise_packages()`, `VALID_MISE_PLATFORMS`, `VALID_MISE_BACKENDS`, `_CHECKSUM_RE`
      from `catalog.py`.
- [ ] Remove `mise_packages` field from `ResolvedCatalog`.
- [ ] Remove mise sections from `_parse_catalog()` and `load_catalog()`.
- [ ] Remove mise validation from `validate_selections()`.
- [ ] Remove `[mise_packages.*]` sections from `catalog.toml`.
- [ ] Remove `mise_packages` raw dict from `Config` dataclass.
- [ ] Remove `"mise_packages"` from `_load_catalog_sections()` and `EXPECTED_TOP_LEVEL_KEYS`.
- [ ] Remove `"mise-package"` from installer CLI type choices, `_CONFIG_ATTR`, and the
      `installer list` / `installer describe` handlers.
- [ ] Update `VMConfig`: change `mise_packages` from `list[str]` (catalog refs) to `list[str]`
      (`name@version` strings). Add `mise_lockfile` (optional str, source reference),
      `mise_allow_unlocked` (bool, default false), `mise_install_before` (str, default `"7d"`).
- [ ] Update `AgentConfig`: add `mise_packages`, `mise_lockfile`, `mise_allow_unlocked`,
      `mise_install_before` with same types and defaults.
- [ ] Update config TOML parsing for both sections.

Done when: config loads cleanly with the new fields, catalog has no mise concept.

## Phase 2: Source Reference Module

Implement the reusable source reference primitive.

- [ ] Create `cli/agentworks/sources.py` with:
  - `SourceRef` dataclass (kind, path, subpath, ref).
  - `parse_source_ref()` function that parses `file::`, `git::`, or bare path strings.
  - `fetch_file()` function that resolves a `SourceRef` to a file on an `ExecTarget`:
    - File sources: copy via `ExecTarget.copy_to()`.
    - Git sources: shallow clone to temp dir on target, copy subpath file to dest, clean up.
  - Validation: git URLs must be https/git@, no `..` in subpath, ref is safe characters.
- [ ] Add tests or at least manual verification of parse edge cases.

Done when: `parse_source_ref("git::https://example.com/repo.git//path/file?ref=main")` returns a
correct `SourceRef` and `fetch_file()` can resolve both local and git sources.

## Phase 3: Rework Installation Logic

Rewrite the mise per-user setup to use the new model.

- [ ] Rewrite `_install_mise_packages()` in `initializer.py`:
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
- [ ] Remove `_detect_mise_platform()` and `_MISE_PLATFORM_MAP` (no longer needed).
- [ ] Reorder `_phase_b_setup()`:
  - Mise config write happens before dotfiles.
  - Mise lockfile copy/clone and `mise install` happen after dotfiles and git credentials.
- [ ] Rewrite `_run_agent_mise_setup()` in `agents/manager.py` with same logic.
- [ ] Clean up unused imports.

Done when: `mise install` uses user-provided lockfiles, `mise_allow_unlocked` controls fallback
behavior, `mise_install_before` is written to mise settings.

## Phase 4: Sample Config and Docs

- [ ] Update `sample-config.toml`: replace mise catalog sections with new per-user settings
      (`mise_packages`, `mise_lockfile`, `mise_allow_unlocked`, `mise_install_before`) in both
      `[vm.config]` and `[agent.config]`. Include examples of both local and `git::` lockfile
      sources.
- [ ] Remove commented-out `[mise_packages.example]` section from sample config.
- [ ] Update comments to clarify the distinction between VM-level and admin-user settings.
- [ ] Add `"mise_lockfile"` to completions if it becomes a CLI argument (likely not needed since it
      is config-driven).

Done when: `agentworks config sample` shows the new settings clearly.

## Phase 5: Verification

- [ ] Run ruff and mypy across all changed files.
- [ ] Manual test: reinit with `mise_packages` and no lockfile (should install unlocked).
- [ ] Manual test: reinit with `mise_packages` and a lockfile (should install locked).
- [ ] Manual test: lockfile missing a package with `mise_allow_unlocked = false` (should fail).
- [ ] Manual test: lockfile missing a package with `mise_allow_unlocked = true` (should warn and
      install).
- [ ] Manual test: dotfiles providing mise config without `mise_packages` (should pick up dotfiles).
- [ ] Verify agent creation respects agent-specific mise config.
