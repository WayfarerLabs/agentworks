# Resource show: locked

<!-- cspell:ignore grammer -->

**Locked:** 2026-08-18

This effort is complete in PR #597 with explicit operator merge intent. The lock takes effect when
the PR lands on `main`; until then, this file records the final reviewed implementation state.

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
- The shared JSON v1 encoder now escapes every `Cc`, `Cf`, `Cs`, `Zl`, and `Zp` character before
  UTF-8 encoding. It preserves ordinary Unicode and exact parsed string values, including lone
  surrogates and astral format characters, while retaining atomic one-document output for every
  machine command.
- The command opens the database only for supported read-only live-use projection. It resolves no
  secret values, prompts for nothing, performs no authenticated runup or provider mutation, and
  changes no operator state.
- `resource describe` remains removed without an alias or compatibility runway. `secret describe`
  remains the domain-specific secret synthesis surface.
- The installed guide-content tree is deliberately unchanged. Its owning guide effort will teach
  `resource show` after this command lands.

## Verification and review

The final reviewed implementation checkpoint is `ade93750f229f500bc27308c890cac27aa7f752e`, based
directly on `origin/main` at `0830cc2744b68422244f657769c0a6dfb3d81fb8`. The exact tree passed:

- 7,254 non-integration tests with one platform-specific skip;
- the 29 focused machine-output and resource-show tests, including a real manifest/registry/CLI
  regression for the originally reported hostile string;
- Ruff lint and format across 693 files;
- strict mypy across 692 source files;
- Prettier, markdownlint, cspell, locked-SDD validation, Rulesync drift, and diff guards;
- forge CI across Python 3.12, 3.13, and 3.14, plus CodeQL; and
- clean current-main ancestry, mergeability, and zero feature diff under
  `cli/agentworks/guide/guide-content/`.

Equal-tier project review found no Critical or Important issue. Its exhaustive check safely escaped
and round-tripped all 2,285 code points in the five unsafe Unicode categories, retained ordinary
Unicode as raw UTF-8, and confirmed unchanged machine schema and writer atomicity.

Fresh isolated-HOME acceptance drove the real `uv run agw` CLI with a valid secret manifest carrying
ordinary Unicode, U+D800, U+2028, U+2029, U+202E, and U+2066. `resource show --output json` exited
zero with empty stderr and one 1,519-byte envelope; parsing recovered the exact original string,
ordinary Unicode remained raw UTF-8, and no unsafe category remained active in the document. A
second machine command crossed the shared writer safely, human output remained safe, and cleanup
left no config, database, cache, process, provider, VM, network, or operator-state residue.

## Permanent homes and residual boundary

The executable contract lives in `cli/agentworks/resources/show.py`, the shared resource summary,
graph-query and rendering services, the structured doctor producers, the resource CLI, the shared
machine-output encoder, and completion specification. Operator guidance lives in
`cli/command-reference.md`, `cli/README.md`, `docs/guides/resources.md`, and the 0.14 upgrade map.
Nothing in this SDD directory is required to operate or maintain the command.

This effort intentionally leaves installed guide-content teaching to its separately owned follow-up.
It also leaves source-exact YAML, effective inheritance expansion, capability facets, transitive
graph traversal, global doctor checks, provider operations, secret values, mutation, and
compatibility aliases outside the command. The active saga owns its target-state and ledger
correction for the superseded no-replacement-card ruling.

-- agw-ns-cli-grammer (SDD lead)
