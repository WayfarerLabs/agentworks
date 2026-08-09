# Plan: Secret Sources

- Status: Draft for pre-implementation review
- FRD: [frd.md](./frd.md)
- Architecture: [hla.md](./hla.md)
- Migration: [migration-strategy.md](./migration-strategy.md)

## How to work this plan

This is a large contract migration delivered on one ordinary `feat/secret-sources` branch and one
feature PR. The PR remains draft while the phases below are incomplete. Phase commits stay green and
reviewable, but no intermediate phase is proposed as a separately mergeable product.

The lead owns this plan and its cross-phase invariants. Each LLD and implementation phase is
delegated to an `agentworks-dev` subagent in an isolated detached worktree. The lead integrates each
phase commit promptly onto the feature branch; no additional remote branch or PR is created. Every
code phase is reviewed by an `agentworks-reviewer` at least as capable as its developer; the ready
feature PR also receives the normal fresh-eyes review. Valid findings return to the implementing
developer until the phase is clean.

Completed checkboxes are immutable. A checkbox is checked only in the commit that contains, or
follows, the work that makes it true. Permanent docs and completions land with the behavior they
describe, never as final polish.

## Global definitions of done

- **Green:** from `cli/`, `uv run ruff check agentworks/ tests/`,
  `uv run ruff format --check agentworks/ tests/`, `uv run mypy agentworks/ tests/`, and
  `uv run pytest tests/ -v -m 'not integration'` pass; from the repository root,
  `./scripts/lint-files.sh`, `./scripts/rulesync-upgen.sh --check`, and
  `./scripts/check-locked-sdds.sh` pass.
- **Source-only:** settings references, graph edges, mapping validation, attemptability, inspection,
  and runtime all resolve through `secret-source`; no fallback branch interprets a name as a
  backend.
- **Simple-case parity:** absent settings, explicit `env-var`/`prompt` chains, env-name derivation,
  prompt opt-out, precedence, deduplication, partial inspection, fail-before-prompt behavior, and
  operation-scoped caching remain pinned.
- **No leaks:** no resolved value appears in an outcome, exception, log, doctor/describe/verify
  record, human rendering, JSON rendering, object representation, graph row, or persisted config.
- **Lockstep teaching:** source-model changes update samples, capability/secrets READMEs, resource
  guide, upgrade guide, and relevant ADRs in their owning phase. The new command updates command
  reference and Bash, Zsh, and PowerShell completions in its phase.
- **No SDD dependency:** permanent code and docs stand alone and contain no link to this feature
  directory.

## Phase 0: pre-implementation artifacts

- [x] FRD ruling confirmed: direct configured-backend references, including OnePassword, break in
      0.14; implied `env-var` and `prompt` sources keep current spelling.
- [x] Prior-art research and HLA authored, repository gates passed, independent architecture review
      clean, and roadmap review converged with every requested fold incorporated.
- [x] Design review converged on PR #453; the operator ruled that planning and implementation stay
      on the same ordinary branch and PR, which remains draft with auto-merge disabled until the
      whole feature is ready.
- [x] Migration strategy and this plan pass repository gates and independent `agentworks-reviewer`
      review with every valid finding resolved.
- [ ] Roadmap lead reviews the migration strategy and plan; Phase 0 closes when review converges,
      while the feature PR remains draft for implementation.

**Definition of done:** design intent, migration boundary, phase sequence, and objective gates are
stable before implementation delegation begins.

## Source model and typed runtime core

### Phase 1: source and backend contract LLD

- [x] Delegate `source-contract-lld.md`. It pins:
  - final non-client `SecretBackend` abstract capability and registration conformance, including
    runtime rejection of `preflight` or `runup` overrides, while assigning the exact client-factory
    signature to Phase 2 before any implementation begins;
  - `config_model` versus `mapping_model`, descriptor `mapping_schema`/`mapping_host`, and the one
    shared source-to-backend selector used by validation and extraction;
  - `SecretSourceDecl`, its tagged `backend` block, readiness derivation, built-in publication
    order, override provenance, graph dependencies, and exact direct-backend error framing;
  - the class-registry and module-relocation sequence, including which package exports survive;
  - JSON Schema's union-of-all-mapping-models limitation and exact runtime narrowing.
- [x] Lead reviews the LLD against the HLA and migration strategy; `agentworks-reviewer` finds no
      remaining valid issue.

**Definition of done:** the source contract has one owner for every selection and source/backend
identity decision, and the remaining client-lifecycle contract is explicitly assigned to Phase 2.

### Phase 2: resolution lifecycle LLD and typed core

- [x] Delegate `resolution-lifecycle-lld.md`. It pins:
  - frozen `SecretLookupRequest`, caller-owned `InteractionBroker`, `ActiveSource`, the final
    `SecretBackend.create_client` signature, context-manager, and cleanup protocols;
  - monotonic budget ownership and remaining-time behavior across factory, entry, `prepare`,
    `resolve`, and exit, with human prompt wait excluded;
  - value-free outcome/detail enums, private redacted `ResolutionBatch`, soft/hard miss semantics,
    batch-failure attribution, and complete-or-raise behavior;
  - how the current dict-returning operation callers cross the temporary adapter until Phase 7.
- [x] Lead reviews the LLD; `agentworks-reviewer` finds no remaining valid issue.

**Definition of done:** lifecycle, timeout, error attribution, value authority, and the client
factory are precise before any backend contract or implementation changes.

### Phase 3: capability contract, registry, and relocation scaffold

- [x] Add the dual backend model contract and descriptor map-host records, with registration-time
      conformance for model type/constructibility, JSON-native mapping input annotations, forbidden
      secret references in source config, fixed no-op capability lifecycle, and class-by-name
      storage.
- [x] Move capability-owned modules with `git mv` under `agentworks.capabilities.secret_backend`;
      repoint the descriptor, plugin adapter, registration snapshot/restore, graph publication, and
      imports; remove the `CONSTRUCTED_SINGLETON` policy and constructed adapter branch.
- [x] Keep production behavior green behind temporary internal call adapters within the feature
      branch; no new operator surface or unused public abstraction is merged at this phase boundary.
- [x] Tests cover every conformance rejection, class identity on registry/graph rows, plugin
      registration atomicity, and absence of the old singleton branch.
- [x] Update the capability author README for the contract that is now true; run Green and phase
      review.

**Definition of done:** backend code is an ordinary class-registered capability under its permanent
package; the descriptor has no constructed-instance exception.

### Phase 4: declarable sources and schemas

- [x] Add `SecretSourceDecl` and its resource kind to discovery, manifest decoding, samples,
      schema-set membership, reference metadata, kind-name completion, and describe-kind.
- [x] Add the domain publisher for `env-var` and `prompt` before operator manifests, using normal
      built-in origins and `builtin_override="allow"`; tests pin discovery and operator override
      provenance.
- [x] Extend shared spec projection and emission to consume `mapping_host`: property names reference
      `secret-source`, every value uses the union of registered mapping models and the host's
      declared opt-out arm, and fixture-plugin tests prove the mechanism is descriptor-derived
      rather than secret-specific.
- [x] Broaden the raw secret mapping carrier to all JSON-compatible values while reserving `false`
      as opt-out; tests prove scalar, mapping, collection, and `true` plugin mapping models reach
      exact backend-specific narrowing.
- [x] Add source validation and extraction helpers that use the same backend selector without yet
      repointing production settings, graph, or runtime consumers. Add the descriptor-derived
      map-key existence helper, constrained to `USES` references targeting error-policy kinds, but
      do not invoke it from Registry finalize in this phase.
- [x] Keep the feature PR draft: this additive phase is a review checkpoint on the feature branch,
      not a separately mergeable product. Run Green and phase review.

**Definition of done:** the final source model, built-in publication, and descriptor-derived schema
machinery exist on the feature branch; the atomic production cutover remains owned by Phase 5.

### Phase 5: bounded clients and typed resolution core

- [x] Implement one lazy client per attempted source turn. No unused later source is constructed;
      setup is batched, first resolved source wins, and every context closes before the next source.
- [x] Implement env-var, prompt, and OnePassword clients. Prompt uses only the caller-owned broker;
      OnePassword applies the remaining timeout to subprocess calls and translates timeout,
      authentication/connectivity, hard mapping, and external failures once.
- [x] Implement stable outcome categories `resolved`, `unavailable`, `refused-interaction`,
      `timeout`, and `resolution-failure`, with safe detail codes and remediation.
- [x] Implement private `ResolutionBatch` value storage, redacted `repr`, no serializer, and a
      complete-or-raise adapter used by existing operation callers until Phase 7.
- [x] Land the client and typed-batch machinery behind current production behavior and prove it with
      focused tests before changing any settings, graph, mapping, or runtime reference to source
      names.
- [x] In one atomic cutover, repoint `[secret_config].backends`, `SecretDecl.dependencies`, mapping
      validation/extraction, graph candidate edges, chain validation, inspection, and runtime chain
      construction from backend names to source names. Replace the finalize backend-instance tuple
      with the generic read-only capability-class projection. Activate collection of validation-only
      map-key references as each host row enters the registry build or fixed-point walk, resolving
      them in the corresponding existing resolve stage so initially published and later materialized
      rows follow the same source-first rule without changing error precedence.
- [x] Only after source lookup misses, if the unknown name exactly matches a backend, hard-error
      with the exact config- or manifest-specific source rewrite. Pin that a same-name synthesized
      or operator-declared source wins; every explicit mapping key is checked even when `false`
      suppresses candidate-edge emission. Do not add a deprecation producer, manifest carrier,
      compatibility source, or legacy parser.
- [x] OnePassword source config owns `account` and a positive external-operation `timeout`; its
      permanent mapping is an `op://` reference. Update plugin fixtures and the 0.14 upgrade guide
      in this phase.
- [x] Preserve deduplication, precedence, soft fallthrough, hard-miss halt, readiness skipping,
      control-character rejection, batch attribution, and fail-before-prompt doom behavior.
- [x] Sentinel tests prove values cannot appear in outcomes, representations, warnings, logs,
      errors, or renderer inputs. Lifecycle tests prove construction/prepare/resolve/close order on
      success, soft miss, hard failure, timeout, and interruption.
- [x] Golden tests prove the implied `env-var`/`prompt` simple case is behavior-identical, including
      absent settings, explicit chains, default mappings, `false` opt-out, and operator overrides.
- [x] Update sample config, `cli/README.md` source-model sections, resource guide, secrets README,
      ADR 0016, and ADR 0023 for behavior made true; run Green, full phase review, and a fresh-eyes
      correctness/security review.

**Definition of done:** the feature branch has a complete source architecture with final bounded
clients and typed core; only the deliberately narrow operation-result adapter remains for Phase 7.

## Consumer migration and operator surfaces

### Phase 6: operator-surface LLD

- [x] Delegate `operator-surfaces-lld.md`. It pins outcome-to-error mapping, operation resolver
      adoption, preview/doctor/describe boundaries, verify command syntax and exits, renderer
      records, and completion/doc ownership.
- [x] Explicitly inventory every remaining `active_backends`, `resolve_secrets`, string-error
      out-parameter, and old capability-package import; the LLD assigns each to migrate or delete.
- [x] Lead and `agentworks-reviewer` close every valid LLD finding.

**Definition of done:** every consumer has one destination and deletion of the temporary adapter is
mechanically provable.

### Phase 7: migrate operation and inspection consumers

- [ ] Migrate the operation-scoped `Resolver`, orchestration union/scoping, gate seeding, and
      command boundaries to `ResolutionBatch`; preserve one resolve boundary, late-registration
      errors, cached operation lifetime, and per-node secret scoping.
- [ ] Replace preview's boolean answer and inspection's partial string errors with value-free typed
      records. Describe remains side-effect-free and doctor remains non-probing and non-interactive.
- [ ] Migrate the existing one-name `secret verify` service from its quiet dictionary wrapper and
      parallel proof record to shared `ResolutionOutcome` rows. The pre-release one-name command may
      remain as the Phase 7 presentation checkpoint, but no legacy resolution adapter survives it.
- [ ] Ordinary commands derive interaction permission from stdin TTY plus global
      `--non-interactive`; global refusal wins. Preserve fail-before-prompt semantics exactly.
- [ ] Remove the dict-returning compatibility adapter, old `ActiveBackend` vocabulary, error
      out-parameter, and dead imports. Guard tests reject new consumers of the retired paths.
- [ ] Run focused resolver/orchestration/doctor/inspection suites, Green, and phase review.

**Definition of done:** every internal consumer uses source-based typed results, and the temporary
operation adapter is gone.

### Phase 8: complete `agw secret verify`

- [ ] Reshape the existing command to `agw secret verify NAME...` as the final explicit read
      surface. Replace the pre-release `--allow-interactive` spelling with `--allow-interaction`
      without an alias. It defaults to interaction refusal; the opt-in is rejected when global
      `--non-interactive` is set.
- [ ] Human rendering reports one value-free row per requested secret with category, source, safe
      identifier, detail, and remediation. Exit is nonzero if any secret is not resolved.
- [ ] Use the shared outcome records for future JSON compatibility; do not add a second result model
      or expose `ResolutionBatch` to a renderer.
- [ ] Test resolved, unavailable, refused-interaction, timeout, hard failure, duplicate names, mixed
      batches, disabled/not-ready sources, interaction precedence, exits, and sentinel
      non-disclosure.
- [ ] Update the secrets CLI README and root CLI command reference. Repoint `secret verify` in the
      shared dynamic-completer specification from the singular parameter to variadic secret names,
      regenerate Bash, Zsh, and PowerShell completions, and pin command-name plus all-shell variadic
      completion behavior in the same commit.
- [ ] Run Green and phase review.

**Definition of done:** AC5 is observable through a safe explicit command, and every shell teaches
the new command.

### Phase 9: acceptance, permanent records, and closeout

- [ ] Re-run the source-only, simple-case, and no-leak global definitions against the full suite;
      mutation-check the descriptor-derived map-host guard and retired-path guards.
- [ ] Update remaining permanent capability/secrets docs and changelog/release-note surfaces for the
      exact behavior at HEAD. Add guide topic prose through the universal contract if present;
      otherwise record the approved deferral in this effort's `locked.md`, notify the onboarding
      lead/operator through the sanctioned cross-effort channel for Phase 4, and do not edit the
      onboarding SDD or add a temporary adapter.
- [ ] Exercise the real CLI in an isolated configuration: implied env-var resolution, prompt
      refusal, `secret verify`, unknown direct `onepassword` remediation, and a declared OnePassword
      source without printing any secret value.
- [ ] Run Green on every supported Python version through CI. Complete `agentworks-reviewer`,
      roadmap-lead, and fresh-eyes review (the fallback if Copilot is unavailable) with every valid
      finding resolved.
- [ ] Create `locked.md` only in the final feature commit, summarizing shipped behavior, the 0.14
      break, guide-topic status, PRs, gates, and any honest residual work.
- [ ] Mark PR #453 ready, request Copilot review, and resolve every valid finding before merge;
      notify the operator of any roadmap ledger inconsistency without editing the roadmap SDD.

**Definition of done:** every FRD acceptance criterion is demonstrated, permanent documentation is
truthful without this SDD, the feature directory locks with the final code, and no migration bridge
remains.
