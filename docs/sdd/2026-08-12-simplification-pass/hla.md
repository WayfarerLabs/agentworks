# Simplification Pass: High-Level Architecture

This effort ships deletions, consolidations, and guidance rather than a new subsystem, so the
architecture here is three things: the doctrines the code changes implement, the delivery topology
for guidance, and the sequencing that keeps every merge green and honest.

## Doctrine 1: validate at boundaries, trust the interior

The system has exactly these trust boundaries today:

1. **Operator-authored input**: config TOML, YAML manifests, CLI arguments, environment.
2. **External processes and services**: provider SDK responses, subprocess output (`op`,
   `tailscale`, git), PyPI, GitHub content (per the `github-input-trust` rule).
3. **Packaged-but-untrusted evidence**: release-notes bodies rendered as inert text.

Everything else, all first-party code and the values it produces under mypy strict, is interior.
Interior guarantees are carried by types, frozen dataclasses, and registration-time checks, not by
runtime re-validation. A validator that survives this pass names its boundary in its docstring; a
validator that cannot name one is deleted.

The **future external-plugin boundary** is real (operator ruling) but not yet load-bearing. Its
design, recorded now so the kept mechanisms have a stated purpose:

- **Load boundary** (when external loading lands): a plugin module is untrusted at import and
  registration. Registration-time checks that earn their keep for that future: constructibility,
  duplicate/collision policy, and the trusted/untrusted catalog split with fail-soft scoped issues.
  These stay and are documented as the boundary's skeleton.
- **Contribution boundary**: plugin-supplied guide topics and action records are data, validated
  where they enter the catalog. The inert-action token discipline (literal tokens, sensitive input
  rejection) stays. The byte-cap and markdown-scanning arsenal sized for adversarial markdown is
  deleted; when external contributions actually arrive, validation is re-derived against the real
  channel, which is cheaper and more accurate than maintaining a guess.
- What does not return: re-validating first-party contributions on every render, reflective
  signature conformance duplicating mypy, and MRO walks re-checking `@final`.

## Doctrine 2: tests assert behavior; agreement is derived

The test-value rule (FRD R1.2) generalizes `no-prose-policing-tests`: the unit of protection is the
invariant, never the spelling of an authored artifact. Concretely, the shapes this pass deletes and
must not regenerate: AST assertions on our own source, verbatim pins of workflows and CSS tokens,
required-phrase lists, wording blacklists, and mutate-one-string-assert-raises loops over reviewed
templates. The shapes it keeps and adds: exit codes and error types, derivation parity against a
canonical source, structural presence, and threshold (not exact-value) assertions.

## Guidance delivery topology

Probed facts (findings.md): `paths:`-scoped rules inject lazily on project-path file touches and
never reach subagents working in out-of-tree checkouts; frontmatter-less rules and CLAUDE.md always
load. Therefore:

- The two new compact rules (trust-boundary, test-value) ship frontmatter-less, and their size is
  budgeted like always-on context (target: under 25 lines each).
- The `agentworks-dev` and `agentworks-reviewer` personas gain a self-heal preamble: verify the
  named always-on rules are present; when absent (worktree isolation), read them from
  `.claude/rules/` in the current checkout before starting work.
- One plan item verifies whether harness-created worktrees (`isolation: "worktree"`) sit inside or
  outside the project path, and the delegation guidance in `agentic-dev-process` records the answer.
- Rule consolidation (FRD R7.2) shrinks the paths-scoped corpus so that when it does load, it is
  worth its tokens.

## Migration-notes pipeline (FRD R6)

No new machinery. The existing chain is: conventional-commit `BREAKING CHANGE:` footers, which
release-please accumulates into `cli/CHANGELOG.md`, which is packaged into the wheel, which
`agw guide concept-release-notes/vX-Y-Z` renders as bounded offline evidence. The convention this
pass adds: the footer text is written as operator-actionable migration guidance (what breaks, what
to change, one example), and the guide's migration topic points agents at the packaged evidence for
the installed version span. Compat shims stop being the default answer to breaks; the note is.

## Sequencing and delivery vehicle

Phases (detailed in [plan.md](plan.md)):

0. **Verify and place guidance.** R1 plus the harness-worktree probe. Smallest possible PRs, first
   to merge, because every later PR is reviewed under these rules.
1. **Deletions.** Independent, contained, per-cluster PRs (phase7 corpus, workflow-pinning test,
   prose-policing sweep contributions, dead surface, dead descriptor fields). Each PR is pure
   subtraction plus the replacement structural tests findings name.
2. **Consolidations.** Per-cluster PRs with behavior pinned by existing suites: schema-state reader,
   sentinels derivation, resolve-path unification, platform extractions, process-tree dedup.
3. **0.14 reshapes.** The breaking fixes ride the release window as one coordinated group (rename,
   union collapse, compat expiry), each writing its R6 migration note.
4. **Promotion and closeout.** Plugin-boundary design recorded in a permanent home (`docs/arch/` or
   ADR), spawned-effort seeds delivered, lockfile.

Vehicle: **coordinated independent PRs off main**, not a stack; the phases gate on review
convergence, not on each other's diffs, and no two PRs reshape the same files. The exception is
phase 3, whose PRs may stack if the rename and the union collapse touch the same config-loading
seams. Every PR is a complete, honest increment per the one-PR-per-feature rule with the size
ceiling respected; nothing here approaches the ceiling individually.

Ownership: this SDD's lead owns the plan and rulings; implementation of each phase item is delegated
per the dev process with worktree isolation, which is exactly why phase 0 lands first.

## Risks

- **Deleting a validator that was quietly load-bearing.** Mitigation: every deletion PR runs the
  full suite; where a validator guarded a real invariant, findings.md names the structural
  replacement, and the replacement lands in the same PR.
- **0.14 window closes mid-pass.** Mitigation: phase 3 items are sequenced early within the release
  cycle and are individually small; if the window closes, unshipped renames move to a
  recorded-expiry compat entry instead (FRD R5.4 fallback).
- **Coordination drift with prose-test-purge.** Mitigation: the boundary is by-file (FRD
  Boundaries), contributions delivered as a findings message per SDD message conventions.
- **Guidance rules grow into essays.** Mitigation: hard size budget in R1.3, enforced at review.
