# Harness Scope Framework: Implementation Plan

- Status: Implementation authorized by the operator after design review on 2026-09-07
- Governing requirements: [FRD](frd.md)
- Approved architecture: [HLA](hla.md)
- Implementation baseline: `3641ea8c` on main, combined with the approved design

## Delivery and ownership

The operator directed the approved FRD/HLA and implementation to share PR 761. Continue on
`sdd/harness-scope-framework-hla`; PR 780 is superseded, with its review reports retained and linked
from the consolidated checkpoint. Config exposure, execution, receipts and migration form one
working increment. The large diff receives separate configuration, lifecycle, native setup and
migration review units before the combined handoff. The implementation is intended to merge. A
complete, green handoff is marked ready to request independent integration testing and review; live
acceptance is not a prerequisite for that signal. `review-requested` is reserved for unfinished
draft checkpoints. The earlier design-only delivery intent no longer applies.

The operator owns requirements and has approved the design and implementation. The lead owns this
plan, architecture, integration, and implementation disposition. Delegates own only their assigned
files and return design decisions to the lead. Shared saga corrections and the intended successor
artifact SDD remain saga-owned. This effort delivers no artifact declarations, routing protocol,
feature machinery, session identity work, or workspace plugin installation.

Checked boxes require evidence from committed work and observed validation. Design approval does not
mark implementation acceptance complete. Permanent collateral travels with the behavior it explains.
Do not introduce a lockfile until the entire approved effort is complete.

## Design detail before dependent implementation

- [x] Consolidate successor direction in the FRD, record operator approval, review the correction,
      and publish the resulting approved design checkpoint.
- [x] Complete the config-binding design below with the concrete consumer walk and migration tests.
- [x] Author `native-setup-lld.md` for typed invocations, checkpoint records, native command plans,
      operation serialization, partial failure, and readiness; review it before wiring lifecycle
      calls.
- [x] Author `migration-strategy.md` for declarative fields, stored overlays, explicit session
      selection, native ownership conflicts, and operator remediation.
- [x] Record native command research and local fixture observations in `prior-art-research.md`.
      Verify both CLI formats and destructive-command scope before adopting their output as
      evidence.

## Config and schema foundation

Extend the existing `config_for` hook with the fixed facet selector. Ordinary capability config
continues to select one model without facet obligations. The harness kind enumerates four answers;
selection, validation, secret/reference extraction, merging, constructor binding, and reference
output use the same cached selected model. A no-config answer permits a name-only activation and
rejects extra keys. Harness consumers must supply their facet; they cannot default a multi-facet
answer to session.

Extend the existing hosting descriptor into a sequence of concrete surfaces, including facet and
singular/list cardinality. Walk each setup block with its own index and origin. Preserve ordinary
vm-platform, secret-backend and git-credential-provider behavior, including mapping models.

- [x] Implement facet selection and declaration-sensitive caches, including registration validation
      of malformed hooks and model answers and coherent constructor secret extraction.
- [x] Propagate host facet/cardinality through schema emission, reference/explain output, structural
      extraction, graph edges, defaults, merge, manifest decode, and instance overlay handling.
- [x] Add ordered `harness_integrations` to VM, admin, agent, and workspace templates and their
      effective setup models. Admin and agent use the same user-facet schema.
- [x] Enforce duplicate-name rejection, explicit selection, name-only defaults, whole-list
      replacement, omission inheritance, and explicit empty-list clearing.
- [x] Make the synthesized default session explicitly select shell; remove silent fallback from
      dictionary resolution and effective template finalization while preserving inherited
      selection.
- [x] Verify ordinary capability conformance, per-facet unknown fields, selected model cache
      replacement/restoration, secret parity, list indexes, and all first-party sample manifests.

The first internal work unit implements the selection and schema plumbing with only the existing
session hosting enabled. Setup fields become public only together with executing lifecycle calls;
accepting setup configuration that performs nothing is not a deliverable. The native settings
parser/merge unit can be built independently and becomes reachable through those calls.

## Resource invocation and native lifecycle evidence

Keep setup bound to its owner. Session launch intent and conversation state stay session-specific.
VM/admin calls belong to VM init/reinit, agent calls to agent init/reinit, and workspace calls to
the shared creation body before successful commit. Existing install commands retain their hermetic
env; the integration lane receives the resource's assembled env through its full transport runner.

- [x] Separate setup construction from session target guards, probe caches and conversation state;
      add typed VM/user/workspace invocations with no-op defaults and one shared user method.
- [x] Increment the harness contract version across the descriptor and four integrations; preserve
      the required session `start` operation and launch alternatives.
- [x] Bind env using existing precedence and protected identity variables; extend owning secret
      preflight to cover setup env, including pending targets, without persisting resolved values.
- [x] Define versioned compact native receipt codecs under the existing instance-state facility,
      with VM/admin separation and agent/workspace owners; preserve unknown versions and unrelated
      keys.
- [x] Serialize observation, native mutation and checkpoint persistence per owning resource across
      processes without holding a state-database transaction over remote work.
- [x] Invalidate completed evidence before mutation, checkpoint each successful prefix, retain
      incomplete/failed cleanup evidence, and complete only after the facet succeeds.
- [x] Reconcile removed activations using prior ownership with absent desired config, including
      unavailable integrations, unowned conflicts and already-absent effects.
- [x] Integrate cleanup before owner deletion discards needed evidence. Verify cascade lock order,
      partial failure and recovery without falsely adopting unrecorded native residue.
- [x] Carry typed evidence through VM backup exports, exported codec round trips, and existing
      database backup/restore. Do not add a VM restore workflow.
- [x] Integrate workspace setup with existing create rollback and fresh-create retry for both
      standalone and session-created workspaces; no workspace reinit or residue-adoption path.
- [x] Add typed required/recommended upstream gaps to session readiness using actual bindings,
      current completion evidence and inexpensive native probes; report owning remediation.
- [ ] Exercise shell no-op setup and launch through real CLI, including setup env delivery, explicit
      enablement and absence of implicit ancestor setup during start/restart.

## Claude and Codex native setup

The integration owns commands, native format parsing, destination roles and observations. Core owns
source snapshot facilities, transport context and receipt persistence. Plan settings and plugin
changes together before the first native write; installation commands may modify native settings.

- [x] Implement local workstation file snapshots using existing SourceRef spelling and native
      host-path rendering, with stable bytes for the operation and cleanup on success/failure.
- [x] Implement user/project settings mappings for Claude JSON and Codex TOML with replace,
      merge-overwrite, merge-preserve and skip-existing policies.
- [x] Verify recursive objects/tables, atomic arrays, type collisions, duplicate-key rejection,
      source validation even when skipping, invalid merge destinations, and unsuitable destination
      links.
- [x] Define native plugin/marketplace query and reconciliation for both integrations; preserve
      installation identity, scope, drift probes and safe removal facts in receipts.
- [x] Reject conflicting desired plugin/settings state before writes; reconcile matching entries
      once, honoring the effective mapped document after preserve/skip policies.
- [x] Prove repeated setup converges, source changes take effect on owning reinit, managed removal
      converges and settings removal retains files while relinquishing only mapping claims.
- [x] Migrate Claude fields and both core installer call sites into the user facet, including
      existing desired-overlay payloads and actionable old/new conflict handling.
- [x] Preserve ordinary session models, fresh/resume behavior and CLI installation through existing
      install-command configuration. Session start must need no workstation settings source.

## Migration and permanent collateral

- [x] Update declarative samples and user setup guidance to explicit integration lists, including
      administrator activation placement and multiple configured integrations.
- [x] Document custom silent-session-lineage migration and existing instance recreation remedies;
      never infer an integration from its stored conversation namespace.
- [x] Update capability, harness integration, env, instance-state and idempotency documentation with
      implemented contracts; remove obsolete core-Claude ownership descriptions.
- [x] Update generated schema/reference/completion and guide surfaces through their owning
      mechanisms, with behavior evidence for each new field rather than prose-pinning tests.
- [x] Promote load-bearing architectural conclusions into permanent homes with their implementation.

## Acceptance and closeout

Implementation tasks are complete. The remaining live scenarios below belong to integration
validation, followed by acceptance and SDD closeout. Keep those boxes open until observed evidence
satisfies them; ready-for-review does not claim that they already passed.

Use local native plugin/marketplace/settings fixtures so external services cannot confound the
vertical. Real CLI evidence must observe destinations and receipts, not merely mock the dispatch.
Read the integration-testing and agw-test-env skills and establish the scoped inventory and budget
before creating live resources. Native CLI research can use isolated local homes and fixtures,
without touching the operator's installed plugins or authentication.

- [ ] Prove VM, admin, agent, workspace and session env and facet mapping, explicit enablement,
      multiple independent configurations, inherited lists and deliberate empty selections (R1-R6,
      R8).
- [ ] Observe two users and two workspaces for native plugin/settings applicability and all four
      mapping policies; verify session-only compatibility (R3, R13-R15).
- [ ] Observe required/recommended/absent prerequisites for the actual user, including stale and
      failed setup and pending explicit resource creation (R10).
- [ ] Exercise repeat setup, one-entry removal, removal of an integration activation, interruption,
      concurrent mutation, native drift, unknown codec versions, failed cleanup and owner deletion
      (R9).
- [ ] Prove failed workspace setup unwinds under standalone and session-created paths and retry
      refuses unexplained residue (R13).
- [x] Run full repository gates and focused native Windows tests from the current CI selection;
      record actual commands, exit codes and unreachable live surfaces.
- [x] Obtain independent project, complexity and generic correctness/security reviews, resolve
      material findings, and validate the final integrated head.
- [x] Publish a coherent implementation handoff and consume published feedback only within operator
      authorization. Never treat a review report as authority to widen the effort.
- [ ] Verify cleanup independently at all layers used by testing, update the plan with evidence, and
      complete lock/promotion only when all retained requirements are proven.

## Local implementation evidence

The implementation and approved design are consolidated on `sdd/harness-scope-framework-hla` in
PR 761. Private review and local validation cover the implementation boxes above; live acceptance
remains open. GitHub authentication uses the configured Git credential helper. The implementation
checkpoint `024fa2b3` passed all ten CI checks on PR 780, including native Windows (104 passed, one
skipped); its published review reports remain part of the review record after consolidation. Live VM
acceptance still requires the operator's scoped backend inventory and budget. Final locking remains
pending acceptance.

At `bb99e9eb`, the final Linux unit selection passed 8,847 tests with three skips. Ruff lint and
format passed; strict mypy passed across production and tests (801 files). The isolated native CLI
suite includes 48 cases against actual Codex and Claude commands. Independent project review passed
136 focused cases at `701a4d7a`, and generic security review independently passed the 48 native
cases. Both marketplace identity findings, the reinit intent race, executable discovery and
retirement config-root findings were resolved. The complexity reductions in `bb99e9eb` remove unused
deletion parameters, a duplicate rehome check and unnecessary agent template walks; all three
independent lanes reviewed this final delta clean. Project review passed 128 focused cases, while
complexity and generic security review each passed 51.

Website Python (160 cases) and Node (103 cases) tests, file linters, rulesync drift, locked-SDD
checks and business-layer typer isolation passed locally. Deterministic website double builds passed
for `/` and `/agentworks/`, with scratch outputs removed. An isolated real `agw` drive passed config
initialization, manifest loading, resolved inheritance and empty-list checks, all five facet-host
schemas and samples, all four integration references, and duplicate-name refusal. Guide examples
also passed registry validation and six CLI inspections. Fixture homes and completed scratch
checkouts were removed; the committed design and implementation checkouts remain for publication.
These results do not substitute for native Windows or remote VM acceptance.

## Implementation feedback round 1

The operator authorized this round after both published lanes reported across PRs 780 and 761. The
batch includes the repeated Muntz findings on the consolidated head. Native implementation helpers
now live in `agentworks.plugins._harness_native`, resolving the R11 placement question without
changing generic capability dispatch or adding a per-tool abstraction. The redundant
post-publication settings read is removed; guarded atomic publication still compares the expected
destination. Both user settings paths share claim matching and retain every matching receipt.
Readiness probes now take actual destination inputs without constructing setup invocations, and the
shell reference describes explicit selection accurately.

The prerequisite severity guard remains: it validates the result of an integration hook, whose
dataclass annotations are not runtime enforcement. New hook-boundary tests demonstrate refusal for
malformed objects and unknown severity. Recorded VM, user and workspace destination hashes remain
compatible. The native cleanup passed 128 focused cases and 159 Claude/Codex integration unit cases;
the readiness change independently passed 40 focused cases in project review.

The earlier suite counts used different selections: 8,847 passed with CI's `not integration`
selection, while three additional integration-marked doctor tests account for the published 8,850
count. The stale `claude_plugins`/`claude_marketplaces` example in
`.rulesync/subagents/agentworks-reviewer.md` is recorded in the published feedback for its policy
owner; it is outside these implementation edits. Saga conformance was clean. Live acceptance and
final locking remain open.

At `863dee06`, all three private round reviews were clean. Project review passed 91 native and
readiness cases plus 159 Claude/Codex integration cases. Complexity review passed 91 changed-area
cases and demonstrated that removing hook-boundary enforcement makes both malformed-return tests
fail. Generic correctness/security review passed 66 focused cases, eight independent retention
fixtures and six cold import orders. The integrated CI selection passed 8,855 tests with three
skips; strict mypy passed across 802 production/test files, and Ruff lint/format passed. File lint,
rulesync drift, locked-SDD and typer-isolation checks passed. Website Python (160) and Node (103)
tests and deterministic double builds for both site bases passed. These local gates returned exit
code zero. Native Windows remains a CI check; remote VM acceptance remains unperformed.

## Operator terminology refinements

The operator requested `create_new_agent_user` for fresh account creation; `6ae9dd3f` delivered the
identifier-only rename with 56 focused tests, the full 8,855-test selection and clean project and
complexity reviews. The subsequent terminology change names each explicit integration declaration an
integration activation, including defaults-only declarations. Code identifiers, guides and this
effort's design use that name. Facet is defined generally as a scoped part of a capability; the
current four-facet harness contract and other capability kinds' behavior remain unchanged. Neither
integration activation nor plugin enablement is proof of successful setup. Live acceptance and final
locking remain open.

The activation rename passed 196 focused cases and the full CI selection (8,855 passed, three
skipped) at `4938a36e`, with strict mypy across 802 production/test files and Ruff lint/format
clean. The final wording correction passed 221 config/reference/plugin cases; real CLI references
for Claude and Codex show the chosen facet terminology. Project and complexity reviews are clean at
`97651d3f`; syntax-tree comparison confirmed the rename and subsequent prose-only refinement. File
lint, locked-SDD, rulesync drift, typer isolation, website Python (160) and Node (103) tests, and
deterministic builds for both site bases passed. Main's artifact-reconciliation documentation at
`566d1819` was merged without conflicts or implementation changes before handoff.

## Main reconciliation and ready handoff, 2026-09-10

The operator requested reconciliation with merged PRs and clarified the handoff signal. Main at
`c7dce3d4` adds degraded inventory and raw database recovery, session listing fields and bulk-start
selection, and integration-testing coverage guidance. The database constructor conflict retains both
main's database-use lock and this effort's resolved `Database.path`; automatic session lifecycle
merges retain upstream selection and this effort's setup readiness checks.

The added restore cases exercise their shared boundary: replacement is refused while setup holds the
database open, and pending native receipts and unknown future payloads survive restore after the
database is closed. The source is opened through a relative path so the resolved native-lock
identity is exercised too. Implementation has no planned feature work remaining; the ready handoff
requests independent integration validation. Live acceptance and final locking remain open.

At `bc8a4685`, the local Linux CI selection passed 8,919 tests with three skips; strict mypy passed
across 803 production/test files. Ruff lint/format, file lint, rulesync drift, locked-SDD and typer
isolation checks passed. Website Python (160) and Node (103) tests and deterministic double builds
for both site bases passed. Project, complexity and generic correctness/security reviews of the
reconciliation are clean, with 147, two and 202 focused tests respectively. These are local and
fixture results; native Windows CI and independent live backend acceptance are separate evidence.

## Operator-directed simplification, 2026-09-11

The operator rejected the new native-receipt restrictions on VM deletion and directed a full KISS
scan of this effort. Parent deletion follows the existing core lifecycle: deleting a VM, user home,
or workspace directory removes its contained native files. Setup records do not authorize or veto
those operations. This ruling supersedes the earlier checked implementation of cleanup before owner
deletion and the settings-claim retention work recorded above; those entries describe work that was
completed and is now deliberately removed.

The scan covered lifecycle integration, native helpers, facet/config/schema propagation, migration,
environment composition, readiness, receipt codecs, and inspection/backup. It removes individual
plugin retirement before parent deletion, receipt-based workspace rehome refusal, the generic
destination mismatch mutation gate, per-file/per-key settings ownership bookkeeping, and the
native-settings changed-key classifier. Settings mappings apply their selected policy to the live
file; plugin fields remain explicit, and final publication retains its atomic compare-and-swap.
Plugin and marketplace claims remain because their removal needs actual ownership evidence.

The shared VM-family mutation lock remains: deleting a parent must not race setup inside it.
Conditional receipt discovery or parent/child lock ordering would introduce more machinery. The
tradeoff is serialization of mutating operations on the same VM, including ones without harness
activations. Fresh-account creation checks also remain because failed-create rollback must not
delete a pre-existing user's home. Explicit activation, no-op facet defaults, scoped secrets, and
integration-directed readiness retain their approved behavior.

The receipt codec now validates its already-parsed value directly, avoiding a second JSON parser
with a different nesting limit. The restore regression changes working directories after opening a
relative database path, so it now requires the resolved database identity it claims to exercise.
Native fixture launchers expose their Node runtime inside isolated test homes. The upgrade guide now
includes both template migrations. Independent live backend acceptance and final SDD locking remain
open.

The integrated simplification passed 8,930 tests with three skips on Linux using Codex CLI 0.154.0
and Claude Code 2.1.269. Ruff and strict mypy (803 files) passed. Project review identified stale
lifecycle and settings-claim prose, now corrected. Independent correctness review passed 232 focused
tests without a finding. The complexity pass also removed redundant terminal receipt checkpoints and
an unused setup-preparation parameter.

Installing native CLI fixtures in CI remains a follow-up: GitHub rejected the prepared workflow
change because both available credentials lack workflow permission. The reviewed patch is retained
with the lead's handoff; this PR's CI workflow remains unchanged. Native fixture validation here is
local evidence, not a claim that CI installs those tools. Independent live acceptance remains open.

The final checkpoint and signature simplifications passed 74 focused tests. Website Python (160) and
Node (103) tests and deterministic double builds for both site bases passed. Typer isolation,
locked-SDD checks and Rulesync generation parity also passed.
