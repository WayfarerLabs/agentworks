# Resource show: locked

<!-- cspell:ignore grammer -->

**Locked:** 2026-08-17

This effort is complete in draft PR #597. The lock takes effect when the PR lands on `main`; until
then, this file records the final verified implementation state. The PR remains draft pending the
operator's merge-intent signal.

## What shipped

- `agw resource show KIND/NAME` projects one concrete loaded Resource Registry row in human or
  closed `resource.show` JSON v1 output.
- Every row carries its kind, name, category, description, safe origin, enablement, and readiness.
  Disabled rows carry null readiness because their internal ready placeholder is not an observed
  verdict. Enabled rows distinguish ready, blocked, and deliberately unavailable checks.
- Declarable rows carry a deterministic normalized `agentworks/v1` manifest envelope reconstructed
  from their loaded Pydantic model. It includes loaded defaults and JSON-native nested values while
  excluding framework provenance. It is neither source-exact YAML nor an effective inheritance
  merge.
- Capability rows carry a null declaration. The command does not copy capability configuration or
  future facets from `resource explain`.
- Human scalar facts remove terminal controls, Unicode format controls, surrogates, and Unicode line
  separators. Declaration YAML uses printable ASCII escapes and parses back to the exact JSON value.
- The command opens no database, traverses no relationship edges, resolves no secret values, calls
  no provider operation, mutates nothing, and emits no loader warnings. Relationships remain with
  `graph show`, accepted fields with `resource explain`, diagnosis with `doctor`, and source editing
  with `resource edit`.
- `resource describe` remains removed without an alias. `secret describe` remains the
  domain-specific synthesis surface.

## Verification and review

The final reviewed implementation checkpoint is `6f0bd59e`, based on `origin/main` at `217930fd`.
The exact tree passed:

- 7,205 non-integration tests in 50.60 seconds;
- Ruff check and format across 693 files;
- strict mypy across 692 source files;
- Prettier, markdownlint across 337 files, and cspell across 314 files;
- Rulesync drift, locked-SDD validation, and diff checks; and
- 121 focused integrated resource-show, machine-output, and completion tests.

An equal-tier project review and an independent fresh-eyes review found two material issues in the
first implementation: Unicode line/format safety and an undefined `is_available` machine-contract
field. The final fix removes unsafe Unicode categories from fact lines, emits declaration Unicode as
round-tripping YAML escapes, and defines all three enabled readiness states. Both reviewers reran
the 39-test fix focus at the exact final checkpoint and reported no remaining Critical or Important
finding.

The draft artifact checkpoint also passed public saga-lead review. Local real-CLI acceptance covered
declarable not-ready JSON, capability human output, disabled declarable JSON, malformed selectors,
and unknown selectors. No cloud provider, remote backend, state database, or secret resolution was
needed or touched.

## Permanent homes and residual boundary

The executable contract lives in `cli/agentworks/resources/show.py`, the resource command,
`machine_output.py`, and the completion specification. Operator and machine-consumer guidance lives
in `cli/command-reference.md`, `cli/README.md`, `docs/guides/resources.md`, and the release-scoped
0.14 upgrade map. Nothing in this SDD directory is required to operate or maintain the command.

This effort intentionally leaves capability facets, effective inheritance projection, source-exact
manifest preservation, relationships, live usage, diagnostics, and compatibility aliases outside the
command. Review also reproduced a pre-existing Unicode line-separator weakness in the graph human
renderer; that is a separate terminal-safety follow-up rather than scope added to this focused
command.

-- agw-ns-cli-grammer (SDD lead)
