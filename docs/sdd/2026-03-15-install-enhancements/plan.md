# Install Enhancements -- Implementation Plan

**Status:** Active **Branch:** `feat/install-enhancements`

---

## Phase 1: Catalog Infrastructure and Config Changes

Foundation work: the catalog file, loader, and config schema changes.

### 1.1 Built-in Catalog File

- [x] Create `cli/agentworks/catalog.toml` with initial entries:
  - Apt sources: github-cli, docker, hashicorp, nodesource
  - Apt packages: gh, docker, terraform, nodejs
  - System install commands: az-cli
  - Per-user install commands: bun, claude, nvm
- [x] Verify catalog TOML parses correctly and covers common tools

**Definition of done:** `catalog.toml` exists, parses without error, and contains at least the
entries listed above.

### 1.2 Catalog Loader Module

- [x] Create `cli/agentworks/catalog.py` with data classes:
  - `AptSourceEntry(name, description, key_url, key_path, source, source_file)`
  - `AptPackageEntry(name, description, apt_sources, apt)`
  - `SystemInstallCommandEntry(name, description, command, path)`
  - `UserInstallCommandEntry(name, description, command, path)`
  - `ResolvedCatalog(apt_sources, apt_packages, system_install_commands, user_install_commands)`
- [x] Implement `load_builtin_catalog()` to load from bundled `catalog.toml`
- [x] Implement `load_catalog(config)` that merges built-in + user-defined entries (user wins)
- [x] Implement cross-reference validation (apt_sources references in apt packages)
- [x] LLD not needed -- catalog.py is straightforward
- [x] Write tests: `cli/tests/test_catalog.py`
  - Loads built-in catalog without error
  - User entries override built-in entries by name
  - Cross-reference validation catches bad apt_sources references
  - Selection validation catches unresolvable references

**Definition of done:** `load_catalog()` returns a `ResolvedCatalog` with merged entries. Tests
pass.

### 1.3 Config Schema Changes

- [x] Add `apt_sources` section loader in `config.py` (same schema as catalog apt sources)
- [x] Add `apt_packages` section loader in `config.py` (same schema as catalog apt packages)
- [x] Add `system_install_commands` section loader in `config.py` (same schema as catalog system
  commands)
- [x] Add `user_install_commands` section loader in `config.py` (same schema as catalog user install
  commands)
- [x] Add `vm.config.apt_packages` list (references apt package set names)
- [x] Add `vm.config.system_install_commands` list (references system install command names)
- [x] Add `vm.config.admin_user_install_commands` list (references user install command names)
- [x] Rename `vm.config.install_commands` to `vm.config.admin_user_install_commands`
- [x] Rename `agent.config.install_commands` to `agent.config.user_install_commands`
- [x] Validate that all references in `vm.config.apt_packages` resolve in the merged catalog
- [x] Validate that all references in `vm.config.system_install_commands` resolve in the merged
  catalog
- [x] Validate that all references in `vm.config.admin_user_install_commands` resolve in the merged
  catalog
- [x] Validate that all references in `agent.config.user_install_commands` resolve in the merged
  catalog
- [x] Update `sample-config.toml` to reflect new structure
- [x] Update tests in `cli/tests/test_config.py`

**Definition of done:** Config loads with new sections. Validation catches unresolvable references.
Sample config reflects new structure.

---

## Phase 2: Declarative Initialization

Remove CLI arguments that override initialization behavior. Config becomes the single source of
truth for what gets installed.

### 2.1 Remove Initialization Args from CLI

- [x] Remove `--extra-packages` from `vm create`
- [x] Remove `--git-credentials` from `vm create`
- [x] Remove `--git-credentials` from `vm reinit`
- [x] Update `vm reinit` to take only the VM name (no options)

### 2.2 Update Manager Functions

- [x] Remove `extra_packages` parameter from `create_vm()` in `vms/manager.py`
- [x] Remove `git_credentials` parameter from `create_vm()` and `reinit_vm()`
- [x] Read git credentials from `config.defaults.git_credentials` directly in the initializer
- [x] Update all callers (provisioners, initializer, db)

### 2.3 Update Completions

- [x] Remove completions for dropped arguments
- [x] Run completions tests

**Definition of done:** `vm create` only accepts provisioning shape params. `vm reinit <name>` has
no options. All initialization behavior comes from config.

---

## Phase 3: Initializer Changes

Wire the resolved catalog into VM initialization.

### 3.1 Apt Source Setup

- [x] Add `_configure_apt_sources()` function to `initializer.py`
  - Detect VM architecture via `dpkg --print-architecture`
  - For each required apt source: check if key_path exists, skip if so
  - Download GPG key via `curl` to key_path (with dearmor support)
  - Write source entry to `/etc/apt/sources.list.d/{source_file}`
  - Run `apt-get update` once if any sources were newly configured
- [x] Insert apt source step at the beginning of Phase B, before apt package installation
- [x] Write tests for idempotency logic (mock ExecTarget)

**Definition of done:** Apt sources are configured idempotently. Re-running skips existing sources.

### 3.2 Apt Packages in Phase B

- [x] Update Phase B apt package step to collect packages from both `vm.config.apt` (direct list)
  and resolved `vm.config.apt_packages` catalog entries
- [x] Ensure apt sources required by apt package entries are included in the apt source step
- [x] Single `apt-get install` call for all apt packages

**Definition of done:** `vm.config.apt_packages = ["gh"]` results in github-cli apt source being
configured and `gh` package being installed.

### 3.3 System Install Commands in Phase B

- [x] Add system install command step after apt packages, before per-user install commands
- [x] Run shell commands from `vm.config.system_install_commands` in the admin user's login shell
- [x] PATH additions from system install commands combined with user command PATH additions

**Definition of done:** `vm.config.system_install_commands = ["az-cli"]` results in the az CLI
being installed system-wide.

### 3.4 Per-User Install Commands in Phase B

- [x] Update Phase B install command step to use `vm.config.admin_user_install_commands`
- [x] PATH additions sourced from resolved catalog entries

**Definition of done:** Per-user commands run for the VM admin user using the new config key.

---

## Phase 4: Agent Install Commands

Wire per-user install commands into agent creation.

### 4.1 Agent Install Command Execution

- [x] In `agents/manager.py`, after creating the agent Linux user, run install commands from
  `agent.config.user_install_commands` in the agent's login shell (via `su`)
- [x] Write PATH additions to agent's `~/.agentworks-path.sh`
- [x] Source from agent's shell profiles (.profile and .bashrc)
- [x] Handle failures as non-fatal warnings (agent still usable, just missing a tool)

**Definition of done:** Agent creation runs per-user install commands for the agent. PATH is
configured. Failures warn but do not abort.

---

## Phase 5: CLI Discoverability

### 5.1 installer Command Group

- [x] Add `installer` Typer group to `cli.py`
- [x] Implement `installer list` with `--type` and `--source` filters
  - Table output: TYPE, NAME, SOURCE, DESCRIPTION
  - Types: `apt-source`, `apt-package`, `system-install-cmd`, `user-install-cmd`
  - Sources: `built-in`, `user`
- [x] Implement `installer describe <name>`
  - Show full entry details
  - Indicate source (built-in or user, and whether user is overriding built-in)
- [x] Update shell completions for new command group
  - Add `installer` group with `list` and `describe` subcommands
  - Dynamic completions for `describe` argument (catalog_entries completer)

**Definition of done:** `agentworks installer list` shows all entries. `describe` shows full
details. Completions work.

---

## Phase 6: Documentation and Cleanup

### 6.1 Documentation

- [x] Update `cli/README.md`:
  - Document new config structure (apt_sources, apt_packages, system_install_commands,
    user_install_commands)
  - Document `installer` CLI commands
  - Update VM initialization section to describe new step order
  - Document built-in catalog and override behavior
- [x] Update sample config comments for clarity

### 6.2 Completions

- [x] Verify all new commands appear in shell completions
- [x] Run completions tests

### 6.3 Final Validation

- [x] Full test suite passes (100 tests)
- [ ] Manual test: create a VM with `apt_packages = ["gh"]` and verify gh is installed
- [ ] Manual test: `vm reinit` skips already-configured apt sources
- [ ] Manual test: `installer list` shows expected output
- [ ] Spell check documentation with cspell
