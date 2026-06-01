# Source Reference - Low-Level Design

## Overview

A reusable utility for resolving file content from multiple source types. Inspired by Terraform's
module source syntax, a single string field can reference a local file, a file in a git repository,
or (in the future) other source types.

This is initially used for `mise_lockfile` but is designed to be adopted by other features (e.g.,
dotfiles, nerf manifests) that need to fetch content from local or remote sources.

## Source Reference Syntax

A source reference is a string with an optional scheme prefix:

```text
# Local file (no prefix, or explicit file:: prefix)
~/.config/agentworks/mise.lock
file::~/.config/agentworks/mise.lock

# Git repository
git::https://github.com/user/infra.git
git::https://github.com/user/infra.git//mise/mise.lock
git::https://github.com/user/infra.git?ref=main
git::https://github.com/user/infra.git//path/to/file?ref=v1.0
```

### Components

- **Scheme**: `file::` (default if omitted) or `git::`.
- **URL/path**: The repository URL or local file path.
- **Subpath** (git only): `//path/to/file` after the URL. Specifies a file within the repo. If
  omitted, defaults to `mise.lock` (the caller can specify a different default).
- **Query parameters** (git only): `?ref=<branch|tag|commit>` to pin a specific ref. Defaults to the
  repo's default branch.

### Parsing rules

1. If the string starts with `git::`, parse as a git source.
2. If the string starts with `file::`, strip the prefix and treat as a local path.
3. Otherwise, treat as a local path.
4. For git sources, split on `//` to separate URL from subpath. Split on `?` to extract query params
   from the URL portion.

## Data Model

```python
@dataclass(frozen=True)
class SourceRef:
    """Parsed source reference."""
    kind: str          # "file" or "git"
    path: str          # local path (for file) or repo URL (for git)
    subpath: str       # file within repo (git only), empty for file sources
    ref: str           # git ref (branch/tag/commit), empty string = default branch
```

## Fetch Operation

```python
def fetch_file(
    source: SourceRef,
    target: ExecTarget,
    dest: str,
    *,
    default_filename: str = "",
    logger: SSHLogger | None = None,
) -> None:
```

- **File sources**: Copy the local file to `dest` on the target via `ExecTarget.copy_to()`. If
  `dest` is a directory, uses the source filename (or `default_filename`).
- **Git sources**: Clone the repo (or `git pull` on reinit) to a temporary directory on the target,
  then copy the subpath file to `dest`. The clone uses `--depth 1` for efficiency and
  `--branch <ref>` if a ref is specified. The temporary clone is cleaned up after the file is
  copied.

## Module Location

`cli/agentworks/sources.py` - a standalone module with no dependencies beyond `ssh.py`.

## Validation

`parse_source_ref()` validates:

- Git URLs must start with `https://` or `git@` (after `git::` prefix).
- Subpath must not contain `..` (directory traversal).
- Ref must be alphanumeric with hyphens, dots, underscores, and slashes (branch names).

Config-level validation (in `config.py`) calls `parse_source_ref()` during load to catch malformed
source references early.
