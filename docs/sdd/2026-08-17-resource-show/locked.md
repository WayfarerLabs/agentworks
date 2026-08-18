# Resource show: locked

<!-- cspell:ignore grammer -->

**Locked:** 2026-08-18

This effort is complete in draft PR #597. The lock takes effect when the PR lands on `main`; until
then, this file records the final reviewed implementation state. The PR remains draft pending the
operator's merge-intent signal.

## What shipped

- `agw resource show KIND/NAME` is the complete focused view of one loaded registry resource in
  human or closed `resource.show` JSON v1 output.
- The result includes the exact facts from the selected `resource list` row, structural enablement
  and readiness, every direct declared dependency and dependent, current live usage where supported,
  every `doctor` check attributable to the resource, and a normalized declaration for declarable
  resources.
- List, graph, and doctor overlap is intentional. Shared summary, graph-edge, live-use, readiness,
  and structured diagnostic producers keep focused and bulk surfaces consistent rather than scraping
  rendered output or duplicating interpretations.
- Direct relationship projection is independent of traversal policy. A future relationship may be
  non-traversable and still remains a direct fact about the selected row; general direction, depth,
  traversal, and arbitrary neighborhoods remain with `graph show`.
- Declarable resources expose a deterministic normalized `agentworks/v1` envelope reconstructed from
  the loaded Pydantic model. One exported framework-field set is shared by manifest decode and
  projection. Core-owned model classes accept plugin-authored manifest rows, and a closed finite
  JSON carrier guard prevents unexpected values from being coerced into machine output.
- Capability resources expose a null declaration; capability configuration and future facets remain
  with `resource explain`. `category` remains explicit in the focused record.
- Disabled rows remain visible with disabled enablement and null readiness. The compact
  `disabled`/`not_ready_reason` fields preserve exact list parity while structural
  `enablement`/`readiness` fields expose the richer focused state axes.
- One shared fact-line sanitizer protects both resource and graph human output from terminal
  controls, Unicode format/surrogate categories, and line/paragraph separators while preserving
  ordinary Unicode. Declaration YAML relies on ASCII-only safe encoding and round-trips without a
  redundant post-encoding sanitizer.
- The command opens the database only for supported read-only live-use projection. It resolves no
  secret values, prompts for nothing, performs no authenticated runup or provider mutation, and
  changes no operator state.
- `resource describe` remains removed without an alias or compatibility runway. `secret describe`
  remains the domain-specific secret synthesis surface.

## Verification and review

The final reviewed implementation checkpoint is `23773dfd0426c130d33418b6ecf10ca4fe529262`, based
directly on `origin/main` at `0e529ce0c7ba327d45fb790617385073bb7b7833`. The exact tree passed:

- 7,220 non-integration tests;
- focused summary, graph, doctor, show, CLI, completion, projection, and hostile-input suites;
- Ruff lint and format across 693 files;
- strict mypy across 692 source files;
- Prettier, markdownlint, cspell, locked-SDD validation, Rulesync drift, and diff guards; and
- patch-identical range-diff and conflict-free ancestry checks after rebasing onto current `main`.

Equal-tier project review and an independent fresh-eyes review found no remaining Critical or
Important issue. Review caught one wording contradiction that incorrectly described plugins as
contributing declarable model classes; code and HLA now accurately distinguish core-owned models
from plugin-authored manifest rows.

Two published isolated-HOME integration passes drove the real `uv run agw` CLI. The final rebased
head verified exact list/show parity, direct graph facts, atomic JSON, normalized declaration,
secret safety, hostile Unicode handling, selector failures, and residue-free cleanup. No product
finding or blocker remained. Remote provider testing was intentionally unnecessary because this is a
local, read-only inspection command and no authorized test inventory was available.

## Permanent homes and residual boundary

The executable contract lives in `cli/agentworks/resources/show.py`, the shared resource summary,
graph-query and rendering services, the structured doctor producers, the resource CLI,
machine-output registry, and completion specification. Operator guidance lives in
`cli/command-reference.md`, `cli/README.md`, the installed management guide,
`docs/guides/resources.md`, and the 0.14 upgrade map. Nothing in this SDD directory is required to
operate or maintain the command.

This effort intentionally leaves source-exact YAML, effective inheritance expansion, capability
facets, transitive graph traversal, global doctor checks, provider operations, secret values,
mutation, and compatibility aliases outside the command. The active saga owns its target-state and
ledger correction for the superseded no-replacement-card ruling.

-- agw-ns-cli-grammer (SDD lead)
