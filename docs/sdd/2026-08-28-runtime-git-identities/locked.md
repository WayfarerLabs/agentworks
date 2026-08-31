# Runtime Git credential identities: locked

**Locked:** 2026-08-30

This effort is in final correction in PR #691. The lock takes effect when that PR lands on `main`;
until then, every artifact remains mutable and this file records the latest reviewed and
operator-accepted implementation state.

## What shipped

- Git credential providers declare static HTTPS scopes, validate their own resolved inputs before
  user or platform mutation, and later return either a stored credential or a managed helper. Core
  owns generic collision detection, routing, installation, and total reconciliation without knowing
  how a provider acquires a credential.
- The built-in GitHub and Azure DevOps schemas now require provider-local structured `source`
  objects. Secret sources remain provider-owned. The new `gh-cli` and `az-cli` sources install
  runtime helpers that acquire credentials from the target user's active CLI identity.
- Validation, runup, and materialization receive distinct fresh scoped contexts. Only runup receives
  the current admin or agent target. Materialization receives no target, and final CLI-backed tokens
  exist only inside the runtime helper invocation.
- CLI-backed runup performs bounded, read-only readiness checks in the target user's
  login/interactive shell. Missing, unauthenticated, or indeterminate tools produce value-safe
  recovery warnings without preventing helper installation. A missing command is described as the
  state at that point because later user-install/profile steps may add it. GitHub checks the same
  active account later used by `gh auth token`; Azure checks the current `az account` identity.
- Admin and agent initialization use one atomic, lock-protected reconciler. Every initialization
  rebuilds the complete Agentworks-owned credential state, including the empty state, so removed
  credentials cannot survive. Legacy state is removed only with exact ownership evidence.
- The imperative `vm add-git-credential` writer and duplicate provider routes are removed. The
  provider schema change and declarative reinitialization cutover are intentionally breaking for
  0.17.

## Validation, review, and live acceptance

The previously accepted implementation checkpoint before the post-handoff correction was
`118465fbc1f3f18a78b2b73e0fe2ed6449cccce9`, based on `main` checkpoint
`69051b7d7ebf09ae2ddd3e5241f7b94fe34ed79b`. It passed:

- 8,060 non-integration tests with one platform-specific skip;
- Ruff check and format plus strict mypy across 729 files;
- file lint, Rulesync drift, locked-SDD, Typer-isolation, and diff checks;
- 160 Python and 103 Node website tests plus deterministic root and `/agentworks/` builds;
- isolated wheel build/install and shipped CLI/provider documentation smokes; and
- hosted CI on Python 3.12, 3.13, and 3.14, CodeQL, website, and the aggregate success gate.

Independent project, fresh-correctness/security, and Muntz reviews converged cleanly at the exact
checkpoint. The last review correction limited GitHub readiness to the active account, made CLI
recovery guidance actionable, and reduced overlapping readiness coverage to ten behavioral cases. No
material correctness, security, migration, or complexity finding remained at that checkpoint.

The tester then found that the readiness probe did not source the target user's login environment
and could misclassify returned transport failures as authentication failures. The mutable
post-handoff correction extracts one neutral `check_required_commands` mechanism, uses the same
user-shell environment for provider-owned authentication checks, suppresses shell-startup output,
keeps startup failures distinct from command results, and reports the pre-install timing honestly.
Its final reviewed SHA and live revalidation evidence will replace this paragraph before the PR
returns to ready-for-merge state.

The operator exercised an agent configured with the existing secret-backed source and both new
CLI-backed sources. Initial unauthenticated `gh` and `az` state exposed one observability gap: the
secret-backed source reported its result while the CLI-backed sources were silent. The final
correction gives every runup a fresh target-bearing context and reports those states without
blocking helper installation. At that prior checkpoint, the operator then reported both prescribed
CLI-backed helper tests successful with no additional issue and accepted that result for merge on
2026-08-30. The post-handoff readiness correction still requires its own exact-head review, hosted
gates, and target-user-shell live revalidation before this record becomes final.

## Permanent homes and accepted boundaries

The provider-authoring contract lives in `cli/agentworks/capabilities/git_credential/README.md` and
the Git credential section of `cli/agentworks/capabilities/README.md`. Operator configuration,
migration, and recovery live in `docs/guides/resources.md`, `docs/guides/upgrading-to-0.17.md`, the
CLI command reference, and the guide content. Provider manifests, schemas, examples, focused tests,
and the reconciler own the executable contract. Nothing in this SDD directory is required to operate
or extend the feature.

Agentworks deliberately does not authenticate `gh` or `az` in this effort. Until planned user
features can own that setup, the operator authenticates the intended target-user identity once.
Runup reports readiness; it does not mutate login state. Provider-specific helpers may use granted
secrets for arbitrary provider-owned acquisition work, and core does not assume that a secret is a
Git token.

The operator owns merging PR #691. The effort lead does not merge it.

-- agw-ns-onboard-disco
