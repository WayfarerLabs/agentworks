# Secret Sources: locked

**Lock record, updated 2026-08-10.** Secret Sources is implemented on the single ordinary
`feat/secret-sources` branch in PR #453. The lock binds when that PR lands on `main`.

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
values for operation-scoped consumers. The workstation process is the trust boundary: process memory
and ordinary traceback locals are outside the security guarantee. Durable and externally observable
boundaries remain strict: values do not enter provider-retained configuration, host argv, logs,
rendered diagnostics, or raised exception-object messages, arguments, attributes, cause, or context.
Lima instance YAML, WSL2 and Proxmox bootstrap staging, Azure `OSProfile.custom_data`, and AWS
`RunInstances.UserData` are the five final-inspection surfaces pinned by provider-shaped tests.
Lima, Azure, and AWS retain credential-free bootstrap payloads and join through a fixed command on
provisioning-transport stdin. WSL2 and Proxmox use private temporary staging with one verified
removal attempt.

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

- The implementation candidate is `e7010946`. Its full local non-integration suite passed with 7,472
  tests and 3 deselected. Ruff check and formatting passed, strict mypy passed across 650 source
  files, and file lint, Rulesync drift, locked-SDD, and diff checks passed.
- CI on that exact candidate passed on Python 3.12, 3.13, and 3.14, including CodeQL and the
  aggregate `ci-success` gate.
- Independent `agentworks-reviewer` review of the exact candidate found no Blocking, Important,
  Minor, or open issue. The fresh-eyes fallback findings were resolved before the candidate was
  pushed and independently checked the provider transports, provider-neutral bootstrap prose, and
  agreement between the completed plan and this lock.
- The permanent POSIX real-entry harness exercises the shipped console script with isolated config:
  implied environment resolution, prompt refusal, mixed variadic verification, direct OnePassword
  remediation, a declared source through an exact fake-only `op` boundary, doctor, guide output, and
  all-shell generated completion assertions. Every child result is scanned for the sentinel.
- Mutation review proved that tests fail when descriptor-derived source-key validation, `false`
  opt-out, retired-path enforcement, implied prompt fallback, value-free verification, variadic
  verification, all-shell completion behavior, key-free Lima configuration, or source-only prompt
  broker scope is neutered; each restored tree passed its focused gates.
- Proxmox and WSL2 now use private randomized bootstrap staging files and make one verified removal
  attempt on success, failure, timeout, and interruption. Lima, Azure, and AWS retain key-free
  provider payloads and join through stdin after boot. Remote Lima provision logs use the database
  VM identity, so normal deletion removes the exact log created for the VM.
- A real remote-Lima run at `383c0050` on 2026-08-10, using a rotated key, passed create,
  initialization, independent SSH and boot checks, and deletion. The retained instance YAML was
  credential-free, and the independent final sweep found no VM, database row, Lima instance or
  directory, detached artifact, log, SSH reference, workspace file, or operation temp entry. The
  later class-wide provider change retained Lima's established execution wrapper and fixed guest
  command; its final Lima coverage is provider-shaped, not another live run.
- The credential-free remote-Lima payload supersedes the staging mechanics added by main commits
  `668826af` and `d8eeb916`: the staged template no longer contains the Tailscale key, so retaining
  that hardening would add mechanism without protecting sensitive content.
- Marking PR #453 ready triggered the repository's Copilot reviewer. It declined because the diff
  exceeds its 20,000-line limit, so the required fresh-eyes fallback is the independent cold review
  recorded in PR discussion.

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

- The operator still owns merging PR #453 and ensuring release PR #402 refreshes before release.
- Real 1Password authentication and multi-account parsing were not exercised with operator
  credentials. Tests and the acceptance harness deliberately use a closed fake-provider boundary.
- The acceptance harness intentionally supports POSIX hosts only. Generated PowerShell completion
  text is validated on Linux, but no native Windows CLI run is claimed.

## Supersession (2026-08-14)

The `2026-08-12-simplification-pass` effort's wave 1 deleted `validate_interaction_policy` and the
`phase7` corpus that enforced its use, so the conventions this SDD's `operator-surfaces-lld.md`
records as normative no longer describe HEAD. Recorded here because that LLD is the only place on
`main` that still specified the mechanism as current design, and this directory is locked. The LLD
sections stating those requirements, principally its lines 145-153, 184-189, 396-400, 556-566, and
669-677, are superseded in full.

What went: the interior half. The 152 call sites that re-checked a first-party `InteractionPolicy`
already carried by a typed parameter, and the AST guard requiring
`interaction = validate_interaction_policy(interaction)` as the first executable statement of every
policy boundary. A value forwarded between our own functions within one execution under strict mypy
is interior by the trust-boundary doctrine the simplification pass landed in
`development-principles` principle 3, so those checks bought nothing.

What stayed, at the boundary that deletion first got wrong: `interaction` is still checked once on
arrival, by `require_exact_interaction_policy`. `InteractionPolicy` is a `StrEnum` and every
consumer compares it by identity, so a value that is equal but not identical (a plain `"refuse"`)
takes the not-refuse branch and resolves through an interactive source in a run that meant to
refuse. That is reachable through the published service surface, whose functions take `interaction`
from callers outside our type checking, and a probe against a configured OnePassword source
reproduced the fault before the check was added. Forwarding a checked policy onward is not
rechecked, and that is the whole difference from the 152-site convention this note supersedes.

Where the check goes is mechanical rather than a judgment about which functions read as published:
**every construction of a `ResolutionPolicy` is preceded on its own call path by the check**, and
`grep -rn "ResolutionPolicy(" cli/agentworks/` is the whole audit. The six constructions today sit
in five functions (`secrets.verification.verify_secrets`,
`secrets.orchestration.resolve_for_command`, `secrets.resolver.Resolver.__init__`,
`secrets.resolve.resolve_partial_for_reveal`, and the three inside `Resolver`), and every path to
`resolve_batch` crosses one. The published-service framing this supersedes named four of them and
got both directions wrong: `env.show.show_env` forwards to `resolve_partial_for_reveal` rather than
consuming, and `resolve_partial_for_reveal` consumes and was left unchecked because it had a single
caller, which is the same reasoning the 152-site deletion had to correct.

Three entry points call the check themselves rather than inheriting it from the resolver they reach:
`vms.manager.power.delete_vm`, `agents.manager.lifecycle.reinit_agent`, and
`workspaces.manager.rehome.rehome_workspace`. Position, not presence, is what those buy. Each does
consequential work before reaching its resolver, and `delete_vm` reaches its resolver inside a
best-effort span that downgrades an `AgentworksError` to a warning, so a deeper rejection there was
swallowed and the delete ran to completion with the backend delete skipped: exactly the #329
orphaning the span exists to prevent. A rejected policy must leave nothing behind, so the check runs
before any prompt, any DB write, and any transport.

Every interaction behavior recorded under "What shipped" still holds: the enum, caller-authorized
prompting, fail-before-prompt ordering, `agw secret verify`'s refuse-by-default posture and its
final `--allow-interaction` opt-in, and the forwarding of an explicit policy across every boundary.
No call site changed which policy it passes, and no operator-visible behavior changes. One coverage
question was checked rather than assumed: the corpus's lexical assertion that auth-key acquisition
and `_ensure_tailscale` sit inside the activation hold is covered observationally, with
failure-unwind and single-release coverage the lexical pin could not see, in
`cli/tests/vms/test_lifecycle_orchestrated.py` and `cli/tests/vms/test_vm_nodes.py`.

Issue 529 keeps the half not fixed here: `resolve.py` dispatches the interaction broker on a
hardcoded `name == "prompt"`, so the underlying identity comparison fails loud for that one backend
and silent for every other. That is an accident of a built-in-name special case rather than a
property of the gate, and no backend's safety should depend on its name.

This note narrows the interaction-policy validation convention only. The source-and-backend model,
the resolution protocol, the value-free outcome vocabulary, the trust-boundary statement, and the
0.14 break recorded above are untouched.
