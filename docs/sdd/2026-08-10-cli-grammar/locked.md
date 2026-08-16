# CLI grammar correction: locked

<!-- cspell:ignore grammer -->

**Locked:** 2026-08-16

This effort is complete in draft PR #491. The lock takes effect when the PR lands on `main`; until
then, this file records the final verified state of the implementation branch. The PR intentionally
remains draft pending explicit operator merge intent.

## What shipped

- `agw graph show KIND/NAME` provides deterministic, focal-resource graph inspection in human and
  closed `graph.show` JSON forms. It supports dependencies, dependents, or both; positive finite
  depth or `all`; complete authoritative declared edges among reached resources; and frontier-only
  terminal live-instance facts from one coherent, lazy, read-only database snapshot.
- The generic `agw resource describe` card and `resource.describe` machine identity are removed
  without an alias or compatibility runway. Relationship inspection belongs to graph. The
  secret-specific `agw secret describe` surface remains unchanged.
- `agw resource describe-kind` is now the config-free `agw resource explain`, and the fixed schema
  writer is `agw resource schema --install`. The path-taking `resource sample --write PATH` surface
  remains distinct and unchanged.
- Resource-name completion and `resource list --names-only` use the finalized registry without a
  database dependency. Ordinary resource lists retain their database-backed behavior. Bash, Zsh, and
  PowerShell completion projections agree with the shipped command tree.

## Verification and review

The final code-and-test checkpoint is `51149bf195e31ef8cc5996086ab3c211eb53f090`, based on
`origin/main` at `ca924ff1b7ef3a3f78e3d98f59c309ae0b6a8966`. The implementation passed the complete
7,417-test non-integration suite locally, Ruff check and format, strict mypy across 695 files, file
lint, Rulesync drift, locked-SDD validation, and diff checks. CI is green on Python 3.12, 3.13, and
3.14 and on every repository gate.

Project-values and fresh-eyes code reviews converged cleanly. The final saga-lead, Muntz, and
integration-tester lanes are clean at the exact checkpoint. The last directed fix changed only two
tests: it structurally proves both surviving unknown-kind paths expose every registered kind in
sorted order without asserting authored sentence wording. The affected 50-test suite and exact
static checks passed afterward.

## Live acceptance

The tester exercised the shipped CLI in a disposable home and SQLite environment. Acceptance covered
human and JSON graph output; default, directional, finite, mixed, and unbounded traversal; a two-hop
platform-to-site-to-live-VM case; absent and invalid databases; config-free explain; schema
installation and sample writing; removed spellings; unchanged secret description; and registry-only
names behavior. The tester's environment passed 7,419 tests and left zero residue.

No cloud provider, remote backend, secret resolution, or operator state was needed or touched. Graph
and explain inspection are read-only, and graph live facts are local SQLite projections; schema
installation and sample writing targeted only disposable local paths. The approved live charter
therefore required no provider mutation. The later exact-head changes contained only reconciled SDD
records and test-only hint coverage, so the tester reran the affected tests and carried the clean
live verdict forward.

## Permanent homes and future boundary

The operator contract lives in `cli/command-reference.md`, `docs/guides/resources.md`,
`docs/guides/upgrading-to-0.14.md`, `cli/README.md`, and `cli/agentworks/sample-config.toml`.
Production code, focused tests, and the three completion projections own the executable contract.
Nothing in this SDD directory is required to operate or maintain the shipped surface.

Multi-faceted capabilities remain future work. This implementation leaves room for the planned
harness-integration capability but does not add a facet model, infer facets from graph usage, or
expand live nodes. No material review finding or consciously retained implementation risk remains.

-- agw-ns-cli-grammer (SDD lead)
