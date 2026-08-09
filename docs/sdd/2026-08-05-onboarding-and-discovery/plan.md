# Plan: Onboarding, Discovery, and Management

- Status: Active, Phase 2
- FRD: `docs/sdd/2026-08-05-onboarding-and-discovery/frd.md`
- HLA: `docs/sdd/2026-08-05-onboarding-and-discovery/hla.md`

## Working rules

- The lead owns this plan and never edits the saga ledger or another effort's artifacts.
- Every implementation step receives an `agentworks-dev` implementation pass and an
  `agentworks-reviewer` review before its checkbox is completed.
- Run `./scripts/lint-files.sh --fix` and check its exit code before every commit.
- CLI changes include completions and permanent docs in the same commit.
- Main is authoritative. Wave 2 branch content informs coordination but is not an implementation
  dependency.
- After the draft artifact PR, each implementation phase is an intentionally separate ready-to-merge
  PR because it is independently usable and keeps main green. Later phases build only on merged
  predecessors.
- Completed checkboxes are immutable.

## Phase 0: pre-implementation artifacts and coordination

- [x] Topic-content contract completed as the first design deliverable, reviewed, and committed as
      the sole file in PR #420 for delivery to wave 2's directory on `main`.
- [x] PR #420 merged so the topic-content contract message is delivered to wave 2 through `main`.
- [x] `prior-art-research.md` covers every source named by R13, ties findings to design decisions,
      and records rejected inferences and source quality.
- [x] `hla.md` resolves every FRD-assigned question: onboarding state, agent mode, JSON contract,
      universal contributions, safe template vocabulary, taxonomy, multi-topic behavior, bootstrap
      compatibility/parity, and feedback.
- [ ] Wave 2 confirms or raises concerns with the early contract before building plan section 2.8;
      the resolved outcome is incorporated into the HLA without depending on provisional branch
      code.
- [x] HLA and plan reviewed by `agentworks-reviewer`; every valid finding resolved.
- [x] Artifact files lint clean with vocabulary scoped in this SDD's `.cspell.json`.
- [x] Artifact-only commit pushed and draft PR opened for roadmap-lead pre-implementation review.
- [x] Draft PR feedback resolved and explicit approval received before implementation starts.

Definition of done: the draft PR makes every design choice needed for the conflict-free first slice,
identifies all wave 2 gates, and has no unresolved review finding.

## Phase 1: guide core and safe projection

- [x] `guide-contract-lld.md` pins Python records, validation errors, catalog build timing, package
      data layout, semantic block identity, the deny-by-construction `GuideView` API, and the inert
      onboarding action record (identifier, sanitized precondition, required inputs, consent,
      command, expected state, verification, and refusal alternative). Catalog construction is
      guide-scoped and fail-soft, so invalid contributed content cannot break unrelated commands or
      valid core topics.
- [x] The guide LLD inventories documented, unambiguous Claude Code and Codex environment
      signatures; pins explicit flag, signature, then TTY detection precedence; rejects general
      configuration and secret variables as signatures; and tests `--human` for piped output.
- [x] Immutable `TopicContribution`, typed anchors, and closed block records implemented with strict
      registration validation; unknown fields, duplicate slugs, broken links, placeholder syntax,
      and executable contributions rejected.
- [x] `GuideView` implemented over finalized registry and graph facts with no capability object,
      secret resolver, raw config, run target, mutation, or arbitrary traversal surface.
- [x] Tests prove rendering performs no probe, secret resolution, capability invocation, finalize,
      or mutation and rejects an expression-evaluation attempt from plugin content.
- [x] Broken-config fixtures prove authored content and the framed config error still render, every
      affected dynamic block reports unavailable, full guide requests attempt the normal registry
      build, and `GuideView` construction cannot prompt for a secret.
- [x] Following live acceptance, guide-scoped registry construction preserves normal declaration,
      publication, materialization, validation, finalization, and freezing while disabling host
      probes; probe-dependent readiness remains unavailable and unverifiable. This corrective item
      supersedes the preceding completed item's historical "normal registry build" wording. Ordinary
      command registry builds retain their existing readiness checks.
- [x] Core concept topics and initial kind topics colocated with their owning packages, including
      security disclosure, consent-first behavior, progressive onboarding, management, secrets, and
      troubleshooting. `concept-reporting-bugs` covers redacted reproduction, existing-issue search,
      the repository bug template, and explicit operator authorization before external submission;
      it does not solicit general feedback.
- [x] `concept-onboarding` assessment derives done, not-ready, disabled, and unverifiable status
      only from registry rows, finalized graph verdicts and relationships, and stored instance rows,
      with no raw-config reach-around, doctor execution, or persistent onboarding ledger.
- [x] Guided and replayable modes consume the same ordered action records; fixture scenarios prove
      equal registry, graph, stored-row, and explicit verification outcomes for equal inputs,
      including equal `unverifiable` outcomes after refusal.
- [x] Final review correction exposes strict repeatable target-scoped evidence on the guide CLI so
      caller-owned replay logs can produce verified no-op and refusal-equivalent reruns without an
      Agentworks onboarding ledger.
- [x] Verification-surface inventory is rechecked against HEAD in the guide LLD: secret describe and
      doctor prediction, doctor and readiness tool checks, and lifecycle-only SSH checks are marked
      existing; actual secret proof and a non-mutating named-VM connection check are marked gaps.
- [x] A named-secret verification operation resolves through the normal boundary and reports only
      success or framed failure, never returns or emits the value, and never invokes an interactive
      backend without explicit consent.
- [x] A non-mutating named-VM connection verification operation uses the standard transport and
      reports success or framed failure without repair, rekey, or implicit power-state change.
- [x] `agw guide [topic ...]`, `--agent/--human`, and `--names-only` implemented with atomic
      multi-topic validation, exact lookup, deterministic ordering, and markdown-only output.
- [x] Dynamic topic completion implemented and tested for Bash, Zsh, and PowerShell, including
      `concept-` discovery and resource/capability `kind/name` topics.
- [x] Golden-path acceptance is step-bounded in automation and timed manually on a clean machine;
      the evidence records time to first working session and every operator interaction.
- [x] Phase 1 acceptance records time to the first actionable guide plan and every interaction in
      that guide-only path. This corrective item supersedes the preceding completed item's premature
      "first working session" wording; the actual fresh-operator working-session acceptance requires
      the published bootstrap packages and remains in Phase 3.
- [x] A management coverage matrix proves topics cover creating and changing resources, adopting a
      capability, resolving upgrade deprecations, and consented doctor-driven troubleshooting.
- [x] Permanent CLI and contributor documentation shipped with the command. Sample config reviewed;
      either updated for a real new setting or recorded as unaffected in the commit handoff.
- [x] Canonical Rulesync sources gain an always-on guide-contribution rule plus matching
      `agentworks-dev` completeness and `agentworks-reviewer` drift checks; other roles are audited,
      shared Claude Code, Codex, and Copilot outputs regenerated, and
      `./scripts/rulesync-upgen.sh --check` passes.
- [x] Unit, integration, CLI, completion, packaging, typing, and lint gates pass.
- [x] Step reviewed by `agentworks-reviewer` and a fresh-eyes reviewer; valid findings resolved.
- [x] Always-green ready-to-merge implementation PR opened and roadmap-lead review requested.
- [x] Roadmap re-review corrective: restore sanitized SSH warning diagnostics; make verification
      exception sanitization fail closed for malformed plugin exceptions; audit every secret-bearing
      logger construction now that redactions are immutable; and document the CodeQL-recognized sink
      constraint without weakening runtime error propagation or descriptor cleanup.
- [x] Roadmap re-review corrective: prove the guide database is opened read-only with an actual
      rejected write and service-construction assertion; frame any escaped read-only database error.
- [x] Roadmap re-review corrective: extend probe, secret, capability, filesystem-write, and mutation
      denials across end-to-end `render_guide` on a live registry, recording this corrective item
      without altering the earlier checked no-power claim.
- [x] Roadmap re-review corrective: exercise taxonomy ownership, untrusted collision, and
      broken-link fixpoint behavior with fixtures that reach those gates; make trusted-core taxonomy
      mistakes fail CI while runtime catalog construction remains fail-soft, align the HLA and LLD
      with that split, and isolate invalid contributions from valid topics at runtime.
- [x] Roadmap re-review corrective: make authored-content wheel inclusion a normal CI assertion, not
      an integration-only test.
- [x] Roadmap re-review corrective: sanitize C0/C1 terminal controls at one render boundary for all
      authored and projected text; accept action tokens only through a closed literal allowlist that
      excludes shell syntax, expansion, globbing, comments, whitespace, and line breaks; reject or
      safely frame controls in evidence errors.
- [x] Roadmap re-review corrective: keep secret-sanitizer scope narrow and diagnostics useful while
      distrusting every backend-authored field; make verification result fields authoritative rather
      than hard-coded or unread; add malformed-exception adversarial coverage.
- [x] Roadmap re-review corrective: make view construction and exit status fail soft for missing
      plugin roots, missing resources, unsupported concept instance lists, and unrelated invalid
      topics; derive probe suppression from one shared policy until the descriptor owns it.
- [x] Roadmap re-review corrective: pin registry validation, materialization, deferred targets,
      finalized-state gating, empty inventory, no-topic live rendering, first-run config framing, VM
      verification CLI behavior, every block renderer, and unsupported-block refusal with meaningful
      non-vacuous tests.
- [x] Roadmap re-review corrective: record sample config as unaffected in a repository-visible
      handoff; add contributed-content size bounds; remove or clarify dead and implicit
      discriminators identified by review where doing so improves the forward contract; defer only
      organizational test consolidation that changes no behavior or proof.
- [x] Roadmap re-review follow-up: give renderer-owned framework headings a visible reserved label,
      reject that label in authored markdown without restricting ordinary authored headings, and
      prove a plugin cannot emit the exact literal marker in raw CLI Markdown; document that the
      marker is provenance syntax, not downstream presentation security or authorization.
- [x] Roadmap re-review follow-up: make the guide power-import boundary inspect Python files
      recursively so a future guide subpackage cannot escape the denial proof.

Definition of done: R2, R3, R4, R5, R9, R10, R12, R13, R14, and R15 work for static topics and
current registry/resource-derived content without any wave 2 surface.

### Release-gate adoption after wave 2

The 2026-08-07 remediation-posture ruling supersedes Phase 1's originally dependency-free merge
boundary. PR #428 addressed all wave-2-independent review findings first, then rebased after the
declarative-schema service contracts and their explicit authentication and placement variants merged
to `main`. It has no provisional branch dependency.

- [x] After wave 2 merges, add `concept-migration` as the exceptional resource-model remediation
      topic, distinct from ordinary upgrading, using authoritative live sample and field-reference
      service APIs rather than copied shapes or rendered CLI output.
- [x] Config-free `describable_targets`, `SchemaReference`, and `sample_text` adapters populate kind
      and capability-implementation topics from one source, including disabled implementations,
      exact contribution validation, completions, field references, and declarable samples.
- [x] Link migration from onboarding and management without duplicating its teaching; cover TOML to
      manifest rewrites, tagged capability configuration, strict validation changes, and the precise
      credential decision path: omission or explicit null inside a credential arm selects its
      default secret reference; Azure and AWS select ambient authentication through the defaulted
      `auth` union, Lima selects local placement through the defaulted `placement` union, and
      Proxmox has no no-secret mode. Written legacy authentication and placement fields receive
      their exact hard-error rewrites before cutover. Git credential token acquisition preserves
      omission and scalar shorthand, retires an outer explicit null with its exact stored-mode
      rewrite, and keeps omitted or null inner `secret` on the well-known default; no minted arm is
      taught before one exists.
- [x] Prove the topic remains available when operator config does not load, names exact live
      remediation surfaces, and verifies results through normal loading and doctor rather than a
      frozen migration oracle.
- [x] Every migration read, probe, and mutation crossing a consent boundary is a validated inert
      action record with exact scope, expected result, verification, and refusal behavior; backups
      and the complete pre-migration identity-and-origin inventory precede edits.
- [x] Reconcile schema rendering and migration teaching with the final PR #444 union surfaces,
      confirm PR #446's context-free validation boundary adds no guide-side filling, then rerun the
      full Phase 1 and CI gates before requesting roadmap-lead re-review.
- [x] Revalidate schema rendering and migration teaching after PR #455's structural-union and git
      credential token-acquisition contracts land, including the retained shorthand/default paths,
      exact outer-null rewrite, stored arm, and absence of a minted arm.
- [x] Round-3 performance follow-up: materialize global kind and implementation inventories only for
      concept views permitted to read them, replace snapshot list-membership deduplication with
      insertion-ordered identity maps, and pin bounded work and stable ordering structurally without
      redesigning `GuideView`, adding bulk hook APIs, or imposing wall-clock thresholds.

The release-gate adapter is specified in `wave2-guide-adapter-lld.md`. It binds existing schema
blocks and the migration workflow only; the broader registry-inventory scope remains in Phase 4.

## Phase 2: machine-readable operational output

- [x] `machine-output-lld.md` inventories every covered list/describe service, pins JSON v1 schemas,
      enum spellings, ordering, nullability, error behavior, and human-output compatibility
      fixtures.
- [x] Shared `--output human|json` option and v1 envelope serializer implemented without replacing
      the global output handler, adding a process-global output mode or renderer, or implying
      support on mutation commands; narrow request-scoped state controls presentation suppression
      and error styling only for covered JSON commands.
- [x] Resource list, kinds, and instance describe serialize their existing service fact records;
      human output remains byte-compatible.
- [x] VM, workspace, agent, session, console, and secret list/describe paths return fact records and
      gain JSON v1 while preserving human output and read-only behavior.
- [x] Doctor gains JSON v1 from `HealthReport`, emits a complete failing report, and preserves its
      current exit status semantics.
- [x] Guide action records direct the agent to consume covered list, describe, and doctor JSON at
      applicable verification steps; an end-to-end fixture parses and asserts each v1 document.
- [x] `--names-only` and JSON mutual exclusion, deterministic output, no ANSI bytes, stderr error
      routing, and schema-version compatibility covered by CLI tests.
- [x] JSON v1 documented as a permanent contract with examples and compatibility rules; command docs
      and completions updated in the same commits.
- [x] All focused and full gates pass; step reviewed by `agentworks-reviewer` and a fresh-eyes
      reviewer; valid findings resolved.
- [x] Always-green ready-to-merge PR opened and roadmap-lead review requested.
- [x] Close corrupted persisted operational JSON enum values with documented sentinels and preserve
      human-output compatibility.
- [x] Apply the operator scope correction: remove the database-copying and hostile-filesystem
      inspection subsystem, its unavailable-result protocol, tests, and documentation; retain only
      straightforward non-migrating doctor reads and the independently required JSON projections;
      then rerun project, fresh-eyes, full, and PR gates before requesting re-review.

Definition of done: R7 and AC4 hold across the named commands, with human and JSON renderers sharing
one fact source.

## Phase 3: README and cross-harness bootstraps

- [ ] `bootstrap-packaging-lld.md` pins the canonical source, generated Claude Code and Codex
      layouts, marketplace metadata, install commands, security-setting links, minimum CLI version,
      regeneration guard, README derivation or equivalence check, and clean-environment probe
      matrix.
- [ ] Canonical thin bootstrap contains installation, the complete R12 disclosure, strict harness
      posture, and `agw guide concept-onboarding --agent`, with no duplicated teaching content. The
      disclosure names the intended workstation, full file inspection and command execution under
      the harness account, separate explicit privilege elevation, and the strictest practical
      approval and visibility posture that preserves the required workstation access.
- [ ] Generator emits committed Claude Code and Codex plugin/marketplace wrappers from that source;
      CI requires regeneration to produce no diff.
- [ ] Repository README Getting Started leads with the R16 agent-addressed fenced block generated
      from the canonical source and retains a clear human installation path below it.
- [ ] Both packages install directly from GitHub in clean harness environments and reach the guide;
      minimum-version failure produces an actionable upgrade instruction.
- [ ] Both bootstrap packages drive the same guide action inventory through guided and
      non-interactive fixture runs, exercising consent boundaries, refusal handling, rerun no-op
      behavior, post-upgrade newly available capability reporting, and JSON v1 consumption.
- [ ] Every bootstrap fixture asserts the R12 disclosure is emitted before its first setup command,
      probe, verification command, or other action, and pins every workstation-access and privilege
      boundary from the canonical source.
- [ ] Permanent installation and security documentation ships with the packages.
- [ ] Packaging, generation, lint, and end-to-end gates pass; step reviewed by `agentworks-reviewer`
      and a fresh-eyes reviewer; valid findings resolved.
- [ ] Always-green ready-to-merge PR opened and saga-lead review requested.

Definition of done: R1, R11, R12, R16, AC1, AC3, AC7, AC8, and AC10 hold for both harnesses and the
zero-plugin README path.

## Phase 4: registry inventory and specific-resource projection

The Phase 1 release gate now owns schema-derived kind and implementation pages because the 0.14
migration topic requires them. This phase retains only runtime registry inventory and
specific-resource depth that does not block the first release.

- [ ] After the descriptor inventory merges, registry inventory renders capability kinds and
      implementations, including enablement/readiness, without a hand-maintained adapter table.
- [ ] Specific-resource topics delegate to the same service fact source as instance describe.
- [ ] Adding a registered implementation or resource changes the guide inventory with no topic
      switchboard edit, pinned by fixture-plugin tests.
- [ ] Full registry integration gates pass; step reviewed by `agentworks-reviewer` and a fresh-eyes
      reviewer; valid findings resolved.
- [ ] Always-green ready-to-merge PR opened and saga-lead review requested.

Definition of done: R8 and AC5 project runtime registry and specific-resource facts without a
hand-maintained switchboard.

## Phase 5: acceptance, promotion, and closeout

- [ ] Fresh-operator acceptance matrix run for Claude Code, Codex, and README-only paths with
      evidence for all 13 FRD acceptance criteria.
- [ ] No telemetry, general-feedback prompt, or non-bug manual-relay request ships; acceptance runs
      retain their own timing and unexplained-intervention evidence as test artifacts.
- [ ] `concept-reporting-bugs` is tested to redact sensitive evidence, point at the repository bug
      template, require explicit operator authorization for external submission, and never
      auto-submit an issue.
- [ ] All load-bearing guide, JSON, contribution, packaging, and security contracts promoted to
      permanent docs so deleting this SDD would not remove operating knowledge.
- [ ] `./scripts/lint-files.sh --fix`, focused tests, full test suite, typing, completion
      generation, package build, and locked-SDD checks pass.
- [ ] Final `agentworks-reviewer` and fresh-eyes diff reviews complete with all valid findings
      resolved; Copilot comments on ready PRs triaged.
- [ ] `locked.md` created with final state and date, while recognizing the lock takes effect only
      after merge to `main`.
- [ ] Final ready-to-merge PR reviewed by saga lead and handed off with commit and test evidence.

Definition of done: every FRD requirement and acceptance criterion is evidenced, permanent docs are
self-sufficient, and the effort is ready to merge and lock.
