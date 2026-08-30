# Implementation Plan: Runtime Git Identities

<!-- cspell:ignore sdds -->

- Status: Ready for merge
- Date: 2026-08-28
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)
- Detailed design: [credential-helper-lld.md](./credential-helper-lld.md)
- Migration: [migration-strategy.md](./migration-strategy.md)
- Research: [prior-art-research.md](./prior-art-research.md)
- Design baseline: `4c47f9c70a58f62f3a2e366f2870013d5fa032b8`
- Delivery vehicle: one draft PR carries design review and, after design clearance, implementation

## Delivery Rules

- Until design clearance, the draft PR contains only this SDD directory and carries
  `sdd:runtime-git-identities` plus the author-owned `review-requested` label. It has no merge
  intent while design review is active. After design clearance, the same draft receives the
  pre-authorized implementation.
- The operator authorized the original four published SDD feedback/fix iterations plus up to two
  additional SDD-only rounds while findings converge. Each iteration critically evaluates the
  complete batch, removes `review-requested` before edits, pushes one coherent corrected checkpoint,
  comments with dispositions/evidence, and reapplies `review-requested`. A requirements change,
  divergent redesign, or work beyond that extended budget stops for operator direction.
- After the complete implementation is handed off, the operator separately authorizes up to four
  implementation feedback/fix rounds while findings converge.
- Private artifact review uses `agentworks-reviewer` and Muntz before the first public handoff.
  Document-only work does not add a generic code-correctness lane.
- Review convergence recommends implementation but does not authorize it. The operator has
  pre-authorized implementation on this same draft PR only if the final design checkpoint is clean
  with the designated next-steps boundary reviewer (`agw-next-steps`), project reviewer, and Muntz;
  a material design disagreement stops first.
- Before merge, the operator will live-test both new CLI-backed arms through the generated helpers.
  The Azure proof obtains the Azure DevOps-audience token through an active service-principal `az`
  identity and uses the proposed organization-username/token-password response for real Git
  operations. Failed or unavailable proof is not a pass and keeps the implementation from merge;
  failure returns the affected arm for design/implementation correction.
- Implementation begins on this same draft branch from the exact cleared design checkpoint and lands
  in the same PR so the provider contract, callers, reconciliation, cleanup, and live behavior
  cannot land in a misleading half-state. This effort does not define a safe implementation split.
- No implementation checkbox is checked before design clearance. Completed checkboxes remain
  immutable after merge.
- The effort lead does not merge its own PR.

## Full Gates

From `cli/`:

```console
uv run ruff check agentworks/ tests/
uv run ruff format --check agentworks/ tests/
uv run mypy agentworks/ tests/
uv run pytest tests/ -m 'not integration'
```

From the repository root:

```console
./scripts/lint-files.sh
./scripts/check-locked-sdds.sh
./scripts/rulesync-upgen.sh --check
python3 -m unittest discover -s website/tests -p 'test_*.py'
node --test website/tests/*.test.mjs
```

The implementation additionally receives isolated-home installed-wheel checks plus authorized live
GitHub and Azure Repos validation under the `integration-testing` and `agw-test-env` skills. Tests
assert structure, behavior, state, and value containment; they do not police authored prose.

## Phase 0: Design Checkpoint

- [x] Base the effort on current `origin/main` and record the exact implementation baseline.
      (2026-08-28: `4c47f9c7`.)
- [x] Audit provider configuration, secret graph edges, runup, material building, admin/agent
      initialization, direct add, Git registration, stale-state behavior, tests, and permanent
      collateral at the baseline.
- [x] Research the Git credential protocol, GitHub CLI token boundary, Azure CLI Azure DevOps token
      acquisition, service-principal authorization, and Git Credential Manager prior art using
      primary sources.
- [x] Draft FRD, HLA, helper LLD, migration strategy, prior-art research, and this plan.
- [x] Run artifact lint, spelling, link, lockfile, and diff checks. (2026-08-28: permanent links
      checked against the primary-source research; `lint-files.sh --fix`, locked-SDD, and diff
      checks passed.)
- [x] Obtain private `agentworks-reviewer` and Muntz passes and resolve every material finding.
      (2026-08-28: four converging private passes removed the direct writer/manifest, bounded and
      unified helper execution/reconciliation, closed stale-state and concurrency gaps, and ended
      with no unresolved material finding.)
- [x] Commit with the current Agentworks session trailer, push, open the design-only draft PR,
      create or apply `sdd:runtime-git-identities`, and apply `review-requested`. (2026-08-28: draft
      PR #691 opened from `docs/git-credential-runtime-identities`; the review-requested signal was
      applied after this final bookkeeping commit.)
- [x] Incorporate public feedback cycle 2 by making providers own secret interpretation, final Git
      credential construction, and forge-to-HTTPS-scope translation; limit core to scoped delivery,
      generic routing, validation, and reconciliation; add the runtime-only executable check; and
      remove speculative launcher ABI/versioned-root promises while retaining the current adjacent
      pairing proof. (2026-08-28: FRD, HLA, LLD, migration, research, and implementation plan
      corrected together.)
- [x] Incorporate public feedback cycle 3 by removing the duplicate core executable probe added in
      cycle 2, putting command detection and actionable fixed failure guidance in each provider
      helper, and structurally limiting managed helpers to zero-secret configurations. (2026-08-28:
      accepted the Muntz findings with the saga lead's diagnostic and enforceability refinements.)
- [x] Incorporate authenticated operator correction cycle 4 by making provider inputs and outputs
      orthogonal, removing core's secret-presence/output restriction, replacing built-in `token`
      with independently provider-owned required structured `source` unions, removing shorthand and
      whole-source omission, and recording the schema break for shipped release notes. (2026-08-28:
      FRD, HLA, LLD, migration, research, and implementation plan corrected together.)
- [x] Incorporate authenticated operator correction cycle 5 by removing the premature
      pre-implementation Azure inventory gate and requiring the operator to live-test both new arms
      before merge. (2026-08-29: the blocked probe established that Azure VM inventory does not
      imply Azure Repos inventory; the operator accepted both final live-proof gates and authorized
      implementation to proceed.)
- [x] Collect the requested public artifact perspectives and process the separately authorized SDD
      rounds to a clean final design checkpoint. (2026-08-29: cycle 4 received exact-head clean
      project and Muntz reviews; the next-steps lead's two published concerns were incorporated; no
      further material feedback arrived in the one-hour window.)
- [x] Confirm this final operator correction is clean with the project reviewer and Muntz, then
      begin the pre-authorized implementation on this same draft PR. (2026-08-29: both exact-head
      re-reviews were clean after one shared wording correction; implementation is unblocked.)
- [x] Incorporate authorized correction cycle 6 by removing the speculative previous/current
      launcher pairing, narrowing mixed-state guarantees to no partial new activation and no
      reactivation after disablement, and aligning helper lifetime with its open generation-owned
      descriptor. (2026-08-29: second and final extra SDD-only round; this correction supersedes the
      adjacent-pairing language retained in cycle 2 while preserving that completed history.)

### Phase 0 Definition of Done

- Every requirement maps to architecture, detailed design, migration, implementation work, and an
  objective proof.
- Secret-backed validation and runtime CLI acquisition have distinct, unambiguous lifecycle rules.
- Providers own secret interpretation, acquisition, forge scope translation, and final output; core
  owns generic Git routing and total empty-state reconciliation.
- The removed direct-add path has no replacement writer or installed-state compatibility seam.
- Azure Git authentication and Git config precedence are named implementation proofs, not assumed
  facts; both CLI arms are final generated-helper pre-merge proofs.
- Review reports no unresolved material design or complexity finding.

## Requirement Traceability

| Requirement | Design authority                     | Planned proof                                      |
| ----------- | ------------------------------------ | -------------------------------------------------- |
| R1, R2      | HLA models; helper LLD contract      | phase 1 contract/config/context tests              |
| R3          | HLA lifecycle; helper LLD runup      | phases 1 and 2 provider-policy tests               |
| R4, R5      | HLA runtime; helper LLD helper rules | phases 2, 3, and 5 runtime/locking/live tests      |
| R6          | HLA builder; helper LLD scope router | phases 2 and 4 generic selection/collision tests   |
| R7, R8      | HLA reconciler/config boundary       | phases 3 and 4 idempotence/ownership tests         |
| R9          | HLA one-write-path decision          | phase 4 removed-command and residual tests         |
| R10         | HLA security; helper LLD containment | phases 2 through 6 adversarial/value scans         |
| R11         | HLA permanent homes                  | phase 6 collateral and generation gates            |
| R12         | FRD non-goals; HLA identity details  | phase 6 residual sweep and future-feature boundary |

## Phase 1: Provider Contract and Configuration

- [x] Replace `token` with independently provider-owned required structured `source` unions: GitHub
      `secret | gh-cli`, Azure DevOps `secret | az-cli`; reject outer omission/scalar shorthand
      while retaining only the explicit secret arm's inner default, and structurally derive each
      provider's complete secret-reference set.
- [x] Prove current CLI arms declare no secret, secret arms declare their configured reference, a
      synthetic provider may declare several, scoped delivery refuses every undeclared read, and
      both stored and managed-helper outputs are accepted independently of secret presence.
- [x] Add frozen generic HTTPS-scope, stored-credential, managed-helper, and credential-material
      types with value-safe construction and representations; require every managed helper to
      provide fixed value-safe failure guidance, and keep the provider result free of a redundant
      provider-echoed credential name.
- [x] Cut the capability descriptor and all in-tree providers atomically to contract version 3 and
      the single context-taking `credential_material` operation; each provider descriptor uses
      `AgwModel` directly and owns its complete schema without a shared source/config base.
- [x] Delete v2 `helper_entry`, `credential_lines`, static-store username routing, universal
      `secret_name`, naked-token calls, and core token-map assumptions.
- [x] Retain provider-owned static validation and `defaults.runup_git_credentials`; make current
      CLI-backed runup a provider no-op.
- [x] Update schema/resource samples and provider contract tests without asserting authored prose.

### Phase 1 Definition of Done

- Every provider config validates through the framework, declares one static scope set, and produces
  exactly one final payload from its scoped context.
- Graph edges, scoped delivery, and runup behavior follow provider declarations without core
  acquisition special cases.
- No contract-version-2 implementation or adapter remains.

## Phase 2: Runtime Helpers and State Builder

- [x] Make GitHub and Azure secret arms read their scoped context, retain provider-owned validation,
      and return final `StoredCredential` values; add a synthetic multi-secret exchange provider to
      prove core remains acquisition-agnostic, plus a secret-bearing managed-helper fixture to prove
      core does not infer output from input shape.
- [x] Make GitHub and Azure CLI arms return fixed provider-owned `ManagedHelper` programs that
      declare `gh`/`az`, emit complete Git credential responses, handle dependency execution
      failure, and perform no provisioning-time CLI checks.
- [x] Pin the GitHub helper to `gh auth token --hostname github.com` and the Azure helper to the
      exact resource-ID/query/TSV command plus provider-owned organization-username/token-password
      response.
- [x] Implement one core bounded managed-helper envelope using guaranteed coreutils `timeout`; pass
      the Git request, validate only response protocol shape, suppress upstream output, and emit the
      provider-fixed value-safe failure guidance.
- [x] Build the generic longest-path dispatcher, host-specific Git include, generation layout, and
      atomic `current` activation from final provider materials; store each static credential as an
      exact private Git-protocol record and carry explicit immutable stored-credential and
      managed-helper file sets in the desired-state model.
- [x] Prove providers translate GitHub repository/owner and Azure organization into generic path
      tuples; preserve selection outcomes, reject duplicate nonempty scopes, and retain released
      multiple-host-default behavior.
- [x] Add fake helper/protocol tests for success, missing command, nonzero, timeout, malformed or
      control-bearing response, noisy stderr, unsupported operations, and no-match cases.
- [x] Prove stored credential values containing Git/URL delimiters such as `:`, `@`, `/`, `%`, `?`,
      `#`, `=`, and `\` round-trip literally without URL parsing or serialization.
- [x] Prove built-in credentials appear in no dispatcher, include, representations, errors, logs,
      process arguments, managed-helper programs, or test artifacts; cover every built-in
      managed-helper program without adding secret-lineage scans or provider attestations.

### Phase 2 Definition of Done

- The complete desired per-user state can be built from final provider material before any target
  mutation.
- Managed helpers acquire credentials only on a matching Git `get` and fail safely otherwise.
- Core selection is generic HTTPS context and independent of provider vocabulary or store usernames.

## Phase 3: Atomic Per-user Reconciliation

- [x] Implement one transport-neutral reconciler that stages private generations, uses one stable
      shared/exclusive lock, atomically activates `current`, cleans inactive state, and supports an
      empty desired state.
- [x] Register exactly one Agentworks include and remove it when empty; remove only exact legacy/new
      Agentworks config values.
- [x] Prove host-scoped helper reset and `useHttpPath` behavior against Debian Bookworm's Git 2.39
      with unrelated operator helpers/includes present.
- [x] Delete the exact Agentworks-owned legacy `~/.git-credentials` path without reading its secret
      content; preserve unrelated Git config and paths.
- [x] Add idempotence and transition tests for fresh, same, add, remove, scope change, mode switch,
      rejected-last-static, mixed, malformed legacy, staged failure, and empty states.
- [x] Prove concurrent helper/swap/empty cleanup never mixes generations or deletes files under an
      in-flight helper.
- [x] Prove a staged or activation failure cannot expose partial new state, cleanup never
      reactivates a mechanism after disabling it, and retry converges from retained complete or
      no-credential state.
- [x] Prove bounded helper/reconciler lock timeouts, open-generation helper lifetime during cleanup,
      child lock-descriptor closure, corrupt owned-path repair, and empty reconciliation.

### Phase 3 Definition of Done

- One call converges the Agentworks-owned Git state exactly without altering unrelated Git config.
- A failed installation exposes no partial new credential state.
- Zero desired credentials removes every provably Agentworks-owned credential/routing artifact; only
  the inert stable lock may remain. An indistinguishable generic `store` helper has no managed
  credential file to serve.

## Phase 4: Admin and Agent Cutover

- [x] Make admin initialization/reinitialization invoke the reconciler unconditionally before
      private Git-backed setup.
- [x] Make agent creation/reinitialization use the same builder/reconciler unconditionally and
      delete its duplicate writer/config logic.
- [x] Change composition roots to resolve the provider-declared union once, construct a
      `ScopedSecrets` context per provider, collect final material, and preserve secret-resolution
      failures plus rejected-provider skip/partial behavior.
- [x] Delete `vm add-git-credential`, its manager path, tests, completion/help/docs entries, and
      static-store append/fallback behavior; teach the declarative template plus reinit path.
- [x] Delete conditional provider gates, duplicate constants/comments, installed-state speculation,
      and all stale-state paths.

### Phase 4 Definition of Done

- Admin and agent flows install the same format through one reconciler.
- Full user initialization is the sole declarative authority and always runs, including empty state.
- No imperative material writer or global `--replace-all credential.helper` remains.

## Phase 5: Live Integration and Runtime Proof

- [x] Load `integration-testing` and `agw-test-env`; inventory exact authorized GitHub and Azure
      identities, repositories, mutation budget, cleanup, and sensitive-output controls.
- [x] Prove a real `gh` identity supports HTTPS clone/fetch and a reversible write through the
      generated helper; exercise missing/unauthed state in a disposable user home.
- [x] Prove a real `az` service-principal identity obtains an Azure DevOps-audience token and
      supports HTTPS clone/fetch and a reversible write through the generated helper.
- [x] Verify Azure organization membership/permissions and wrong-identity failures are distinguished
      from helper/CLI failures without leaking provider output.
- [x] Prove per-`get` reacquisition with fake CLIs returning different values on consecutive calls;
      do not manipulate live login or cache state owned by future authentication features.
- [x] Stop for design revision if Azure Git or host-scoped Git config behavior disproves the LLD; do
      not add a Git upgrade, GCM dependency, or alternate transport implicitly.

### Phase 5 Definition of Done

- The shipped helper works for real Git, not only direct CLI token commands or REST probes.
- Runtime failure messages are actionable and value-safe.
- Tester cleanup leaves no repository, branch/tag, identity, token, or VM residue beyond the
  explicitly retained evidence policy.

## Phase 6: Permanent Collateral, Review, and Delivery

- [x] Rewrite `capabilities/git_credential/README.md` prose and requirements around provider-owned
      acquisition and final materialization, generic HTTPS scopes, stored credentials, managed
      helpers, scoped contexts, reconciliation ownership, and provider-authoring rules; align the
      Git-credential section of the root `capabilities/README.md` to the same sharp boundary.
- [x] Update capability/resource prose, schemas, examples, sample config/manifest disposition,
      command reference, guide concepts, CLI README, upgrade guide, and plugin documentation; the
      upgrade guide must pin the paired CLI/full-resource-directory cutover, short validation
      outage, pre-reinitialization rollback, and post-reinitialization fix-forward boundary.
- [x] Make the implementation's release-visible feature commit a breaking Conventional Commit and
      add one paragraph `BREAKING CHANGE:` footer describing required structured `source`; verify
      Release Please derives exactly one breaking release-note entry. Do not edit its generated
      changelog directly or duplicate the marker through the PR title.
- [x] Document the exact `gh`/`az` runtime commands, active-identity prerequisite, Azure
      organization permission prerequisite, and recovery path.
- [x] Add no prose-policing tests; retain structural schema/generation/command/helper/collateral
      validation only.
- [x] Incorporate the first changed-head implementation review batch: neutralize inherited Bash
      tracing, preserve one stable lock through corrupt-path repair, bound escaped helper output,
      remove directory-shaped legacy state, short-circuit unknown operations, and delete the unused
      constants, VM-owned provider resolver, and provider dictionaries. (2026-08-29: focused
      adversarial and affected-flow suites passed with the new Git-domain construction and
      announcement paths.)
- [x] Incorporate correction round 3 without rewriting completed history: separate static scopes and
      side-effect-free input validation from later payload acquisition; reject all exact scope
      collisions before creation; share launcher parent handoffs while excluding reconciliation;
      require the exact legacy helper-registration witness before removing `~/.git-credentials`;
      sanitize reconcile diagnostics; remove provider/route counts; and document target-user CLI
      identity mutability. (2026-08-30: structural and orchestration regressions cover every
      corrected boundary. This supersedes the completed Phase 1/2 descriptions of a combined
      credential-material wrapper, one required provider operation, and retained multiple-default
      tie-breaking while preserving that history. Live acceptance and final delivery remain open
      below.)
- [x] Incorporate correction round 4 without rewriting completed history: retain a scoped context
      assembler instead of a preparation-time context; give validation a fresh context with no
      target and give runup a fresh op-start context containing exactly the current admin or agent
      target while materialization gets a separate fresh context with no target; add provider-owned,
      read-only `gh`/`az` readiness checks that warn without blocking helper installation; and make
      the runup policy skip both secret and CLI checks. (2026-08-30: this supersedes the completed
      Phase 1/2 current-CLI-no-runup ruling. Focused tests cover the three distinct contexts, both
      target roles, missing/unhealthy/ready CLIs, output suppression, nonfatal helper retention, and
      disabled runup. Live acceptance and final delivery remain open below.)
- [x] Apply the bounded round-4 review fix: limit GitHub readiness to the active account with
      `gh auth status --active`, sharpen missing/unhealthy CLI recovery guidance without blocking
      helper installation, and collapse overlapping readiness tests to the ten behavioral cases that
      cover provider outcomes, transport suppression, nonfatal retention, and policy skip.
- [x] Run focused and full Python, static, lint, locked-SDD, Rulesync, website, installed-wheel, and
      live integration gates.
- [x] Obtain private `agentworks-reviewer`, Muntz, and generic correctness/security reviews; resolve
      material findings and rerun affected gates.
- [x] Hand off a complete ready implementation PR with exact-head evidence and monitor published
      review/testing under the authorized process.
- [x] Before the final implementation PR merges, create `locked.md` summarizing the final contract,
      promoted permanent homes, live evidence, and any deliberate deviations.

### Phase 6 Definition of Done

- Permanent documentation is sufficient after this SDD is deleted.
- Review and live testing find no unresolved material correctness, security, migration, or
  complexity issue.
- Current state matches the FRD/HLA/LLD, every completed checkbox is truthful, and the SDD locks
  only after implementation lands.
