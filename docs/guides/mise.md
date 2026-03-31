# Using mise with agentworks

[mise](https://mise.jdx.dev/) is a polyglot tool version manager that agentworks installs by
default on all VMs. It provides a unified way to install CLI tools (jq, adr-tools, node, python,
etc.) with optional integrity verification via lockfiles.

## How it works

Agentworks handles three things:

1. **Installing mise** via apt (system-wide, available to all users)
2. **Shell activation** via per-user `.agentworks-rc.sh` (works for bash and zsh)
3. **Per-user tool setup** based on your agentworks config

Everything else (lockfile management, version resolution, backend selection) is delegated to mise
itself. Agentworks does not reinvent mise's integrity mechanisms.

## Quick start

Add packages to your agentworks config:

```toml
[admin.config]
mise_packages = ["jq@1.8.1", "adr-tools@3.0.0"]
```

Run `agentworks vm create` or `agentworks vm reinit` and the tools will be available.

## Config reference

These settings are available in `[admin.config]` (for the admin user) and `[agent_templates.*]`
(for agents). The `install_mise` setting is VM-level and lives in `[vm_templates.*]`.

| Setting | Default | Description |
| --- | --- | --- |
| `install_mise` | `true` | Install mise via apt (VM-level, `[vm_templates.*]` only) |
| `mise_activate` | `true` | Add `mise activate` to the user's shell profile |
| `mise_packages` | `[]` | List of `name@version` tool declarations |
| `mise_lockfile` | (none) | [Source reference](source-refs.md) to a `mise.lock` file |
| `mise_allow_unlocked` | `false` | Install packages not covered by the lockfile (with warning) |
| `mise_install_before` | `"7d"` | Reject versions published more recently than this |
| `mise_prune_on_reinit` | `true` | Remove stale tool versions on reinit |

### Agents

Agents default to nothing. They only get mise tools if explicitly configured in an agent template:

```toml
[agent_templates.default]
mise_packages = ["jq@1.8.1"]
```

## Lockfiles

Mise lockfiles (`mise.lock`) contain per-platform checksums and URLs for tool versions. When a
lockfile is present, `mise install --locked` verifies downloads against it. This is mise's own
integrity mechanism, not something agentworks invented.

### Creating a lockfile

On any machine with mise installed:

```sh
# Write a mise.toml with your tools
cat > mise.toml << 'EOF'
[tools]
jq = "1.8.1"
adr-tools = "3.0.0"
EOF

# Generate the lockfile
mise settings lockfile=true
mise lock

# The lockfile is now at mise.lock
cat mise.lock
```

Not all tools support checksums. Tools installed from GitHub source archives (like adr-tools) will
have URLs but no checksums in the lockfile. Tools with binary releases (like jq) will have both.

### Providing a lockfile to agentworks

Point `mise_lockfile` at your lockfile using a local path or a
[source reference](source-refs.md):

```toml
[admin.config]
mise_packages = ["jq@1.8.1", "adr-tools@3.0.0"]
mise_lockfile = "~/.config/agentworks/mise.lock"
```

Or from a git repository:

```toml
[admin.config]
mise_packages = ["jq@1.8.1", "adr-tools@3.0.0"]
mise_lockfile = "git::https://github.com/myorg/tool-locks.git//mise.lock?ref=v1.0"
```

Git source references are fetched after git credentials are configured, so private repos work.

### Locked vs unlocked behavior

When a lockfile is present:

- Agentworks runs `mise install --locked`.
- If all packages are in the lockfile, everything is verified and installed.
- If some packages are missing from the lockfile:
  - **`mise_allow_unlocked = false`** (default): the install fails for those packages. You see
    which ones are missing.
  - **`mise_allow_unlocked = true`**: a warning is logged for the missing packages, and they are
    installed without verification.

When no lockfile is present, `mise install` runs without `--locked` (no verification).

## Sequencing

Understanding the init ordering matters if your dotfiles also configure mise:

1. **Mise config write** -- agentworks writes `~/.config/mise/config.toml` from `mise_packages`
2. **Dotfiles sync** -- your dotfiles can override the mise config (or provide their own lockfile)
3. **Mise lockfile fetch** -- if `mise_lockfile` is set, it is copied last, overriding anything
   dotfiles put there
4. **Mise install** -- runs with the final config + lockfile state

This means:
- If your dotfiles provide a complete `~/.config/mise/config.toml`, you can omit `mise_packages`.
- If your dotfiles provide a `~/.config/mise/mise.lock` and you have no `mise_lockfile` set, the
  dotfiles lockfile is used as-is.
- If you set `mise_lockfile` explicitly, it always wins over a dotfiles-provided lockfile. This is
  intentional: an explicit lockfile in the agentworks config is a deliberate policy decision that
  should not be silently overridden by dotfiles.

## Release age filtering

The `mise_install_before` setting filters out tool versions newer than the specified age. This
provides defense-in-depth against supply chain attacks on newly published versions.

```toml
[admin.config]
mise_install_before = "7d"    # reject versions less than 7 days old
```

Supports relative durations (`7d`, `90d`, `6m`, `1y`) and absolute dates (`2024-06-01`). This only
affects fuzzy version requests (e.g., `latest`, `node@20`). Explicitly pinned versions (e.g.,
`jq@1.8.1`) bypass the filter.

## Disabling mise

If you do not want mise on your VMs:

```toml
[vm_templates.default]
install_mise = false
```

This skips the apt install, shell activation, and all per-user mise setup.
