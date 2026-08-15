# Installer resource plugins: locked

**Locked:** 2026-08-14

This effort is complete. The lock takes effect when the implementation PR lands on `main`; until
then, this file records closeout intent on the merge-intent branch. The revised design artifacts
landed separately before implementation so other saga efforts could build against the narrowed
resource-only contract.

## What shipped

- The `apt` system plugin owns five apt sources and five apt packages. The `install-command` system
  plugin owns six user install commands. Both plugins ship with Agentworks and are disabled by
  default.
- All 16 selectors, payloads, dependency edges, completion checks, and execution order remain
  unchanged when their owning plugin is enabled. Core continues to own the resource kinds,
  references, runners, and initializer execution.
- The existing resource framework supplies publication, disabled-row visibility, same-name operator
  precedence, provenance, and the standard pre-mutation disabled-resource error. This effort adds no
  alias, migrator, automatic enablement, compatibility warning, or special gate.
- Each plugin owns one request-scoped conceptual guide topic and packaged Markdown. File-backed
  adapter failures are isolated to a plugin-scoped guide issue; ordinary plugin imports perform no
  guide resource I/O.
- The sample config, CLI and plugin references, resource guide, command reference, and 0.14 upgrade
  guide teach the two opt-ins, all moved selectors, composite apt dependencies, and additive plugin
  enablement.

## Verification

- On current `main`, all 8,101 tests passed. Ruff check and formatting, strict mypy across 316
  source files, Prettier, markdownlint, cspell, locked-SDD validation, Rulesync drift, and diff
  checks also passed.
- A wheel was built and installed without editable mode in a fresh Python 3.12 environment outside
  the checkout. All three plugin YAML bundles and six guide Markdown files were readable through
  package resources.
- Isolated-home shipped-CLI acceptance proved exactly 16 disabled moved rows by default, the two
  standard use-gate errors before VM mutation, each plugin's independent opt-in, both opt-ins
  together, conceptual and dynamic guide topics, Bash/Zsh/PowerShell completion projections, and the
  four expected doctor roster combinations. The run left zero VMs and removed its temporary wheel,
  environment, home, config, database, fixtures, keys, and cache.
- Per-phase project reviews and fresh-eyes reviews converged without implementation findings. The
  aggregate project review found AC1 through AC7 clean and requested only this closeout record. Its
  fresh-eyes documentation finding was corrected and re-reviewed clean.
- The branch was rebased onto `main` at `0a984496`, then the full gates and shipped-wheel acceptance
  were repeated against the pushed rebased head.

## Live-test gap

No VM, remote SSH, cloud backend, installer execution, or secret resolution ran. The moved content
is declarative and every executor remains unchanged, so the approved acceptance boundary used
unchanged executor tests plus the real installed CLI. Doctor observed local host readiness,
including Tailscale status, but made no remote connection or mutation.

## Permanent homes

The operator contract lives in `cli/README.md`, `cli/command-reference.md`,
`cli/agentworks/sample-config.toml`, `docs/guides/resources.md`, the 0.14 upgrade guide, and the two
plugin-owned guide topics. The maintainer contract lives in `cli/agentworks/plugins/README.md`, the
plugin descriptors and manifests, the shared resource publication code, and their tests. No current
code or permanent documentation depends on this SDD directory.
