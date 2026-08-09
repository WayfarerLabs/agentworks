# Phase 1 Acceptance Evidence

- Date: 2026-08-06, updated 2026-08-08 after the PR #444, PR #446, and PR #455 rebases
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
omitted and explicit-null inner secret-reference fields both select the well-known default. Omitted
Azure and AWS `auth` defaults to ambient, and omitted Lima `placement` defaults to local; those
choices may also be declared explicitly through the tagged mode. Written legacy `service_principal`,
`credentials`, and `vm_host` fields follow their exact hard-error rewrites, including the distinct
ambient, ambient, and local mappings for outer explicit null. Proxmox has no no-secret mode.

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

## Post-rebase field-tree and default-filling evidence

PR #444 resolved the previous upgrade-guide inconsistency and landed the exhaustive
`FieldEntry.alternatives` contract. Guide rendering now shows Azure, AWS, and Lima union fields
under the arm that owns them, preserves the defaulted tagged block, marks recursion explicitly, and
gives addressable arms a full guide or describe pointer. Exact section traversal sees every arm:
unique selectors such as `auth.secret`, `auth.access_key_id`, and `placement.host` resolve, while
repeated `auth.mode` remains unavailable under the existing ambiguity guard. A reviewer follow-up
also pins the deeper case: candidates with the same intermediate block name continue through the
complete selector, then one unique terminal field resolves while a duplicated full leaf fails
closed. Runtime schema-catalog failure isolation still retains unrelated union-derived fields.

PR #446 moved owner-templated filling out of Pydantic validation context and into the decode
boundary. The guide needs no corresponding fill step: it renders `FieldDoc.default_template` with
the neutral `<name>` placeholder and never creates or validates a payload. The focused post-rebase
suite covered guide behavior, field reference and schema emission, manifest decode filling, retired
presence-shape errors, and platform config contracts: 627 tests passed.

The same follow-up tightened the migration consent boundary. `edit-one-manifest` is the only action
that applies a retired presence-shape rewrite or deletes its old outer-null line, for either a
pre-existing or TOML-derived manifest. `review-null-secret-fields` only inspects, classifies, and
confirms. If it finds a retired shape, it records the exact required rewrite and routes the manifest
back to the mutation and validation loop without changing the file. The expanded focused suite
passed 632 tests.

Both independent post-rebase reviewers approved the corrected branch with no remaining findings.
They reran 49 direct schema-adapter and migration tests, and the fresh-eyes pass also compared exact
field-row multiplicities across all 30 live schema targets. Final combined validation passed:

- guide, schema, migration, platform, Lima, and SSH focused suite: 675 tests;
- full non-integration suite: 6,470 passed and 3 deselected;
- Ruff check and format check: 611 files clean;
- mypy: 611 source files clean;
- Rulesync generated-output check: clean;
- locked-SDD check: clean;
- mandatory file lint: Prettier, markdownlint, and cspell clean.

## Round-3 projection-growth follow-up

The final performance review found accidental superlinear work in onboarding projection. The fix
remains inside the existing architecture: `GuideView` now materializes global kind and
implementation inventories only for concept roots permitted to read them, while resource,
implementation, and kind views construct no inaccessible global inventory. Onboarding snapshot
deduplication uses insertion-ordered identity maps, preserving the first-seen instance and
relationship order without linear list-membership scans. No cache, builder, bulk hook API, database
projection change, or wall-clock acceptance threshold was added.

Structural tests prove that global capability projection does not repeat per registry row, permitted
concept inventories remain complete, duplicate fact comparisons stay bounded, and first-seen order
is stable. The required project reviewer and independent fresh-eyes reviewer approved with no code
findings. Final rebased validation passed:

- guide suite: 428 tests;
- full non-integration suite: 6,477 passed and 3 deselected;
- Ruff check and format check: 611 files clean;
- mypy: 611 source files clean;
- Rulesync generated-output check and locked-SDD validation: clean;
- mandatory file lint: Prettier, markdownlint, and cspell clean.

## PR #455 git-token structural-union adoption

The post-merge adaptation was based exactly on `25e47637`. PR #455 expresses provider token
acquisition as an untagged scalar-or-table structural union. The live GitHub and AzDO references
both expose exactly one `stored` arm with `mode` and `secret` rows, the `{mode: stored}` outer
default, and the owner-templated `git-token-<name>` secret default. The declarable `git-credential`
reference carries the same complete nested field tree. Focused renderer tests bind all three guide
topics to those live `FieldEntry.alternatives`; no guide-specific field list or adapter switch was
needed.

The migration topic now preserves every released spelling deliberately. Omitted `provider.token`
continues to select the stored arm and default secret. A scalar secret name remains accepted
shorthand, while the guide writes the canonical tagged stored arm and preserves that name. An
omitted or null inner `token.secret` selects the default. Explicit outer `token: null` is retired
and routes through the manifest mutation loop for deletion or the exact `token: {mode: stored}`
rewrite. No minted arm is taught because none exists. The site-specific read-only null review
remains unchanged; any manifest validation hard error returns to `edit-one-manifest` before cutover.

The guide-layer contract tests exercise `reference_for("git-credential")`, both live provider
references, permanent capability validation, and secret-reference extraction. They prove scalar,
omission, tagged, inner-null, outer-null, and unknown-mode behavior together, so prose cannot drift
from either schema projection or service semantics. Final gate evidence for this adaptation is
recorded from the branch runs below:

- direct migration and schema-adapter contract slice: 55 passed;
- complete guide suite: 444 passed;
- guide, manifest, schema, retired-shape, and git-credential boundary: 3,069 passed;
- Ruff: all checks passed; format: 621 files clean;
- scoped mypy: 3 changed Python files clean;
- Rulesync generated-output check: clean;
- mandatory file lint: 272 Markdown files clean, 246 spelling files clean, and Prettier clean.

Superseded inherited-mypy evidence: full mypy was also run for the PR #455 adaptation, but the exact
merged base and that branch both reported the same three errors in unchanged
`tests/test_operational_json_reviewed.py`: two `attr-defined` errors for its former
`agentworks.db.database.tempfile` test override seam and one `SimpleNamespace`/`SessionRow`
list-item error. That guide-only adaptation did not change the separate operational-JSON test. The
later Phase 2 correction below removed the stale override seam, corrected the fixture typing, and
supersedes this inherited result with a clean full mypy run. The full non-integration suite was not
repeated for the earlier adaptation because no general production path changed; its 3,069-test
boundary included the complete guide suite and every relevant schema, manifest, retired-shape, and
git-credential service test.

## Phase 2 accepted-feedback correction

The PR #462 accepted-feedback correction preserves one complete JSON doctor report and now
propagates its failing status through the installed `agw` entrypoint. Doctor collects System, VM
sites, and Database facts once from one verified report-scoped database snapshot. Descriptor-first,
non-blocking, regular-file-only reads reject broken or looping symlinks, FIFOs, devices,
directories, sockets, and other unsupported entries with path-free diagnostics. Focused adversarial
tests cover the installed entrypoint, snapshot generation consistency, bounded special-file
handling, source integrity, aggregate call count, scale, human and JSON parity, and the extracted
module boundaries.

Accepted-feedback correction validation passed:

- full non-integration suite: 6,689 passed and 3 deselected;
- Ruff check and format check: 625 files clean;
- full mypy: 625 source files clean, superseding the inherited PR #455 note above;
- Rulesync generated-output check and locked-SDD validation: clean;
- mandatory file lint: Prettier, markdownlint, and cspell clean.

## Consolidated review correction

The consolidated review correction additionally pins the cross-platform protocol boundary. Hosts
without non-blocking, no-follow, and directory-relative open support fail closed before inspecting
the main database or its WAL/SHM entries. Focused tests simulate each missing primitive and verify
bounded, path-free failure. The correction also restores and pins every inspection symbol moved from
`agentworks.vms.manager.power`, including `VMDiagnostic`, as an explicit compatibility alias.

Consolidated review correction validation passed:

- focused adversarial snapshot and compatibility suite: 17 passed;
- wider doctor, entrypoint, machine-output, and parity slice: 175 passed;
- Ruff check and format check: 625 files clean;
- full mypy: 625 source files clean;
- locked-SDD validation: clean;
- mandatory file lint: Prettier, markdownlint, and cspell clean.

The full non-integration suite was not repeated for this narrow review correction. The integrated
base's 6,689-test result remains recorded above, and the lead reruns that suite after integration.

## Component-pinning correction

The final P1 correction removes the remaining intermediate-ancestor race. After resolving supported
requested-path symlinks, snapshot acquisition walks from the trusted filesystem anchor one component
at a time. Each directory is opened relative to the previously pinned fd with directory-only and
no-follow flags, and the resulting parent fd supplies every main, WAL, and SHM open. A deterministic
active-sidecar test replaces an intermediate ancestor with a symlink after path resolution and
proves the replacement database is never accepted. Stable requested paths containing a component
symlink remain supported because they resolve to the real identity before the descriptor walk.

Component-pinning correction validation passed:

- focused adversarial snapshot and compatibility suite: 20 passed;
- wider doctor, entrypoint, machine-output, and parity slice: 178 passed;
- Ruff check and format check: 625 files clean;
- full mypy: 625 source files clean;
- locked-SDD validation: clean;
- mandatory file lint: Prettier, markdownlint, and cspell clean.

The full non-integration suite was not repeated for this narrow P1 correction. The lead reruns that
suite after integration.
