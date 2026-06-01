# Install Enhancements -- High-Level Architecture

**Status:** Draft

---

## Overview

This document describes the architecture for restructuring agentworks install-time operations into
four clearly separated concepts (apt sources, apt packages, system install commands, and per-user
install commands) with a built-in catalog and CLI discoverability.

---

## Component Overview

```text
~/.config/agentworks/config.toml     cli/agentworks/catalog.toml
         (user-defined)                    (built-in, read-only)
                \                          /
                 \                        /
                  v                      v
              catalog.py  (merge: user wins on collision)
                  |
                  v
          Resolved Catalog
        /      |         |        \
       v       v         v         v
  AptSources AptPkgs SystemCmds UserCmds
       \       |         |        /       \
        v      v         v       v         v
          initializer.py              agent/manager.py
          (VM init/reinit)            (agent creation)
```

---

## Built-in Catalog

### File Location

The built-in catalog is a single TOML file shipped alongside the Python package:

```text
cli/agentworks/catalog.toml
```

This file is read-only at runtime and is loaded via `importlib.resources` (or `__file__`-relative
path resolution) so it works regardless of installation method.

### Catalog Schema

The catalog TOML has four top-level tables matching the four concepts:

```toml
# -- Apt sources --
# Each entry defines a third-party apt repository.

[apt_sources.github-cli]
description = "GitHub CLI official apt repository"
key_url = "https://cli.github.com/packages/githubcli-archive-keyring.gpg"
key_path = "/usr/share/keyrings/githubcli-archive-keyring.gpg"
source = "deb [arch={arch} signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main"
source_file = "github-cli.list"

[apt_sources.docker]
description = "Docker official apt repository"
key_url = "https://download.docker.com/linux/debian/gpg"
key_path = "/usr/share/keyrings/docker-archive-keyring.gpg"
source = "deb [arch={arch} signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian bookworm stable"
source_file = "docker.list"

[apt_sources.hashicorp]
description = "HashiCorp official apt repository"
key_url = "https://apt.releases.hashicorp.com/gpg"
key_path = "/usr/share/keyrings/hashicorp-archive-keyring.gpg"
source = "deb [arch={arch} signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com bookworm main"
source_file = "hashicorp.list"

# -- Apt packages --
# Named sets of apt packages with optional apt source dependencies.

[apt_packages.gh]
description = "GitHub CLI"
apt_sources = ["github-cli"]
apt = ["gh"]

[apt_packages.docker]
description = "Docker Engine (CE)"
apt_sources = ["docker"]
apt = ["docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin"]

[apt_packages.terraform]
description = "HashiCorp Terraform"
apt_sources = ["hashicorp"]
apt = ["terraform"]

# -- System install commands --
# Shell commands that install system-wide tooling (run once per VM).

[system_install_commands.az-cli]
description = "Azure CLI"
command = "curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash"

# -- Per-user install commands --
# Shell commands that install per-user tooling (run for each user).

[user_install_commands.bun]
description = "Bun JavaScript runtime"
command = "curl -fsSL https://bun.sh/install | bash"
path = ["~/.bun/bin"]

[user_install_commands.claude]
description = "Claude Code CLI"
command = "npm install -g @anthropic-ai/claude-code"
path = ["~/.npm-global/bin"]
```

### Resolution

A new module `cli/agentworks/catalog.py` handles loading and merging:

1. Load the built-in catalog from `catalog.toml`
2. Load user-defined entries from config (same four sections)
3. Merge with user entries taking precedence on name collision
4. Validate all cross-references (e.g. `apt_sources` references in apt packages resolve)
5. Return a `ResolvedCatalog` object used by the initializer and agent manager

Pseudocode:

```python
def load_catalog(config: Config) -> ResolvedCatalog:
    builtin = load_toml("catalog.toml")
    user = extract_catalog_sections(config)
    merged = {**builtin, **user}  # user wins per section per name
    validate_references(merged)
    return ResolvedCatalog(merged)
```

---

## Config Changes

### User Config (config.toml)

New and renamed sections:

```toml
# User-defined apt sources (same schema as catalog)
[apt_sources.my-internal-repo]
description = "Internal team apt repo"
key_url = "https://apt.internal.example.com/key.gpg"
key_path = "/usr/share/keyrings/internal-repo-keyring.gpg"
source = "deb [arch={arch} signed-by=/usr/share/keyrings/internal-repo-keyring.gpg] https://apt.internal.example.com/debian bookworm main"
source_file = "internal-repo.list"

# User-defined apt package sets (same schema as catalog)
[apt_packages.internal-tool]
description = "Internal CLI tool from our apt repo"
apt_sources = ["my-internal-repo"]
apt = ["internal-tool"]

# User-defined system install commands (same schema as catalog)
[system_install_commands.my-system-tool]
description = "Custom system-wide tool installed via script"
command = "curl -fsSL https://example.com/install-system.sh | bash"

# User-defined per-user install commands (same schema as catalog)
[user_install_commands.my-tool]
description = "Custom per-user tool"
command = "curl -fsSL https://example.com/install.sh | bash"
path = ["~/.my-tool/bin"]

# VM config -- separate selection lists for each type
[vm.config]
apt = ["zsh", "ripgrep"]                   # direct apt packages (unchanged)
apt_packages = ["gh", "docker"]            # named apt package sets from catalog
system_install_commands = ["az-cli"]       # system-level shell commands from catalog
admin_user_install_commands = ["bun", "claude"]  # per-user shell commands for admin user
snap = []                                  # direct snap packages (unchanged)

# Agent config -- per-user commands only
[agent.config]
user_install_commands = ["claude"]
```

---

## Declarative Initialization Model

### Principle

VM initialization is fully driven by config. There are no CLI arguments that override what gets
installed or configured during init. This makes `vm reinit` a simple "re-apply config" operation
with no arguments beyond the VM name.

### Provisioning vs Initialization Parameters

Parameters split cleanly into two categories:

**Immutable provisioning parameters** (set once at `vm create`, persisted in DB):

- `--name` -- VM name
- `--platform` -- lima, azure, or wsl2
- `--vm-host` -- VM host for remote Lima
- `--cpus`, `--memory`, `--disk` -- resource allocation
- `--azure-vm-size` -- Azure-specific sizing
- `--vm-user` -- Linux username on the VM

These describe the shape of the VM and cannot be changed after creation. They are stored in the
`vms` table and read back for lifecycle operations (start, stop, shell, etc.).

**Config-driven initialization** (read from config on every init/reinit):

- Apt sources, apt packages, system install commands, per-user install commands
- Shell preference
- Git credentials
- Dotfiles
- SSH authorized keys

These come entirely from `~/.config/agentworks/config.toml`. Changing the config and running
`vm reinit` picks up the changes.

### CLI Changes

Current `vm create` arguments that are removed:

- `--extra-packages` -- use `vm.config.apt` instead
- `--git-credentials` -- use `defaults.git_credentials` instead

Current `vm reinit` arguments that are removed:

- `--git-credentials` -- use `defaults.git_credentials` instead

After this change:

- `vm create [--name] [--platform] [--vm-host] [--cpus] [--memory]`
  `[--disk] [--azure-vm-size] [--vm-user]`
- `vm reinit <name>` (no options)

### Future: VM Templates

VM templates (future work) will allow named profiles in config that bundle both provisioning
defaults and initialization config. The selected template name becomes another immutable
provisioning parameter stored in the DB.

---

## Initialization Flow Changes

### Phase B: VM Initialization

The current Phase B step order is:

1. User apt packages
2. Snap packages
3. Shell configuration
4. SSH authorized keys
5. Install commands (current flat list)
6. PATH additions
7. Git credentials
8. Dotfiles

The new order restructures steps 1 and 5:

1. **Apt sources** (new) -- configure any apt sources required by selected `vm.config.apt_packages`
   entries. For each source: check if GPG key exists at `key_path`; if not, download from `key_url`
   and write the source list. Run `apt-get update` once after all sources are configured.
2. **Apt packages** -- install `vm.config.apt` (direct list, unchanged) plus apt packages from
   selected `vm.config.apt_packages` catalog entries. Single `apt-get install` call.
3. Snap packages (unchanged)
4. Shell configuration (unchanged)
5. SSH authorized keys (unchanged)
6. **System install commands** (new) -- run shell commands from `vm.config.system_install_commands`
   in the admin user's login shell. These install system-wide tooling.
7. **Per-user install commands** -- run commands from `vm.config.admin_user_install_commands` in the
   admin user's login shell. These install into the admin user's home directory.
8. PATH additions (from system and per-user install commands)
9. Git credentials (unchanged)
10. Dotfiles (unchanged)

### Agent Creation

Currently agent install commands are not wired up. The new flow:

1. Run commands from `agent.config.user_install_commands` for the agent user (in agent's login
   shell)
2. Write PATH additions from those commands to agent's `~/.agentworks-path.sh`

### Apt Source Idempotency

```text
for source in required_apt_sources:
    if key_path exists on VM:
        log "apt source {name} already configured, skipping"
        continue
    download key_url to key_path
    write source entry to /etc/apt/sources.list.d/{source_file}

if any sources were newly configured:
    apt-get update
```

### Architecture Resolution

The `{arch}` placeholder in apt source definitions is resolved at install time. The target
architecture is detected via `dpkg --print-architecture` on the VM (returns `amd64` or `arm64`).
This value is cached for the duration of the init run.

---

## CLI Commands

A new `installer` command group:

```text
agentworks installer list \
  [--type apt-source|apt-package|system-install-cmd|user-install-cmd] \
  [--source builtin|user]
agentworks installer describe <name>
```

### list

Displays a table of all available entries across all four types:

```text
TYPE              NAME           SOURCE    DESCRIPTION
apt-source        github-cli     built-in  GitHub CLI official apt repository
apt-source        docker         built-in  Docker official apt repository
apt-package       gh             built-in  GitHub CLI
apt-package       docker         built-in  Docker Engine (CE)
apt-package       internal-tool  user      Internal CLI tool from our apt repo
system-install-cmd az-cli        built-in  Azure CLI
user-install-cmd  bun            built-in  Bun JavaScript runtime
user-install-cmd  claude         built-in  Claude Code CLI
user-install-cmd  my-tool        user      Custom per-user tool
```

Filterable by `--type` and `--source`.

### describe

Shows the full definition of a single entry:

```text
$ agentworks installer describe gh

Name:        gh
Type:        apt-package
Source:      built-in
Description: GitHub CLI
Apt sources: github-cli
Apt:         gh
```

---

## File Changes

| File                                | Change                                                                                                                                                                                                                                  |
| ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cli/agentworks/catalog.toml`       | New: built-in catalog file                                                                                                                                                                                                              |
| `cli/agentworks/catalog.py`         | New: catalog loading, merging, resolution                                                                                                                                                                                               |
| `cli/agentworks/config.py`          | Add `apt_sources`, `apt_packages`, `system_install_commands`, `user_install_commands` sections; rename `vm.config.install_commands` to `admin_user_install_commands`; rename `agent.config.install_commands` to `user_install_commands` |
| `cli/agentworks/vms/initializer.py` | Restructure Phase B to use resolved catalog; add apt source setup step                                                                                                                                                                  |
| `cli/agentworks/agents/manager.py`  | Wire up agent install commands using catalog                                                                                                                                                                                            |
| `cli/agentworks/cli.py`             | Add `installer` command group; remove `--extra-packages` and `--git-credentials` from `vm create`; remove `--git-credentials` from `vm reinit`                                                                                          |
| `cli/agentworks/vms/manager.py`     | Remove `extra_packages` and `git_credentials` params from create/reinit; read from config directly                                                                                                                                      |
| `cli/agentworks/sample-config.toml` | Update to reflect new structure                                                                                                                                                                                                         |
| `cli/agentworks/completions/`       | Add `installer` group completions                                                                                                                                                                                                       |
| `cli/README.md`                     | Document new config structure, commands, and catalog                                                                                                                                                                                    |
| `cli/tests/test_catalog.py`         | New: catalog loading, merging, resolution tests                                                                                                                                                                                         |
| `cli/tests/test_config.py`          | Update for new config sections                                                                                                                                                                                                          |
