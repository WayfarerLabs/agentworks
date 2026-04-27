# Source References

Source references are a unified syntax for pointing to files from local paths or git repositories.
They are used in agentworks config fields that accept file sources (e.g., `mise_lockfile`).

## Syntax

### Local file

A plain path or a `file::` prefixed path. Tilde expansion is supported.

```text
~/.config/agentworks/mise.lock
file::~/.config/agentworks/mise.lock
```

### Git repository

A `git::` prefix followed by a git URL. The file is cloned from the repository during VM
initialization.

```text
git::https://github.com/user/repo.git
```

### Git with subpath

Use `//` after the URL to specify a file within the repository. Without a subpath, the default
filename depends on the config field (e.g., `mise.lock` for `mise_lockfile`).

```text
git::https://github.com/user/infra.git//mise/mise.lock
git::https://github.com/user/infra.git//tools/agent-mise.lock
```

### Git with ref

Use `?ref=` to pin a specific branch, tag, or commit.

```text
git::https://github.com/user/infra.git?ref=main
git::https://github.com/user/infra.git//mise/mise.lock?ref=v1.0
git::https://github.com/user/infra.git?ref=abc123def
```

## Full syntax

```text
[git::]<url>[//<subpath>][?ref=<branch|tag|commit>]
```

- **Scheme**: `file::` (default if omitted) or `git::`.
- **URL**: For git sources, must start with `https://` or `git@`.
- **Subpath**: Path to a file within the repository. Must not contain `..`.
- **Ref**: Branch name, tag, or commit hash. Must be alphanumeric with hyphens, dots, underscores,
  and slashes.

## Private repositories

Git source references are fetched on the VM after git credentials are configured. This means
private repos work as long as the appropriate `[git_credentials.*]` are set up in your agentworks
config.

## Examples

```toml
# Local file
mise_lockfile = "~/.config/agentworks/mise.lock"

# File at repo root (defaults to mise.lock)
mise_lockfile = "git::https://github.com/myorg/tool-locks.git"

# File in a subdirectory
mise_lockfile = "git::https://github.com/myorg/infra.git//mise/prod.lock"

# Pinned to a tag
mise_lockfile = "git::https://github.com/myorg/tool-locks.git?ref=v2.1"

# Private repo (requires git credentials)
mise_lockfile = "git::https://github.com/myorg/private-locks.git//mise.lock?ref=main"
```
