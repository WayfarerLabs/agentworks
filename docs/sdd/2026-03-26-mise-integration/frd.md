# Mise Integration - Functional Requirements Document

## Problem Statement

The current agentworks installer catalog supports apt packages, snap packages, and shell-based
install commands. This covers most system tooling, but leaves a gap for tools that are not available
via apt or snap and where a shell install command is brittle or lacks integrity verification.

Tools like `adr-tools` are not packaged for Debian/Ubuntu but are available through mise, a
polyglot tool version manager. More broadly, mise offers content-addressable integrity verification
via lockfiles (checksums per platform) that shell install commands cannot provide. Without this, a
compromised upstream repository could silently inject malicious code into a pinned version.

We need mise as a first-class package manager in agentworks, with an integrity story that delegates
to mise's own lockfile mechanism rather than reinventing it.

## Personas

- **Platform operator**: Configures VMs and workspaces for a team. Wants curated, verified tool
  sets. Needs confidence that what gets installed matches what was reviewed.
- **Agent developer**: Works inside agentworks workspaces. Needs specific CLI tools available in
  their environment without manual setup.

## Domain Model

- **Mise**: A polyglot tool version manager installed system-wide on VMs as agentworks
  infrastructure. Manages user-local tool binaries independently of the system package manager.
- **Mise package**: A tool installed via mise. Specified as `name@version` in the agentworks config.
- **Mise lockfile**: A `mise.lock` file (managed via mise's own `mise lock` workflow) that contains
  per-platform checksums and URLs for tool versions. Users generate and maintain this file outside
  of agentworks using standard mise tooling.

## Requirements

### R1: Mise installed by default

Mise must be installed automatically during VM initialization. A config flag (`install_mise`) allows
disabling this, but it defaults to true.

- Mise is installed system-wide via apt using mise's official deb repository. Its apt source setup
  and installation are hardcoded in the initializer, not represented as catalog entries.
- This makes the `mise` binary available at `/usr/bin/mise` for all users immediately.
- Shell activation is configured system-wide via `/etc/profile.d/mise.sh`.
- Per-user shims PATH is added to `~/.agentworks-path.sh` for non-interactive contexts.

### R2: Optional mise package lists

Users can optionally declare mise packages in their agentworks config for both the admin user and
agents.

- `vm.config` gets a `mise_packages` field: an optional list of `name@version` strings to install
  for the admin user.
- `agent.config` gets a `mise_packages` field: an optional list of `name@version` strings to
  install for each agent user.
- These are written to `~/.config/mise/config.toml` as tool declarations.
- If the user's dotfiles already provide a mise config, `mise_packages` can be omitted entirely.
  Dotfiles are synced before mise packages are installed.
- Agents default to no mise packages. They only get tools if explicitly configured.

### R3: Bring-your-own lockfile

Users can provide a `mise.lock` file for integrity verification.

- `vm.config` / `agent.config` get a `mise_lockfile` field: a source reference that can be a local
  file path or a git repository reference (using `git::` prefix). See source-ref-lld.md for the
  full syntax.
- Examples:
  - `mise_lockfile = "~/.config/agentworks/mise.lock"` (local file)
  - `mise_lockfile = "git::https://github.com/user/infra.git//mise/mise.lock"` (file in git repo)
  - `mise_lockfile = "git::https://github.com/user/locks.git?ref=v1.0"` (pinned git ref)
- Git sources are fetched after git credentials are configured, allowing private repos.
- When a lockfile is provided, it is copied to `~/.config/mise/mise.lock` and mise runs with
  `locked = true`.
- Users generate and maintain lockfiles using mise's own `mise lock` command, outside of
  agentworks. This keeps mise's lockfile format as the single source of truth for integrity data.
- Lockfiles can also come from dotfiles. Dotfiles are synced after agentworks writes its mise
  config but before `mise install` runs, so dotfiles can provide or override the lockfile.

### R4: Unlocked package handling

When a lockfile is present but some declared packages are not covered by it:

- By default (`mise_allow_unlocked = false`), the install fails for those packages. The user is
  told which packages are missing from the lockfile.
- When `mise_allow_unlocked = true`, agentworks warns about the unlocked packages and re-runs
  `mise install` without `--locked` to install them anyway.

This is configured separately for admin and agents.

### R5: Release age filtering

Mise supports an `install_before` setting that filters out tool versions newer than a specified age.
This provides defense-in-depth against supply chain attacks on newly published versions.

- `vm.config` gets a `mise_install_before` field (default `"7d"`).
- `agent.config` gets a `mise_install_before` field (default `"7d"`).
- This is written to the user's `~/.config/mise/config.toml` as a `[settings]` value.
- Only applies to fuzzy version requests (e.g., `latest`, `node@20`). Explicitly pinned versions
  bypass the filter.

### R6: Distinction between VM and admin user config

The mise settings are per-user config that happens to be applied during VM initialization for the
admin user. This distinction should be clear:

- `install_mise` is VM-level (controls whether mise is installed system-wide).
- `mise_packages`, `mise_lockfile`, `mise_allow_unlocked`, and `mise_install_before` are admin
  user settings that live under `[vm.config]` alongside other admin-user init settings.
- Agent settings are separate under `[agent.config]` and default to nothing.

## Future Considerations

- A convenience CLI command to help users generate lockfiles for their configured packages.
- Per-workspace mise config (different tools per workspace, not just per user).
