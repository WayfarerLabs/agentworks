# Config Migration Guide

## Dotfiles: `repo` replaced by `source`

The separate `repo` and `source` fields have been replaced by a single `source` field that accepts
both local paths and git source references.

**Before:**

```toml
[dotfiles]
repo = "https://github.com/user/dotfiles"
```

**After:**

```toml
[dotfiles]
source = "git::https://github.com/user/dotfiles"
```

Local paths work unchanged:

```toml
[dotfiles]
source = "~/.dotfiles"
```

See [source references](source-refs.md) for the full syntax including `?ref=` for pinning.

## Admin user settings moved from `[vm.config]` to `[admin.config]`

Settings that configure the admin user (shell, install commands, mise) are now in their own
`[admin.config]` section, separate from VM-level settings.

**Before:**

```toml
[vm.config]
admin_shell = "zsh"
admin_install_commands = ["bun", "claude"]
mise_activate = true
mise_packages = ["jq@1.8.1", "adr-tools@3.0.0"]
mise_lockfile = "~/.config/agentworks/mise.lock"
mise_allow_unlocked = false
mise_install_before = "7d"
```

**After:**

```toml
[admin.config]
shell = "zsh"
user_install_commands = ["bun", "claude"]
mise_activate = true
mise_packages = ["jq@1.8.1", "adr-tools@3.0.0"]
mise_lockfile = "~/.config/agentworks/mise.lock"
mise_allow_unlocked = false
mise_install_before = "7d"
```

Note the renames:
- `admin_shell` -> `shell`
- `admin_install_commands` -> `user_install_commands`

The `install_mise` setting stays in `[vm.config]` since it controls system-wide apt installation.

## Mise catalog entries removed

Mise packages are no longer defined as catalog entries (`[mise_packages.*]` sections). Instead, use
`mise_packages` in `[admin.config]` or `[agent.config]` with `name@version` strings, and provide
lockfiles via `mise_lockfile` for integrity verification.

**Before:**

```toml
[vm.config]
mise_packages = ["jq", "adr-tools"]

[mise_packages.jq]
description = "Command-line JSON processor"
version = "1.8.1"
backend = "aqua"
checksums.linux-x64 = "sha256:..."
```

**After:**

```toml
[admin.config]
mise_packages = ["jq@1.8.1", "adr-tools@3.0.0"]
mise_lockfile = "~/.config/agentworks/mise.lock"
```

See [Using mise](mise.md) for how to generate and manage lockfiles.
