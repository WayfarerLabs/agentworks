# Install Enhancements -- Functional Requirements Document

**Status:** Draft

---

## Overview

Agentworks currently supports a flat list of install commands that run during VM initialization and
are referenced by name from `vm.config` and `agent.config`. This works but has several limitations:

1. No distinction between system-level install commands (run once, install for everyone) and
   per-user install commands (tools that install into `$HOME`).
2. Every command must be defined in the user's config file, even for common tools (bun, claude, gh).
   This creates boilerplate and makes it easy to get definitions wrong.
3. There is no automated support for third-party apt sources (GitHub CLI, Docker, HashiCorp, etc.).

This effort restructures install-time operations into clearly separated concepts, introduces a
built-in catalog that ships with agentworks, and adds CLI discoverability for available commands.

---

## Goals

- Cleanly separate system-level setup (apt sources, apt packages, and system install commands) from
  per-user install commands
- Ship a built-in catalog of common tools so users can easily incorporate them by name without
  having to define the details
- Allow users to define custom entries and override built-in ones
- Support third-party apt sources with idempotent GPG key and source list management
- Make all operations safe to re-run via `vm reinit`
- Provide CLI commands to list and inspect available catalog entries

---

## Non-Goals

- Package version pinning (use apt pinning or install command flags if needed)
- Replacing the existing snap package support (snap list stays as-is)
- Automatic updates of installed packages after initial install
- Building a general-purpose package manager abstraction

---

## Concepts

### Apt Sources

An apt source is a third-party package repository that must be configured before its packages can be
installed via `apt-get`. Each source consists of:

- A GPG signing key (downloaded from a URL and stored at a specific path)
- A source list entry (written to `/etc/apt/sources.list.d/`)

Apt sources are system-level and run as root during VM initialization. They are idempotent: if the
GPG key file already exists at the target path, the source is considered configured and the step is
skipped. This makes them safe to re-run on `vm reinit`.

Apt sources are a dependency of apt packages -- a source must be configured before its packages can
be installed. This dependency is declared on the apt package entry, not on the source itself.

### System Apt Packages

A flat list of package names installed via `apt-get install`. These are system-wide and run as root.
`apt-get install` is naturally idempotent (already-installed packages are skipped). Packages may
declare a dependency on one or more apt sources, ensuring the source is configured before the
install runs.

The current `vm.config.apt` list continues to work as before. The enhancement is that the built-in
catalog provides named apt package sets (e.g. `gh`, `docker-ce`) that bundle the correct apt source
dependency, so the user just references the name. When an apt package entry declares apt source
dependencies, those sources are automatically configured before the package install -- the user does
not need to separately reference the apt sources.

### System Install Commands

Shell commands that install system-wide tooling. These run once per VM during initialization, in the
admin user's login shell. They are for tools that do not have apt packages or where a script-based
installer is preferred (e.g. `az cli`, `tailscale`). Like apt packages, the result is available to
all users on the VM.

System install commands may declare PATH additions that are written to a system-wide profile.

### User Install Commands

Shell commands that install per-user tooling into the user's home directory (e.g. bun, nvm, claude).
These run for the VM admin user during VM init, and again for each agent during agent creation. Each
user gets their own independent installation.

User install commands may declare PATH additions that are written to `~/.agentworks-path.sh` and
sourced from shell profiles.

### Install Command Idempotency

Both system and user install commands are assumed to be idempotent. Agentworks will re-run them on
`vm reinit` (and on each agent creation) without checking whether the tool is already installed. It
is the responsibility of the command author to ensure that re-running the command is safe (e.g. by
using installers that detect existing installations, or by overwriting in place). The built-in
catalog entries are written to be idempotent.

### Built-in Catalog

A read-only catalog of apt sources, apt packages, system install commands, and user install commands
that ships with the agentworks package. The catalog grows over time as new releases add entries for
common tools.

Users reference catalog entries by name in their config. User-defined entries in the config file
override built-in entries with the same name (user wins on collision). Catalog entries are inert
until referenced -- adding a new entry to the catalog in a future release does not change behavior
for existing users.

---

## Requirements

### R1: Separated Config Sections

The user config must clearly separate the four concepts:

- `[apt_sources.*]` -- user-defined apt source entries
- `[apt_packages.*]` -- user-defined named apt package sets with optional apt source dependencies
- `[system_install_commands.*]` -- user-defined system-level shell commands (run once per VM)
- `[user_install_commands.*]` -- user-defined per-user shell commands

### R2: Separated VM Config Selection

VM config must use separate lists for each type of install-time operation:

- `vm.config.apt` -- direct list of apt package names (unchanged)
- `vm.config.apt_packages` -- selects named apt package sets from the catalog
- `vm.config.system_install_commands` -- selects system install commands from the catalog
- `vm.config.admin_user_install_commands` -- selects per-user install commands to run for the VM
  admin user during VM init
- `vm.config.snap` -- direct list of snap package names (unchanged)

### R3: Agent Config Selection

Agent config selects from per-user install commands only:

- `agent.config.user_install_commands` -- selects user install commands to run for each agent

### R4: Built-in Catalog

Agentworks must ship a built-in catalog file (TOML) containing common apt sources, apt packages,
system install commands, and per-user install commands. The catalog is read-only and bundled with
the package.

Resolution order: user config entries take precedence over built-in catalog entries with the same
name.

### R5: Apt Source Idempotency

Apt source configuration must be idempotent. If the GPG key file already exists at the target path,
the source setup is skipped entirely. This ensures `vm reinit` does not re-download keys or
duplicate source list entries.

### R6: CLI Discoverability

The CLI must provide commands to list and inspect available entries:

- List all available entries (built-in + user-defined) with type and source indicator
- Describe a specific entry showing its full definition

### R7: Architecture-Aware Apt Sources

Apt source definitions must support architecture placeholders (e.g. `{arch}`) that resolve to the
correct value (`amd64`, `arm64`) at install time based on the target VM.

### R8: Declarative Initialization

VM initialization must be fully driven by config. CLI arguments that override initialization
behavior (`--extra-packages`, `--git-credentials` on `vm create`; `--git-credentials` on
`vm reinit`) are removed. The config file is the single source of truth for what gets installed and
configured during initialization.

`vm create` retains only immutable provisioning parameters that describe the VM's shape: `--name`,
`--platform`, `--vm-host`, `--cpus`, `--memory`, `--disk`, `--azure-vm-size`, `--vm-user`. These are
set once at creation time and persisted in the VM database record. They cannot be changed after
creation.

`vm reinit` takes only the VM name. It re-runs initialization using the current config. Any changes
to the config (new packages, different install commands, etc.) are picked up automatically.

Future work: VM templates will allow different VM configurations to be defined as named profiles in
config. The selected template name would be another immutable provisioning parameter.

### R9: Documentation and Completions

- CLI README must be updated to document the new config structure and commands
- Shell completions must be updated for any new commands or subcommands
- Sample config must reflect the new structure

---

## User Stories

**As a developer setting up a new VM**, I want to reference common tools by name (e.g. `gh`, `bun`,
`claude`) without having to write their install commands myself, so that my config stays lean and
correct.

**As a developer using the GitHub CLI**, I want agentworks to handle the apt source setup (GPG key +
source list) automatically when I include `gh` in my apt packages, so that I do not need to encode
the multi-step process manually.

**As a developer running `vm reinit`**, I want all install operations to be safe to re-run without
duplicating apt sources or re-downloading keys that already exist.

**As a developer exploring available tools**, I want to list all built-in and custom install
commands so I can see what is available without reading catalog source files.
