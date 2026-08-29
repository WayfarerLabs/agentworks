# Implementation Plan: Runtime Git Identities

<!-- cspell:ignore sdds -->

- Status: Design review
- Date: 2026-08-28
- Requirements: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)
- Detailed design: [credential-helper-lld.md](./credential-helper-lld.md)
- Migration: [migration-strategy.md](./migration-strategy.md)
- Research: [prior-art-research.md](./prior-art-research.md)
- Design baseline: `4c47f9c70a58f62f3a2e366f2870013d5fa032b8`
- Delivery vehicle: design-only draft PR first; exactly one implementation PR

## Delivery Rules

- The design PR contains only this SDD directory. It stays draft and carries
  `sdd:runtime-git-identities` plus the author-owned `review-requested` label. It has no merge
  intent while design review is active.
- The operator authorized up to four published-feedback/fix iterations while findings are
  converging. Each iteration critically evaluates the complete batch, removes `review-requested`
  before edits, pushes one coherent corrected checkpoint, comments with dispositions/evidence, and
  reapplies `review-requested`. A requirements change, divergent redesign, or fifth needed round
  stops for operator direction.
- Private artifact review uses `agentworks-reviewer` and Muntz before the first public handoff.
  Document-only work does not add a generic code-correctness lane.
- Review convergence recommends promotion but does not authorize it. The operator decides when the
  design PR becomes ready and merges.
- Implementation begins from the merged design on current `main` and lands in one feature PR so the
  provider contract, callers, reconciliation, cleanup, and live behavior cannot land in a misleading
  half-state. This effort does not define a safe implementation split.
- No implementation checkbox is checked by the design-only PR. Completed checkboxes remain immutable
  after merge.
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
- [ ] Collect the requested public artifact perspectives and process at most four authorized
      converging iterations.
- [ ] Receive operator direction to promote and merge the design checkpoint before implementation.

### Phase 0 Definition of Done

- Every requirement maps to architecture, detailed design, migration, implementation work, and an
  objective proof.
- Secret-backed validation and runtime CLI acquisition have distinct, unambiguous lifecycle rules.
- The provider/core ownership boundary and total empty-state reconciliation are explicit.
- The removed direct-add path has no replacement writer or installed-state compatibility seam.
- Azure Git authentication and Git config precedence are named implementation proofs, not assumed
  facts.
- Review reports no unresolved material design or complexity finding.

## Requirement Traceability

| Requirement | Design authority                     | Planned proof                                      |
| ----------- | ------------------------------------ | -------------------------------------------------- |
| R1, R2      | HLA models; helper LLD contract      | phase 1 contract/config/graph tests                |
| R3          | HLA lifecycle; helper LLD runup      | phases 1 and 2 static-token policy tests           |
| R4, R5      | HLA runtime; helper LLD source rules | phases 2, 3, and 5 runtime/locking/live tests      |
| R6          | HLA builder; helper LLD dispatcher   | phases 2 and 4 selection/collision tests           |
| R7, R8      | HLA reconciler/config boundary       | phases 3 and 4 idempotence/ownership tests         |
| R9          | HLA one-write-path decision          | phase 4 removed-command and residual tests         |
| R10         | HLA security; helper LLD containment | phases 2 through 6 adversarial/value scans         |
| R11         | HLA permanent homes                  | phase 6 collateral and generation gates            |
| R12         | FRD non-goals; HLA identity details  | phase 6 residual sweep and future-feature boundary |

## Phase 1: Provider Contract and Configuration

- [ ] Add provider-owned closed acquisition unions: GitHub `secret | gh-cli`, Azure DevOps
      `secret | az-cli`; preserve secret omission/scalar/full forms.
- [ ] Prove only `secret` emits a secret reference and CLI arms reject secret fields and
      cross-provider modes.
- [ ] Add frozen selector/source/helper-definition types with safe construction and value-safe
      representations.
- [ ] Cut the capability descriptor and all in-tree providers atomically to contract version 3 and
      the single `helper_definition` operation.
- [ ] Delete v2 `helper_entry`, `credential_lines`, static-store username routing, and universal
      `secret_name` assumptions.
- [ ] Retain provider-specific static-token verification and `defaults.runup_git_credentials`; make
      CLI-backed runup a structural no-op.
- [ ] Update schema/resource samples and provider contract tests without asserting authored prose.

### Phase 1 Definition of Done

- Every provider config validates through the framework and produces exactly one inert helper
  definition.
- Graph edges and runup behavior follow acquisition shape without caller special cases.
- No contract-version-2 implementation or adapter remains.

## Phase 2: Runtime Helpers and State Builder

- [ ] Implement the static-token source using a core-owned mode-0600 store and line-safety checks at
      final materialization.
- [ ] Implement one core bounded runtime-command executor using the guaranteed coreutils `timeout`;
      feed it the fixed GitHub and Azure provider recipes and value-safe diagnostics.
- [ ] Pin GitHub to `gh auth token --hostname github.com` and Azure to the exact
      resource-ID/query/TSV command plus organization-username/token-password response.
- [ ] Build the deterministic dispatcher, host-specific Git include, generation layout, and atomic
      `current` symlink activation from helper definitions.
- [ ] Preserve exact-repository, owner/organization, and default selection; reject scope collisions
      and unsafe provider output.
- [ ] Add fake CLI/protocol tests for success, missing executable, nonzero command, timeout, empty,
      multiline/control output, noisy stderr, unsupported operations, and no-match cases.
- [ ] Prove no token appears in dispatcher, include, representations, errors, logs, or test
      artifacts.

### Phase 2 Definition of Done

- The complete desired per-user state can be built before any target mutation.
- Runtime helpers acquire credentials only on a matching Git `get` and fail safely otherwise.
- Selection is independent of static-store usernames.

## Phase 3: Atomic Per-user Reconciliation

- [ ] Implement one transport-neutral reconciler that stages private generations, uses one stable
      shared/exclusive lock, atomically activates `current`, cleans inactive state, and supports an
      empty desired state.
- [ ] Register exactly one Agentworks include and remove it when empty; remove only exact legacy/new
      Agentworks config values.
- [ ] Prove host-scoped helper reset and `useHttpPath` behavior against Debian Bookworm's Git 2.39
      with unrelated operator helpers/includes present.
- [ ] Delete the exact Agentworks-owned legacy `~/.git-credentials` path without reading its secret
      content; preserve unrelated Git config and paths.
- [ ] Add idempotence and transition tests for fresh, same, add, remove, scope change, mode switch,
      rejected-last-static, mixed, malformed legacy, staged failure, and empty states.
- [ ] Prove concurrent helper/swap/empty cleanup never mixes generations or deletes files under an
      in-flight helper.
- [ ] Prove both previous/current launcher-dispatcher ABI pairings and a fault between their atomic
      replacements retain one executable generation contract.
- [ ] Fault every mixed legacy/new mutation boundary and prove stale credentials are never
      reactivated; prove helper/reconciler lock timeouts and child-descriptor closure.

### Phase 3 Definition of Done

- One call converges the Agentworks-owned Git state exactly without altering unrelated Git config.
- A failed installation exposes no partial new credential state.
- Zero desired credentials removes every provably Agentworks-owned credential/routing artifact; only
  the inert stable lock may remain. An indistinguishable generic `store` helper has no managed
  credential file to serve.

## Phase 4: Admin and Agent Cutover

- [ ] Make admin initialization/reinitialization invoke the reconciler unconditionally before
      private Git-backed setup.
- [ ] Make agent creation/reinitialization use the same builder/reconciler unconditionally and
      delete its duplicate writer/config logic.
- [ ] Change composition roots to require resolved values only for static sources and preserve
      static resolution failures plus rejected-token skip/partial behavior.
- [ ] Delete `vm add-git-credential`, its manager path, tests, completion/help/docs entries, and
      static-store append/fallback behavior; teach the declarative template plus reinit path.
- [ ] Delete conditional provider gates, duplicate constants/comments, installed-state speculation,
      and all stale-state paths.

### Phase 4 Definition of Done

- Admin and agent flows install the same format through one reconciler.
- Full user initialization is the sole declarative authority and always runs, including empty state.
- No imperative material writer or global `--replace-all credential.helper` remains.

## Phase 5: Live Integration and Runtime Proof

- [ ] Load `integration-testing` and `agw-test-env`; inventory exact authorized GitHub and Azure
      identities, repositories, mutation budget, cleanup, and sensitive-output controls.
- [ ] Prove a real `gh` identity supports HTTPS clone/fetch and a reversible write through the
      generated helper; exercise missing/unauthed state in a disposable user home.
- [ ] Prove a real `az` service-principal identity obtains an Azure DevOps-audience token and
      supports HTTPS clone/fetch and a reversible write through the generated helper.
- [ ] Verify Azure organization membership/permissions and wrong-identity failures are distinguished
      from helper/CLI failures without leaking provider output.
- [ ] Prove per-`get` reacquisition with fake CLIs returning different values on consecutive calls;
      do not manipulate live login or cache state owned by future authentication features.
- [ ] Stop for design revision if Azure Git or host-scoped Git config behavior disproves the LLD; do
      not add a Git upgrade, GCM dependency, or alternate transport implicitly.

### Phase 5 Definition of Done

- The shipped helper works for real Git, not only direct CLI token commands or REST probes.
- Runtime failure messages are actionable and value-safe.
- Tester cleanup leaves no repository, branch/tag, identity, token, or VM residue beyond the
  explicitly retained evidence policy.

## Phase 6: Permanent Collateral, Review, and Delivery

- [ ] Rewrite `capabilities/git_credential/README.md` around scoped helper definitions, static and
      runtime sources, secret-only runup, reconciliation ownership, and provider-authoring rules.
- [ ] Update capability/resource prose, schemas, examples, sample config/manifest disposition,
      command reference, guide concepts, CLI README, upgrade guide, and plugin documentation.
- [ ] Document the exact `gh`/`az` runtime commands, active-identity prerequisite, Azure
      organization permission prerequisite, and recovery path.
- [ ] Add no prose-policing tests; retain structural schema/generation/command/helper/collateral
      validation only.
- [ ] Run focused and full Python, static, lint, locked-SDD, Rulesync, website, installed-wheel, and
      live integration gates.
- [ ] Obtain private `agentworks-reviewer`, Muntz, and generic correctness/security reviews; resolve
      material findings and rerun affected gates.
- [ ] Hand off a complete ready implementation PR with exact-head evidence and monitor published
      review/testing under the authorized process.
- [ ] Before the final implementation PR merges, create `locked.md` summarizing the final contract,
      promoted permanent homes, live evidence, and any deliberate deviations.

### Phase 6 Definition of Done

- Permanent documentation is sufficient after this SDD is deleted.
- Review and live testing find no unresolved material correctness, security, migration, or
  complexity issue.
- Current state matches the FRD/HLA/LLD, every completed checkbox is truthful, and the SDD locks
  only after implementation lands.
