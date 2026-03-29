# 9. Source references for external content

Date: 2026-03-29

## Status

Accepted

## Context

Multiple config fields need to reference external content: dotfiles (a directory from a local path
or git repo), mise lockfiles (a single file from a local path or git repo), and potentially future
features. Each field could have its own pair of options (e.g., `dotfiles_source` + `dotfiles_repo`)
or we could use a unified syntax.

Terraform's module source syntax provides a well-known pattern for this: a single string field that
accepts local paths and `git::` prefixed URLs with optional subpaths and ref pinning.

## Decision

We use a Terraform-inspired source reference syntax as a single string field:

- Local path: `~/.config/agentworks/mise.lock` or `file::~/.config/...`
- Git repo: `git::https://github.com/user/repo.git`
- Git with subpath: `git::https://github.com/user/repo.git//path/to/file`
- Git with ref: `git::https://github.com/user/repo.git?ref=v1.0`

The parsing and fetching logic lives in a reusable `sources.py` module with `fetch_file` (single
file) and `fetch_dir` (directory/repo clone) operations.

## Consequences

- One config field instead of two for every feature that needs external content.
- Users learn one syntax and apply it to dotfiles, lockfiles, and future features.
- Git refs (`?ref=tag`) allow pinning to specific versions, which is important for lockfiles.
- Private repos work because git source references are fetched after git credentials are configured
  on the VM.
- The `sources.py` module is reusable infrastructure. Adding a new feature that needs to fetch
  external content requires no new fetching code.
- Tradeoff: the `git::` prefix is less intuitive than a plain URL. This is the cost of
  disambiguating local paths from git URLs in a single field.
