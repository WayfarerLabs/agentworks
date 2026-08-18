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
  and readiness, direct declared dependencies and dependents, current live usage where supported,
  every `doctor` check attributable to the resource, and a normalized declaration for declarable
  resources.
- List, graph, and doctor overlap is intentional. Shared summary, graph-edge, live-use, and
  structured diagnostic producers keep the focused and bulk surfaces consistent rather than scraping
  rendered output or duplicating interpretations.
- Relationship projection is direct and focus-bound. General direction, depth, traversal, and
  arbitrary neighborhoods remain with `graph show`; unrelated system checks remain with `doctor`.
- Declarable resources expose a deterministic normalized `agentworks/v1` envelope reconstructed from
  the loaded Pydantic model. Capability resources expose a null declaration; capability
  configuration and future facets remain with `resource explain`.
- Disabled rows remain visible with disabled enablement and null readiness. Supported live-use kinds
  distinguish an absent or empty database from kinds without an instance concept.
- Human output retains ordinary loader advisories and safely renders scalar facts and YAML. Machine
  output suppresses ambient warnings, assembles and validates before writing, and emits one closed
  JSON envelope.
- The command opens the database only for supported read-only live-use projection. It resolves no
  secret values, prompts for nothing, performs no authenticated runup or provider mutation, and
  changes no operator state.
- `resource describe` remains removed without an alias or compatibility runway. `secret describe`
  remains the domain-specific secret synthesis surface.

## Verification and review

The final reviewed implementation checkpoint is `bcd3781dc3616735b9dde588a0e5050e4f599490`, based
directly on `origin/main` at `217930fdee2edcf16caa546923a339bd1f37121f`. The exact code tree passed:

- 7,216 non-integration tests;
- focused summary, graph, doctor, show, CLI, completion, and projection suites;
- Ruff lint and format across 693 files;
- strict mypy across 692 source files;
- Prettier, markdownlint, cspell, locked-SDD validation, Rulesync drift, and diff guards; and
- every required GitHub CI and CodeQL job on Python 3.12, 3.13, and 3.14.

Equal-tier project review and an independent fresh-eyes review found no remaining Critical or
Important issue. The published integration-test pass drove the real `uv run agw` CLI in an isolated
HOME and verified exact list-row parity, direct graph parity, structured doctor parity, absent,
empty, seeded, and unsupported live-use states, human and JSON behavior, malformed and unknown
selectors, secret safety, terminal cleanliness, and residue-free cleanup. No product finding or
blocker remained. Remote provider testing was intentionally unnecessary because this is a local,
read-only inspection command and no authorized test inventory was available.

## Permanent homes and residual boundary

The executable contract lives in `cli/agentworks/resources/show.py`, the shared resource summary and
graph-query services, the structured doctor producers, the resource CLI, machine-output registry,
and completion specification. Operator guidance lives in `cli/command-reference.md`,
`cli/README.md`, the installed management guide, `docs/guides/resources.md`, and the 0.14 upgrade
map. Nothing in this SDD directory is required to operate or maintain the command.

This effort intentionally leaves source-exact YAML, effective inheritance expansion, capability
facets, transitive graph traversal, global doctor checks, provider operations, secret values,
mutation, and compatibility aliases outside the command.

-- agw-ns-cli-grammer (SDD lead)
