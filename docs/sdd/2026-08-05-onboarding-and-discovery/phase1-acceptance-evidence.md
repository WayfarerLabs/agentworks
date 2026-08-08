# Phase 1 Acceptance Evidence

- Date: 2026-08-06, updated 2026-08-07 after declarative-schema adoption
- Branch: `feat/onboarding-discovery-guide`
- Environment: isolated temporary home, config, state, and fake executable directories
- Budget: 20 commands and 10 minutes for the initial pass; 8 commands and 5 minutes for the focused
  rerun

## Golden path

The initial clean-environment run reached the first actionable `concept-onboarding` plan in 113 ms.
The plan contained two inert actions and executed neither. The full disclosure preceded inventory
and actions. There were zero prompts or operator interactions.

Fifteen initial CLI invocations covered explicit human and agent modes, piped `--human`, retained
names under missing and broken configuration, live names under valid configuration, atomic
multi-topic success and failure, authored broken-config fallback, and no-topic indexes. Secret
resolution, VM connections, action execution, configuration mutation, and state mutation did not
occur.

This Phase 1 measurement intentionally stops at the first actionable guide plan. FRD acceptance
criterion 1 requires the published Claude Code and Codex bootstrap packages and a working session,
so its clean-machine timing and interaction evidence belongs to Phase 3 rather than this guide-core
slice.

## Acceptance findings and rerun

The initial run found that host-tool readiness inspected executable presence during guide registry
construction and that the no-topic index lacked its explicit onboarding entry. Both defects were
fixed and reviewed.

The focused rerun used seven CLI invocations. Each completed in 108 to 116 ms with empty stderr,
zero prompts, and no external access:

- Ordinary resource readiness changed when a fake `limactl` became available, preserving normal
  command behavior. The fake executable was never run.
- Guide output was byte-identical with `limactl` absent or present and reported host readiness as
  `unverifiable` because guide does not inspect the workstation.
- Human and agent no-topic output both placed the complete disclosure first, then `Start here` with
  `agw guide concept-onboarding --agent`, then the topic index.
- Human and agent documents were byte-identical after normalizing only their mode-specific security
  heading.

All temporary files and temporary SQLite state were removed. The tester made no repository edits.

## Declarative-schema release-gate adoption

After PR #414 merged, the branch rebased onto authoritative `main` and adopted its config-free
schema services directly. Guide discovery now uses `describable_targets`, `SchemaReference`, and
`sample_text`; it does not parse CLI output, copy schema fields, construct runtime capabilities, or
load operator configuration for schema-derived topics. Disabled implementations remain describable.
Per-target contribution and schema-service failures are isolated into scoped, non-echoing issues,
rejected targets disappear from names and completion, and unrelated topics remain available.

The exceptional `concept-migration` topic links from onboarding and management without duplicating
its teaching. Its ten inert actions establish an immutable pre-edit resource identity and manifest
path baseline, preserve fresh out-of-tree backups, migrate and validate one manifest at a time,
classify workstation-probing inventory accurately, review changed null-secret semantics, verify the
complete operator inventory, and require a zero-failure doctor result. The topic teaches that
omitted and explicit-null secret fields both select the default; Azure and AWS use ambient
authentication only when the enclosing authentication block is absent, while Proxmox has no
no-secret mode.

Tests cover broken configuration, strict all-target schema construction, disabled implementations,
fail-soft explicit and index rendering, names-only and completion filtering, action bounds and
consent, migration identity preservation, scalar wire rendering, CommonMark safety, package data,
and completion registration.

Two independent implementation re-reviews approved the final branch with no findings. Their final
guide and completion runs each passed 457 tests. The repository-level release gates then passed:

- full non-integration suite: 5,607 passed and 3 deselected;
- Ruff check and format check: 602 files clean;
- mypy: 602 source files clean;
- Rulesync generated-output check: clean;
- locked-SDD check: clean;
- mandatory file lint: Prettier, markdownlint, and cspell clean.

One cross-SDD inconsistency remains owned by the merged declarative-schema effort. The permanent
0.14 upgrade guide correctly states that omission selects the default secret, but a later decision
branch incorrectly describes deleting only the field as a no-secret choice. This effort's topic
follows the implemented models and tests and does not repeat that incorrect branch. The
inconsistency was flagged to the operator for correction by the owning effort.
