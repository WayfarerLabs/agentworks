# Config Migration Guide

## Dotfiles moved from `[dotfiles]` to `[admin.config]` and `[agent.config]`

The standalone `[dotfiles]` section has been removed. Dotfiles are now per-user settings in
`[admin.config]` (and optionally `[agent.config]`). The `repo` and `source` fields have been
replaced by a single `dotfiles_source` field that accepts local paths and git source references.

**Before:**

```toml
[dotfiles]
enabled = true
repo = "https://github.com/user/dotfiles"
destination = "~/.dotfiles"
install_cmd = "./install.sh"
```

**After:**

```toml
[admin.config]
dotfiles_source = "git::https://github.com/user/dotfiles"
dotfiles_destination = "~/.dotfiles"
dotfiles_install_cmd = "./install.sh"
```

Local paths work the same way:

```toml
[admin.config]
dotfiles_source = "~/.dotfiles"
```

The `enabled` field is gone. If `dotfiles_source` is not set, dotfiles are not synced.

Agents can now have their own dotfiles:

```toml
[agent.config]
dotfiles_source = "git::https://github.com/user/agent-dotfiles"
```

See [source references](source-refs.md) for the full syntax including `?ref=` for pinning.

## Admin user settings moved from `[vm.config]` to `[admin.config]`

Settings that configure the admin user (shell, install commands, mise) are now in their own
`[admin.config]` section, separate from VM-level settings.

**Before:**

```toml
[vm.config]
admin_username = "agentworks"
admin_shell = "zsh"
admin_install_commands = ["bun", "claude"]
mise_activate = true
mise_packages = ["terraform@1.14.5", "adr-tools@3.0.0"]
mise_lockfile = "~/.config/agentworks/mise.lock"
mise_allow_unlocked = false
mise_install_before = "7d"
```

**After:**

```toml
[admin.config]
username = "agentworks"
shell = "zsh"
user_install_commands = ["bun", "claude"]
mise_activate = true
mise_packages = ["terraform@1.14.5", "adr-tools@3.0.0"]
mise_lockfile = "~/.config/agentworks/mise.lock"
mise_allow_unlocked = false
mise_install_before = "7d"
```

Note the renames:
- `admin_shell` -> `shell`
- `admin_install_commands` -> `user_install_commands`
- `admin_username` moved here from `[vm.config]` as `username`

The `install_mise` setting stays in `[vm_templates.*]` since it controls system-wide apt
installation.

## `[vm.config]` replaced by `[vm_templates.default]`

VM configuration now uses the template system, consistent with workspace and task templates.

**Before:**

```toml
[vm.config]
cpus = 4
memory = 8
apt = ["zsh", "build-essential"]
```

**After:**

```toml
[vm_templates.default]
cpus = 4
memory = 8
apt = ["zsh", "build-essential"]
```

The field names are unchanged. You can now define additional templates and select them at create
time:

```toml
[vm_templates.heavy]
inherits = ["default"]
cpus = 16
memory = 64
```

```
agentworks vm create --template heavy
```

## `git_credentials` moved from `[defaults]` to `[admin.config]` and `[agent.config]`

Git credentials are now per-user settings, allowing different credentials for admin and agent users.

**Before:**

```toml
[defaults]
git_credentials = ["github"]
```

**After:**

```toml
[admin.config]
git_credentials = ["github"]

[agent.config]
git_credentials = []    # agents get no credentials by default
```

## `[agent.config]` replaced by `[agent_templates.default]`

Agent configuration now uses the template system, consistent with VM and workspace templates.

**Before:**

```toml
[agent.config]
shell = "bash"
user_install_commands = ["claude"]
```

**After:**

```toml
[agent_templates.default]
shell = "bash"
user_install_commands = ["claude"]
```

Additional templates with inheritance:

```toml
[agent_templates.restricted]
inherits = ["default"]
git_credentials = []
mise_packages = []
```

```
agentworks agent create myagent --vm myvm --template restricted
```

## Mise catalog entries removed

Mise packages are no longer defined as catalog entries (`[mise_packages.*]` sections). Instead, use
`mise_packages` in `[admin.config]` or `[agent_templates.*]` with `name@version` strings, and provide
lockfiles via `mise_lockfile` for integrity verification.

**Before:**

```toml
[vm.config]
mise_packages = ["terraform", "adr-tools"]

[mise_packages.terraform]
description = "Infrastructure as code"
version = "1.14.5"
backend = "aqua"
checksums.linux-x64 = "sha256:..."
```

**After:**

```toml
[admin.config]
mise_packages = ["terraform@1.14.5", "adr-tools@3.0.0"]
mise_lockfile = "~/.config/agentworks/mise.lock"
```

See [Using mise](mise.md) for how to generate and manage lockfiles.

## Agents are now VM-scoped

Agents are no longer scoped to a single workspace. They are now VM-scoped Linux users (`agt--<name>`)
that access workspaces through a grant system. See [ADR-0010](../adrs/0010-vm-scoped-agents-with-workspace-grants.md)
for the full rationale.

**CLI changes:**

- `agent create` takes `--vm` instead of `--workspace`
- `agent create` accepts `--grant-all-workspaces` for agents that need access to every workspace
- `agent workspace-grants grant/deny/list` commands manage workspace access
- `agent shell` accepts optional `--workspace` to cd into a workspace
- `agent delete` and `agent reinit` no longer take `--workspace`

**Workspace groups:**

Workspace groups changed from `ws-<name>` (single hyphen) to `ws--<name>` (double hyphen),
consistent with the `agt--<name>` agent username convention. Use `agentworks workspace repair` to
fix existing workspaces.

## `code_workspaces` renamed to `vscode_workspaces`

The `[paths]` setting `code_workspaces` has been renamed to `vscode_workspaces` for clarity.

**Before:**

```toml
[paths]
code_workspaces = "~/aw-vscode-workspaces"
```

**After:**

```toml
[paths]
vscode_workspaces = "~/aw-vscode-workspaces"
```
