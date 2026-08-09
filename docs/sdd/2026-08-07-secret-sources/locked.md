# Secret Sources: locked

**Prospective lock record, updated 2026-08-09.** The SDD is not yet complete: Phase 10's
operator-gated live remote-Lima rerun remains open. Secret Sources is implemented on the single
ordinary `feat/secret-sources` branch in PR #453. This record will become the final lock only after
that run, final review, and the remaining Phase 10 boxes complete; the lock binds when the PR lands
on `main`.

## What shipped

Secrets now resolve through declarable `secret-source` resources rather than directly through
backend implementation names. A source selects one class-registered `secret-backend`, owns its
shared configuration, and exposes backend-specific mapping validation through a descriptor-derived
map host. The registry, dependency graph, settings chain, inspection surfaces, runtime resolution,
schema emission, samples, and reference metadata all use source identities without a backend-name
fallback.

The synthesized `env-var` and `prompt` sources preserve the simple case: absent settings still imply
that chain, explicit uses of those names remain valid, environment-name derivation is unchanged, and
prompt interaction remains caller-authorized. OnePassword now uses a declared source whose
configuration owns `account` and an optional positive timeout (30 seconds by default); each secret
mapping is one scalar `op://` reference.

Resolution constructs at most one lazy client for each attempted source turn, applies one monotonic
budget across client setup and backend work, closes the source before proceeding, and retains
first-success precedence, batching, deduplication, soft fallthrough, hard-failure halt, readiness
skipping, and fail-before-prompt behavior. Shared value-free outcomes report `resolved`,
`unavailable`, `refused-interaction`, `timeout`, or `resolution-failure`; private batches alone hold
values for operation-scoped consumers. Cleanup and interruption paths scrub provider values,
arguments, native process results, and temporary copies.

`agw secret verify NAME...` is the explicit read-and-prove surface. It refuses interaction by
default, accepts the final `--allow-interaction` opt-in unless global non-interactive mode forbids
it, renders one value-free row per unique requested secret, and exits nonzero if any row is not
resolved. Bash, Zsh, and PowerShell dynamic completions all support repeated secret names and the
final option spelling. Doctor and describe remain non-probing projections.

## Intentional 0.14 break

Configured backend implementation names such as `onepassword` are no longer implicit aliases in
`[secret_config].backends` or `secret.backend_mappings` keys. An exact backend-name miss produces a
hard source-declaration rewrite; it is not accepted through a compatibility source, legacy parser,
deprecation warning, or runtime fallback. The synthesized `env-var` and `prompt` source names do not
break.

For OnePassword, operators declare a source, move the former per-secret `account` into that source,
and replace each old table mapping with its scalar `op://` reference. The feature commit carries a
`BREAKING CHANGE:` footer with this migration and the new timeout default. The permanent upgrade
guide at `docs/guides/upgrading-to-0.14.md` contains the before/after configuration.

## Acceptance and review evidence

- The exact independently reviewed local head `0dad790f` passed **7,562 non-integration tests with 3
  deselected**. Ruff, formatting, strict mypy over 646 files, file lint, Rulesync drift, locked-SDD,
  and diff checks passed.
- PR CI passed on Python 3.12, 3.13, and 3.14, including CodeQL and aggregate `ci-success`, at the
  final code-and-evidence head `94a26dc7`.
- The permanent POSIX real-entry harness exercises the shipped console script with isolated config:
  implied environment resolution, prompt refusal, mixed variadic verification, direct OnePassword
  remediation, a declared source through an exact fake-only `op` boundary, doctor, guide output, and
  all-shell generated completion assertions. Every child result is scanned for the sentinel.
- Mutation review proved that tests fail when descriptor-derived source-key validation, `false`
  opt-out, retired-path enforcement, implied prompt fallback, value-free verification, variadic
  verification, all-shell completion behavior, VM-create operation scrubbing, remote-create state
  scrubbing, or BaseException row unwind is neutered; each restored tree passed its focused gates.
- Independent cold review converged cleanly at `0dad790f` after tracing the complete owned Lima
  delivery graph through manager, remote creation, SSH input, initializer cleanup, and teardown.
  Exact interruption tests cover first-transfer and post-transfer ownership, sensitive native
  traceback removal, warning/cleanup non-masking, and SystemExit/GeneratorExit identity.
- Marking PR #453 ready triggered the repository's Copilot reviewer. It declined because the diff
  exceeds its 20,000-line limit, so the required fresh-eyes fallback is the independent cold review
  recorded above. Findings from the latest fallback review are tracked in Phase 10 rather than
  omitted from this prospective record.

## Permanent record

Current operator and contributor behavior is documented outside this SDD in:

- `cli/agentworks/capabilities/README.md` and
  `cli/agentworks/capabilities/secret_backend/README.md`;
- `cli/agentworks/secrets/README.md`, `cli/README.md`, and `cli/agentworks/sample-config.toml`;
- `docs/guides/resources.md` and `docs/guides/upgrading-to-0.14.md`;
- the universal `concept-secrets` guide contribution and relevant ADRs.

The universal guide contract was available and updated, so no onboarding deferral or temporary
adapter remains. Nothing in this SDD directory is required to understand or operate the feature.

## PR and release coordination

PR #453 is the feature PR. PR #452 was the same branch lineage and was closed by GitHub when the
operator-required branch rename added the conventional `feat/` prefix; it was not a stacked
implementation PR. There is no additional remote planning or phase branch.

Release PR #402 (`chore(main): release 0.14.0`) was already open before Secret Sources reached its
final feature commits, so its generated notes do not yet include this intentional break. Do not edit
or push that release branch from this effort. After #453 merges, the release record must refresh
from the `BREAKING CHANGE:` footer before 0.14.0 is published. The saga owner has also recorded in
PR discussion that the breaking-content ledger needs the same next-round update; this feature does
not edit the saga SDD.

## Honest residual work

- A final real remote-Lima run must exercise the ephemeral stdin join and inspect the actual
  retained instance YAML and `limactl list --json` before this record can lock. This session does
  not have the concrete remote inventory, SSH identity, or a fresh authorized Tailscale key. The key
  exposed by the earlier provider run must be revoked and must not be reused.
- The operator still owns merging PR #453 and ensuring release PR #402 refreshes before release.
- Real 1Password authentication and multi-account parsing were not exercised with operator
  credentials. Tests and the acceptance harness deliberately use a closed fake-provider boundary.
- The acceptance harness intentionally supports POSIX hosts only. Generated PowerShell completion
  text is validated on Linux, but no native Windows CLI run is claimed.
