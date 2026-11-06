# Harness Scope Framework: Implementation Plan

## Facet independence and closeout, 2026-11-01

The operator authorized the saga's facet-independence amendment and completion of both this SDD and
its companion on 2026-11-01. This amendment supersedes earlier ancestor-freshness launch gates and
refusal to adopt matching unclaimed native entries. Completed checkboxes below remain the historical
record. The explicit integration-owned required/recommended prerequisite API and unsupported
activated-facet errors remain in scope and unchanged.

One implementation PR on `fix/harness-facet-independence`, based on `c8d4dfb`, carries both
`sdd:harness-scope-framework` and `sdd:agent-artifacts`, plus `saga:next-steps`. The operator
authorized up to three published feedback/fix rounds per PR. After independent private reviews and
gates, the ready handoff requests the separate integration-test session's full process and
explicitly lists the retained live acceptance obligations. Neither SDD locks ahead of that
acceptance. The final closeout records the R1-R16/R1-R10 audits and adds both lockfiles together.
Transport work remains with its owner; this implementation uses today's transport APIs.

- [x] Amend owned requirements and architecture to independent facets, matching-content adoption,
      per-entry conflict reports and advisory stale-ancestor artifact delivery.
- [x] Implement native adoption/convergence and interrupted-run retry with truthful partial setup
      evidence, preserving differing unowned entries and independent setup work.
- [x] Implement runtime routing over available captures and known deferrals without an ancestor
      freshness gate, recapture, implicit setup or duplicate delivery; preserve own-facet errors.
- [x] Align artifact publication, permanent guides and diagnostics with the amended contract.
- [x] Complete private project, complexity and correctness reviews and the required gates.
- [ ] Publish the complete ready handoff with the full live-test emphasis; critically assess and
      incorporate or rebut feedback within the three-round allowance.
- [ ] Audit the independent live evidence against every retained acceptance requirement, reconcile
      completed follow-up handoffs with their merge evidence, and close actual remaining gaps.
- [ ] Promote remaining load-bearing guidance, audit all requirements and create `locked.md` only
      once both efforts' retained obligations are complete.

- Status: Implementation authorized by the operator after design review on 2026-09-07
- Governing requirements: [FRD](frd.md)
- Approved architecture: [HLA](hla.md)
- Implementation baseline: `3641ea8c` on main, combined with the approved design

## Delivery and ownership

The operator directed the approved FRD/HLA and implementation to share PR 761. Continue on
`sdd/harness-scope-framework-hla`; PR 780 is superseded, with its review reports retained and linked
from the consolidated checkpoint. Config exposure, execution, applied-state records and migration
form one working increment. The large diff receives separate configuration, lifecycle, native setup
and migration review units before the combined handoff. The implementation is intended to merge. A
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
- [x] Define versioned compact native applied-state codecs under the existing instance-state
      facility, with VM/admin separation and agent/workspace owners; preserve unknown versions and
      unrelated keys.
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
- [ ] Exercise unsupported setup refusal and shell session launch through the real CLI, plus setup
      env delivery through implemented facets and absence of implicit ancestor setup at
      start/restart.

## Claude and Codex native setup

The integration owns commands, native format parsing, destination roles and observations. Core owns
source snapshot facilities, transport context and applied-state persistence. Plan settings and
plugin changes together before the first native write; installation commands may modify native
settings.

- [x] Implement local workstation file snapshots using existing SourceRef spelling and native
      host-path rendering, with stable bytes for the operation and cleanup on success/failure.
- [x] Implement user/project settings mappings for Claude JSON and Codex TOML with replace,
      merge-overwrite, merge-preserve and skip-existing policies.
- [x] Verify recursive objects/tables, atomic arrays, type collisions, duplicate-key rejection,
      source validation even when skipping, invalid merge destinations, and unsuitable destination
      links.
- [x] Define native plugin/marketplace query and reconciliation for both integrations; preserve
      installation identity, scope, drift probes and safe removal facts in applied-state records.
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
vertical. Real CLI evidence must observe destinations and applied-state records, not merely mock the
dispatch. Read the integration-testing and agw-test-env skills and establish the scoped inventory
and budget before creating live resources. Native CLI research can use isolated local homes and
fixtures, without touching the operator's installed plugins or authentication.

- [ ] Prove VM, admin, agent, workspace and session env and facet mapping, explicit enablement,
      multiple independent configurations, inherited activation maps and explicit null opt-outs
      (R1-R6, R8).
- [ ] Observe two users and two workspaces for native plugin/settings applicability and all four
      mapping policies; verify session-only compatibility (R3, R13-R15).
- [ ] Observe required/recommended/absent prerequisites for the actual user, including stale and
      failed setup and pending explicit resource creation (R10).
- [ ] Exercise repeat setup, one-entry removal, removal of an integration activation, interruption,
      concurrent mutation, native drift, unknown codec versions, failed cleanup and owner deletion
      (R9).
- [ ] Prove core workspace creation failures unwind under standalone and session-created paths and
      retry refuses unexplained residue. Under the 2026-11-04 ruling, facet execution failures
      instead retain the created owner and partial records while independent work continues (R13).
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
destination. Both user settings paths share claim matching and retain every matching claim.
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
database open, and pending native applied-state records and unknown future payloads survive restore
after the database is closed. The source is opened through a relative path so the resolved
native-lock identity is exercised too. Implementation has no planned feature work remaining; the
ready handoff requests independent integration validation. Live acceptance and final locking remain
open.

At `bc8a4685`, the local Linux CI selection passed 8,919 tests with three skips; strict mypy passed
across 803 production/test files. Ruff lint/format, file lint, rulesync drift, locked-SDD and typer
isolation checks passed. Website Python (160) and Node (103) tests and deterministic double builds
for both site bases passed. Project, complexity and generic correctness/security reviews of the
reconciliation are clean, with 147, two and 202 focused tests respectively. These are local and
fixture results; native Windows CI and independent live backend acceptance are separate evidence.

## Operator-directed simplification, 2026-09-11

The operator rejected the new native applied-state restrictions on VM deletion and directed a full
KISS scan of this effort. Parent deletion follows the existing core lifecycle: deleting a VM, user
home, or workspace directory removes its contained native files. Setup records do not authorize or
veto those operations. This ruling supersedes the earlier checked implementation of cleanup before
owner deletion and the settings-claim retention work recorded above; those entries describe work
that was completed and is now deliberately removed.

The scan covered lifecycle integration, native helpers, facet/config/schema propagation, migration,
environment composition, readiness, applied-state codecs, and inspection/backup. It removes
individual plugin retirement before parent deletion, workspace rehome refusal based on applied
state, the generic destination mismatch mutation gate, per-file/per-key settings ownership
bookkeeping, and the native-settings changed-key classifier. Settings mappings apply their selected
policy to the live file; plugin fields remain explicit, and final publication retains its atomic
compare-and-swap. Plugin and marketplace claims remain because their removal needs actual ownership
evidence.

The shared VM-family mutation lock remains: deleting a parent must not race setup inside it.
Conditional applied-state record discovery or parent/child lock ordering would introduce more
machinery. The tradeoff is serialization of mutating operations on the same VM, including ones
without harness activations. Fresh-account creation checks also remain because failed-create
rollback must not delete a pre-existing user's home. Explicit activation, no-op facet defaults,
scoped secrets, and integration-directed readiness retain their approved behavior.

The applied-state codec now validates its already-parsed value directly, avoiding a second JSON
parser with a different nesting limit. The restore regression changes working directories after
opening a relative database path, so it now requires the resolved database identity it claims to
exercise. Native fixture launchers expose their Node runtime inside isolated test homes. The upgrade
guide now includes both template migrations. Independent live backend acceptance and final SDD
locking remain open.

The integrated simplification passed 8,930 tests with three skips on Linux using Codex CLI 0.154.0
and Claude Code 2.1.269. Ruff and strict mypy (803 files) passed. Project review identified stale
lifecycle and settings-claim prose, now corrected. Independent correctness review passed 232 focused
tests without a finding. The complexity pass also removed redundant terminal applied-state
checkpoints and an unused setup-preparation parameter.

Installing native CLI fixtures in CI remains a follow-up: GitHub rejected the prepared workflow
change because both available credentials lack workflow permission. The reviewed patch is retained
with the lead's handoff; this PR's CI workflow remains unchanged. Native fixture validation here is
local evidence, not a claim that CI installs those tools. Independent live acceptance remains open.

The final checkpoint and signature simplifications passed 74 focused tests. Website Python (160) and
Node (103) tests and deterministic double builds for both site bases passed. Typer isolation,
locked-SDD checks and Rulesync generation parity also passed.

## Applied-state terminology cleanup

The operator confirmed the existing boundary: instance state provides shared storage, the core
harness dispatcher manages completion and checkpoints, and integrations supply native results and
reconciliation semantics. Documentation, diagnostics, comments and test names now call this applied
state consistently. The payload, storage keys and runtime behavior are unchanged; this introduces no
capability-wide reconciliation framework. The instance-state guide also clarifies that native claims
record plugin and marketplace ownership, not settings-file contents or a settings ownership ledger.

Validation: 74 focused tests, Ruff, strict mypy (803 files), file lint and locked-SDD checks passed.
Project review confirmed the Python changes are limited to terminology and test identifiers. The
complexity review clarified ownership claims as facts within applied state and checkpoints as
updates to that state.

## Greenfield review and authorized iteration

The operator commissioned an independent review of the total PR alongside published feedback and
authorized one iteration if needed. The reviewer examined all 177 changed files from main `c7dce3d4`
through `3be6e977`, without prior review comments or conclusions. Separate isolated selections
passed 2,383 tests with one skip. This is local review evidence, not live acceptance.

The review found that session setup prerequisites were checked after secret resolution for existing
owners. The accepted correction moves that check to the existing preflight boundary while retaining
post-creation checks for pending owners. A smaller guide correction states the permissions applied
on publication and the preservation of permissions on unchanged or skipped settings files. The setup
invocation docstring also limits checkpoint requirements to ownership changes.

Published feedback is considered in the same iteration. The empty-facet disagreement concerns the
approved R3 no-op contract: a current applied declaration does not claim native software
installation. The merge-preserve policy retains existing values as requested; source edits require
merge-overwrite when they should replace existing values. Neither changes as part of the preflight
correction.

The integrated correction at `8d9c112e` passed 8,946 tests with three skips using the installed
Codex CLI 0.153.4 and Claude Code 2.1.265. Ruff and strict mypy (804 files) passed, as did file
lint, locked-SDD and Typer-isolation checks, website Python (160) and Node (103) tests, and
deterministic builds for both site bases. The 16 new manager cases cover existing and pending
owners, refusal before secret resolution, one recommendation warning, preserved runtime and
rollback. Independent project, complexity and correctness reviews of the correction are clean; the
project reviewer reproduced the fixed ordering and both original findings are closed. The one
authorized iteration is complete pending its exact-head GitHub checks. At that handoff the separate
R3 disagreement still awaited the operator; the ruling below supersedes it. Live acceptance and
final SDD locking remain open.

## Operator-directed unsupported-facet correction, 2026-09-11

The operator resolved B4 by requiring a hard error for activation of an unimplemented facet. This
supersedes R3's earlier successful base no-ops, including the completed implementation milestone
above. Implemented facets can still succeed with default-only or already-satisfied config; inactive
integrations are skipped and do not receive successful applied state. The correction needs no
support registry or performed-work flag. Shell and Grok remain session-only; Claude and Codex
implement user and workspace setup, and all shipped VM setup facets remain unimplemented.

The successor direction now records lazy passthrough for inactive integrations. Without a VM facet
activation, all defined inputs remain unhandled and go directly to the session, with origin
preserved and no broadcast through user and workspace. Inactive intermediate facets likewise leave
their applicable inputs for the session. This is future design direction, not an artifact API in
this PR. The saga-owned earlier no-op ruling needs reconciliation by its owner; the FRD records the
operator's superseding decision here.

- [x] Reject unsupported setup invocation and cover failure state, skipped activation, and
      successful implemented setup without changes.
- [x] Update FRD, HLA, examples and permanent guidance, then run checks and independent reviews.

Implementation `40ce6b4c` adds only the three base-method errors; existing dispatch records
incomplete setup on failure and handles retirement. The final worker suite passed 8,980 tests with
three skips, Ruff lint/format and strict mypy (804 files). Both affected modules passed 81 tests
again in the integrated lead tree. File lint, locked-SDD and Typer-isolation checks, local Rulesync
parity, website Python (160) and Node (103) tests, and both deterministic double builds passed.
Installed native fixture tools remain Codex CLI 0.153.4 and Claude Code 2.1.265.

Independent project and complexity reviews of `fd796bf1..40ce6b4c` are clean; each passed the 81
affected tests in its own environment. The complexity reviewer restored the old base methods in its
scratch tree and observed all 20 unsupported-activation cases fail, then restored the reviewed code.
This closes B4 under the operator's decision without new runtime routing machinery. The directed
correction is complete; the handoff records final-head CI separately. Independent live backend
acceptance, saga-owner pickup, and final SDD locking remain open.

## 2026-09-15 activation-map correction

The operator requested a separate implementation PR with name-keyed maps for every harness config
facet. This supersedes the completed list-shape work above without changing its historical record.
The new PR branches independently from main. The operator subsequently clarified that the session
selector keeps its existing tagged shape and setup activation maps support explicit null opt-outs.

- [x] Implement the initial keyed config, schema-directed inheritance, per-setting provenance, and
      strict facet validation; apply the subsequent operator clarifications below.
- [x] Carry maps through setup and artifact consumers, retaining applied-state comparison semantics.
- [x] Give historical saved setup lists an explicit migration disposition; preserve compatible saved
      session selections and unrelated instance config.
- [x] Update permanent guides, samples, reference projection, and the governing design together.
- [x] Keep the single session selector unchanged and add per-key null opt-outs to setup maps,
      including persisted instance specs and removal of inherited config references.
- [x] Verify inheritance, defaults, selection replacement, source attribution, saved-state recovery,
      readiness, and lifecycle behavior; run required gates and independent private review lanes.
- [x] Publish the complete PR ready for review and monitor its feedback under the delivery process.

The clarified implementation passed 9,657 non-integration tests with seven skips, Ruff lint/format,
and strict mypy across 849 files. File lint, locked-SDD and Rulesync checks passed, alongside 160
Python and 103 Node website tests and deterministic double builds for both site bases. An isolated
home running the real CLI verified inherited siblings, same-key configuration and source
attribution, null removal, fresh reactivation, the unchanged session selector, and resource
sample/explain output. No live VM provisioning was performed in this author run; the ready handoff
triggers the normal independent PR validation pipeline.

Project, generic correctness, and complexity reviews covered the implementation and the operator's
session/null clarifications. Corrections preserved reserved integration names and non-string mapping
references, kept unrelated unreadable saved state protected during explicit list migration, fixed
session guidance, and removed the obsolete tagged-facet cache. All material findings are resolved;
the final accepted cleanup is recorded at `4405e9e7`. PR feedback and independent live acceptance
remain separate from this completed implementation and private validation.

### PR 816 feedback correction

The operator authorized a focused correction after the later Muntz review identified incorrect
native-list merge guidance. The existing native `plugins` and `marketplaces` replacement policies
remain unchanged. The guides now describe them explicitly and show a disabling template followed by
a reactivating child to reset a whole integration config while preserving its siblings.

- [x] Correct the native-list guidance and whole-config reset example.
- [x] Remove the unused non-null map projection branch, align its fixture with the supported host
      shape, and address the ordering text, shadowed variable, and JSON null boundary comment.
- [x] Validate and privately review the focused correction.

The correction passed 9,657 unit tests (7 skipped), Ruff, strict mypy, file lint, Rulesync parity,
locked-SDD and Typer-isolation checks, 160 Python and 103 Node website tests, and deterministic
double builds for both site bases. An isolated home running the real CLI verified native plugin list
replacement, empty-list clearing, inherited marketplaces and sibling activations, and fresh
configuration after disable/reactivate for both Claude and Codex. No live VM provisioning was
performed. Project, correctness, and complexity reviews found no material issues; their final
example correction explicitly supplies the marketplace discarded by a whole-config reset. The ready
handoff and independent PR feedback remain delivery work recorded on PR 816.

The operator agreed to a separate follow-up for a consistent omitted-versus-null policy across
inheritance: omission inherits; explicit null clears the inherited value, with defaults or required
value validation applied afterward. Unknown opt-out key validation belongs in that follow-up's
clearing contract; this correction preserves the current treatment of unavailable integrations.

## Follow-up handoff audit, 2026-11-01

The activation-map handoff box is complete: PR
[816](https://github.com/WayfarerLabs/agentworks/pull/816) merged on 2026-09-15 as
`a22d5cb6b73b03d116335dc3a732685d0e6289ae`. Its final feedback closeout records clean saga, Muntz
and integration-test reports at `67bb0dc1`, with no remaining correction. The agreed broader
null-inheritance follow-up is separate. This audit closes only the stale publish box; it does not
substitute that report for the live matrix still listed above.

## Facet independence implementation evidence, 2026-11-01

The complete implementation was reviewed at `06fa0dfcb6511627e47b5e98b682ca1f6891ed11` against
`c8d4dfb`. Independent project, complexity and correctness reviews are clean after correction of
stale inactive inspection, obsolete strict routing, ancestor-only argument checks and same-origin
native replay. Recognized ancestor metadata is separate from new policy paths and strict proposal
expectations; old name drift cannot block unrelated new delivery, while actual competing names
remain errors. No persisted-state version or transport API changed.

Lead validation on that code head: **10,297 non-integration tests passed, 11 skipped**; Ruff lint
and format passed (920 files), and mypy passed (884 files). Wheel and source builds passed. File
lint, locked-SDD and Rulesync 7.14.0 checks passed, as did CI's Typer-isolation check. Website
validation passed 160 Python and 103 Node tests, with 14 files identical across both repeated builds
for each site base. Actual isolated local marketplace tests used Claude Code 2.1.316 and Codex
0.166.0; those are observed versions, not minimum requirements. Target-side native probes exercise
Claude, Codex and Grok replay without starting a model.

The real CLI ran help, isolated config initialization, both harness/artifact guide topics and
artifact inspection help successfully. The temporary HOME was independently verified removed. An
attempted workspace-local pytest fixture root inherited default ACLs and exceeded tmux's socket path
limit, producing 14 fixture failures. Unchanged `c8d4dfb` reproduced the initial ACL failure:

```text
AssertionError: assert 'system.posix_acl_access' not in ['system.posix_acl_access']
```

The tmux failure reported `File name too long`. All 145 tests from the affected modules then passed
under a short clean temporary root, followed by the full passing run above. Both short roots were
verified removed; no failing cases were filtered out and no product checks were weakened.

The operator directed live validation to the separate integration-test session triggered by the
ready PR. This lead created no live backend resources and claims no completed live matrix from these
local checks. The handoff requests every retained acceptance scenario below, with particular
attention to interrupted native registration, independent facets during upstream drift, actual users
and workspaces, own-session failure before teardown, native options and cleanup. Both SDDs remain
unlocked until that evidence is audited and the remaining requirements are complete.

## Facet independence feedback, round 1 of 3

PR [862](https://github.com/WayfarerLabs/agentworks/pull/862) opened ready at `72923b6`; all CI jobs
passed. After the one-hour reporting window, the first authorized public correction round began on
2026-11-01 at 07:38 UTC, with the PR returned to draft before source changes. The feedback batch
contains Copilot's six threads and the initial saga and Muntz reports. No independent live
acceptance report is claimed from those code reviews.

The accepted corrections cover whole-file session collisions before teardown, modified obsolete
members of requested skills, pre-render setup name collisions, recorded routes independent of
current upstream routing, shared strict/advisory native discovery and adoption-report events.
Optional simplifications stay within those paths. The native inventory reread and safe retirement
guards stay: marketplace registration can change the source observable for an installed plugin, so
manual inventory updates do not replace the fresh observation.

Codex's listing follows current marketplace registration without proving an unclaimed installation's
original source. The operator has been asked to choose conservative preservation with a reported
recovery limit or persisted installation-intent tracking. That finding remains unresolved;
independent corrections proceed without inventing ownership or an intent-state protocol. The
`awaiting-direction` label identifies this decision. The remaining round allowance stays dormant
after this round until the operator resolves it.

- [x] Complete the independent accepted corrections and their focused regression checks.
- [x] Resolve the Codex provenance decision through authenticated operator direction and complete
      the resulting correction within its agreed boundary.
- [x] Run the three private review lanes and required gates on the corrected exact head; publish the
      complete handoff with the appropriate draft/ready signal (completed by round 2 below).
- [ ] Audit independent live acceptance against every retained requirement and finish promotion and
      both locks. Unit tests and CI alone do not close the live obligations above.

Round 1's independent corrections are reviewed clean through `eb1a081`, whose production code is
unchanged from `a13ddfe`. Each private lane independently passed 400 relevant tests; the project and
complexity lanes also checked collateral and plan honesty. The complexity lane reproduced the
negative control that makes a single setup probe unsafe: deferring a proposed persona after an
identity mismatch can expose another native collision. The iterative probe remains. The fresh native
inventory observation and safe retirement guards also remain. Reporting checks assert event emission
and runtime entry identities, without pinning authored wording.

The full CI-selected local run passed **10,334 tests, 11 skipped** on the corrected production code.
The final type-only fixture changes repair three mypy errors without changing that behavior; full
mypy passes across 885 files, and Ruff lint/format passes across 921 files. Wheel and source builds
pass. Website validation passed 160 Python and 103 Node tests, with 14 files identical across both
builds for each site base. The real CLI passed help, isolated config initialization, both guide
topics and artifact inspection help. Managed temporary fixture/build roots and the isolated HOME
were independently verified removed. After correcting fixture types and document formatting, file
lint, locked-SDD and Rulesync 7.14.0 checks passed. CI and checkpoint state are recorded on PR 862;
the complete correction handoff remains pending the Codex decision.

Codex provenance remains unresolved under the operator question above. These are independent code
and local CLI results, not live VM acceptance or whole-PR readiness. Neither SDD is locked.

## Facet independence feedback, round 2 of 3, 2026-11-02

The operator resolved the held native-provenance question: use the native tool's reported full
association, preserve and warn about differing unowned associations, and add no historical cache
verification or installation-intent tracking. This supersedes round 1's pending-source notes.
Matching adopted entries become managed and may be safely retired when no longer declared. Native
version changes do not change the owned association.

The second authorized batch includes the unresolved source thread, the later live integration
reports and saga reading, and Muntz's checkpoint review. The preceding handoff's reporting window
elapsed before this round began. PR 862 stays draft with `review-requested` removed during work. The
seven live findings are addressed within the existing contract; own malformed/config/process/
transport and unsafe publication failures remain hard. There is no error-aggregation or directory
ownership engine. Empty session/run directories may remain after owned-file retirement.

- [x] Record the authenticated native-association ruling and make full marketplace identity,
      version-stable ownership and safe settings-overlay behavior converge with confirmed reports.
- [x] Make remaining native-name conflicts terminally unhandled, rerender complete session files and
      argv, and preserve genuinely non-conflicting native namespaces and own-session errors.
- [x] Report native-only incomplete or retired setup, leave skipped setup incomplete, preserve
      compatible-version repair guidance, and remove duplicated setup route arguments.
- [x] Add bounded file/section retirement and update reports, accurate executable-intent
      diagnostics, guest-index privacy and artifact ownership counts; keep provenance in workstation
      inspection.
- [x] Supply a bounded fixture launcher through existing registration and the real CLI for the
      separate prerequisite/unsupported-facet live acceptance cases, without a product plugin
      loader.
- [x] Complete all three private review lanes and integrated gates, then publish the exact head
      ready with the retained acceptance matrix and no checkpoint label.
- [x] Assess that ready handoff's full feedback batch. One authorized public fix round remains.
- [ ] Audit final-head live evidence, close every retained requirement, promote remaining durable
      content and add both locks. No unit or fixture result alone closes a real-VM obligation.

Native fixture validation observed Claude Code 2.1.316 and Codex 0.166.0. The focused native slice
passed 104 tests. The flow slice passed 1,143 tests with four skips, including actual adapter/guest
probes and real-manager failures preserving the existing run before teardown. The fixture-tooling
slice passed its fresh-process CLI checks and full branch suite, with real VM execution explicitly
unperformed. These were slice-level results at the start of the round, before integrated validation
or live acceptance. The validation record below supersedes that interim review and handoff status.

Round 2 validation is complete. The integrated CI selection passed **10,401 tests, 11 skipped** with
Claude Code 2.1.316 and Codex 0.166.0 available for the native fixtures. The first integrated run
exposed one existing inventory test still expecting the mutable plugin version in association
identity; the corrected test verifies stability across a version change, and the entire selection
then passed without filtering failures. Ruff lint/format passed across 925 files; mypy passed across
889 files. The acceptance launcher also passed separate lint, format and strict type checks.
Wheel/source builds, Typer isolation, file lint, locked-SDD and Rulesync 7.14.0 checks all passed.
Website checks passed 160 Python and 103 Node tests, with deterministic double builds for both site
bases. Real isolated CLI help, config initialization, both current concept guide topics and artifact
inspection help passed. The initial smoke used obsolete topic names; the corrected calls used the
names exposed by `guide list`. Temporary test/build homes and roots were verified removed.

All three independent private lanes reviewed the full PR and round 2 at `ef6d882`, including the
`0dc51c7` test/document correction, without material findings. The project and correctness lanes
identified the same stale readiness docstring; it now describes advisory artifact drift and explicit
integration-owned prerequisites. Independent boundary suites passed 87 and 96 cases; the complexity
lane also passed eight real-native cases and demonstrated that deleting conflict recovery or full
source/legacy-version handling breaks the required behavior. These are local CLI/native-fixture
results, not final real-VM acceptance. The exact ready head and CI result are recorded in PR 862's
round 2 handoff. One public correction round remains, and both locks still await the final live
requirement audit.

## Facet independence feedback, round 3 of 3, 2026-11-02

The one-hour reporting window elapsed before this final authorized correction round began. PR 862
returned to draft at `3748bcc` before edits; main remains `c8d4dfb`. The frozen batch includes live
report `6572238674`, Muntz `6571607223`, saga `6571111808`, and every unresolved item. The live
report confirms the original seven fixes and #840/#841, but finds two missing-Claude-declaration
paths that still need correction. Its gates passed with 10,401 tests and 11 skips; that evidence
belongs to the ready head, not the correction still under development.

- [x] Preserve unobservable Claude git/GitHub source associations during mapping, and preflight
      user-scoped removal before touching marketplace dependents.
- [x] Make native plugin ambiguity entry-local without retiring unresolved prior associations;
      handle malformed driver claim sources conservatively and retain legacy version support.
- [x] Persist current-attempt incomplete reasons in the existing versioned setup record and expose
      entry-specific repair diagnostics without new ownership or history mechanisms.
- [x] Strengthen missing-target coverage, remove redundant first-party identity-format checks,
      retain retry-progress guards and fix temporary-path-sensitive carrier assertions.
- [x] Align permanent guidance, bounded progress messages and current plan status with the fixes.
- [x] Resolve the own-failure policy question through authenticated operator direction; retain
      today's fail-fast pipeline until that direction arrives. `awaiting-direction` tracks this
      ambiguity, not a second request for the already-authorized FRD amendments.
- [x] Run all three private review lanes and full gates on the integrated head, then publish the
      appropriate complete handoff. This round exhausts the three-round public correction allowance.
- [ ] Audit final live acceptance against every retained requirement and create both locks only
      after the remaining actual-scope evidence exists.

The independent corrections do not wait on the pipeline policy choice. Missing native declarations,
ambiguous selection and unreadable claim identity are handled as conservative entry-local retention;
unexpected own process/config/persistence failures are not silently swallowed. The operator has been
asked whether those unexpected own failures should continue sibling integrations before a final
error. No error-aggregation mechanism is being added without that decision.

The latest live run used Linux and remote Lima/Debian 13, two 2-CPU/4-GiB VMs, Claude Code 2.1.316
and Codex 0.166.0, with local sources and no model calls. It independently verified VM/backend/SSH
cleanup, leaving only offline tailnet records for the operator. It does not close the outstanding
prerequisite/unsupported-facet fixture, real interruption, admin artifact, Git capture, power cycle,
resume-only, Grok launch or Claude workaround obligations. Workspace matching/conflict and project
settings cases can use files supplied by repository checkout during ordinary create; the verified
call order and controlled fixture are recorded in `6572186080`. No workspace reinit or workspace
plugin support is implied. Both locks remain pending.

At the initial private checkpoint, production head `4e286ac` passed the CI-selected local suite with
**10,433 tests, 11 skipped**, with real Claude Code 2.1.316 and Codex 0.166.0 fixtures available.
The first integrated source run passed 10,431 tests; private review then caught and corrected two
diagnostic regressions: session repair incorrectly targeted a VM, and an early retirement shortcut
could discard explicitly pending reason-only cleanup. The final full rerun includes both regressions
and passed without filtering failures.

All three independent private lanes reviewed the full PR and round 3 at `4b44d42`; project and
correctness findings above were incorporated, and all three re-reviewed `4e286ac` clean. Independent
focused runs passed 78, 123 and 139 cases; the complexity lane also exercised 16 real-native cases
and proved the persistence and pre-removal checks with failing deletion controls. Final narrow
checks passed three project/complexity cases and 66 correctness cases. These are local boundary and
native-fixture results, not a new real-VM acceptance claim.

Ruff lint/format passes across 926 files and mypy across 890 files. Full file lint passed after
correcting five spelling findings by revising the prose. Package builds, Typer isolation, Rulesync
7.14.0 and locked-SDD checks passed. Website tests passed 160 Python and 103 Node cases, with both
site bases deterministic. Real isolated CLI config initialization, help, both current concept guides
and artifact inspection help passed. Managed temporary fixture homes and test/build roots were
verified removed. Exact final-head CI and the checkpoint signal are recorded in PR 862's handoff.

The checkpoint deliberately remains draft with `review-requested` while the authenticated
own-failure policy choice is pending; `awaiting-direction` marks that question. This is a coherent,
green handoff of all independent fixes, not merge intent or a statement that either SDD is closed.
The three public correction rounds are exhausted. No additional correction is authorized solely by
new published feedback. The operator's policy answer and final live acceptance still precede both
locks.

A final adversarial native check before publication found the same missing-declaration defects when
Claude accepted an empty marketplace object, or one containing only an update option. Outer-key
membership did not prove a usable source declaration. Both predicates now require the inner source
member. Twenty-six targeted real-native cases passed, including missing, empty and update-only
objects; eight strengthened cases verified actual native ref/sparse source metadata remained intact.
Independent native commands can discard incomplete outer placeholders, so assertions allow that
native normalization while rejecting a fabricated source and verifying plugin/registration/claim
retention. Production code neither parses cache history nor restores a guessed declaration.
Integrated validation and narrow reviews of this final predicate correction follow before handoff.

Final predicate-correction validation is complete at source head `6e2d13a`: **10,449 tests passed,
11 skipped** in the complete local CI selection. Full static checks (926 Ruff files, 890 mypy
files), file lint, locked-SDD and package builds pass. Website checks again passed 160 Python and
103 Node cases with both site bases deterministic. The unchanged Typer-isolation and Rulesync gates
also passed earlier in this round. Required CI is green for source head `6e2d13a`; the exact final
checkpoint head is linked from the PR handoff.

All three private lanes re-reviewed the final two predicates and associated tests clean. Complexity
checks passed seven inventory cases; correctness independently passed 27 real-native cases,
including absent, empty and update-only declarations with actual ref/sparse preservation and
pre-removal dependent retention. The final documentation checkpoint is reviewed separately before
publication. This supersedes the earlier interim validation status while preserving its historical
counts and the late finding. The operator policy answer and the remaining actual-scope acceptance
matrix are still open; no lockfile or merge intent is claimed.

## Independent setup continuation, operator direction 2026-11-04

The operator resolved the earlier own-failure question by directing continuation of independent
facets and a final failure report. This is direct implementation authorization, separate from the
three exhausted public feedback/fix rounds. The 2026-11-02 checkpoint statements that the policy
answer was pending are historical and superseded here. Invalid activation preflight, cancellation,
shared state failures and unexpected internal errors retain immediate failure behavior. Session
launch safety remains unchanged.

- [x] Record the authenticated ruling and align the owned FRD, architecture and permanent guides.
- [x] Implement explicit operation-local failure collection, truthful partial records and
      independent continuation through VM/admin, agent, workspace and session-created ancestors.
- [x] Verify required prerequisites retain completed ancestors, recommended prerequisites remain
      advisory, and persistence/cancellation still abort without swallowed failures.
- [x] Complete the private project, complexity and correctness reviews, run required gates, and
      publish a coherent exact-head handoff.
- [x] Dispose the later published workspace permission/adoption and native status findings under
      authenticated direction; the exhausted feedback budget does not authorize another fix round.
- [ ] Audit the expanded live evidence from PR comment `6575184960` against retained requirements
      and close remaining acceptance gaps before creating either lockfile.

The later live report exercised prerequisite/readiness fixtures, real agent-reinit cancellation,
admin artifacts, Git capture/ref refresh, VM power cycles, Grok and Claude workaround launches,
workspace collisions and settings mappings, and multi-user isolation. It used Linux with remote
Lima/Debian 13 and performed no model calls. It reports independently verified cleanup. Its precise
limits include other workstation/backend combinations, the native-write/checkpoint interruption
window, real transcript creation, some native source variants and workspace copy/rehome. These are
scoped acceptance results, not blanket final-head acceptance or permission to lock either SDD.

The continuation implementation is reviewed at `df7b3cd`; production code is unchanged through the
subsequent VM test-import correction and developer-guide clarification. The full local CI selection
passed **10,498 tests, 11 skipped**. The final import-only delta also receives focused VM checks.
Ruff lint/format passes across 929 files and strict mypy across 893 files. Package wheel/source
builds, file lint, Rulesync 7.14.0, locked-SDD checks and Typer isolation pass. Website tests passed
160 Python and 103 Node cases; both site bases produced deterministic double builds. Real isolated
CLI help, config initialization, both concept guides and artifact inspection help passed; the
isolated home and build roots were removed and their absence checked.

All three private lanes found no remaining code issue in this increment. Project review passed 50
focused cases; complexity passed 68 and proved the callback infrastructure marker and required
consumer retention with failing deletion controls; correctness passed 92. Project review caught one
stale developer-README rollback statement, now corrected. Earlier worker checks run from the
repository root missed the CLI's strict configuration; root checks from `cli/` caught and corrected
four test typing errors and two test import groups before handoff. No ignored failures or relaxed
checks were used. Final documentation-only review and exact pushed-head CI are recorded with the
checkpoint handoff.

The [critical reading](https://github.com/WayfarerLabs/agentworks/pull/862#issuecomment-6638116414)
records every later material finding and the scoped acceptance evidence. Current continuation
contains a malformed native inventory failure to its facet but does not yet make inventory entries
individually skippable. Workspace ACL/adoption, native-only freshness and the uncovered retirement
and ambiguity guards remain open. The exhausted public correction budget is not silently renewed.
Neither SDD is locked or represented as complete.

## Additional authorized feedback/fix round 1 of 1, 2026-11-04

The operator explicitly approved one additional round after the green continuation checkpoint at
`9e52d4e`. The batch was frozen at 20:45 UTC, more than an hour after handoff, and includes the
previously open findings plus saga `6638655434`, Muntz `6639301024` and live report `6640566529`.
The
[critical reading and round charter](https://github.com/WayfarerLabs/agentworks/pull/862#issuecomment-6641797832)
records each disposition. The PR remains draft during work; `review-requested` was removed before
edits. This consumes the single additional round, not a standing allowance for later feedback.

- [x] Freeze the full batch, record authenticated authorization, assess every material item and
      establish isolated implementation ownership.
- [x] Preserve failed routing and existing descendant placements; enforce unsupported-facet
      preflight and retain fatal persistence/cancellation behavior.
- [x] Correct native entry-local inventory, bounded diagnostics, native-only freshness, workspace
      ACL/adoption and generated-section stability; cover the identified deletion controls.
- [x] Align recovery guidance, developer/operator guides, fixtures and acceptance milestones with
      the corrected behavior.
- [x] Integrate the changes, complete all three private review lanes and required local checks.
- [ ] Publish the exact green ready handoff and audit the separate full integration-test result.
- [ ] Reconcile the expanded live evidence and any later findings against retained requirements
      before final promotion and locking. Feedback arriving mid-round is a later batch.

The copied-workspace `template="copied"` resolution failure exists in current main: copy records
that synthetic template at `workspaces/manager/copy.py`, and artifact/runtime resolution calls the
strict workspace-template resolver. Copy also omits native applied evidence on main. This round
leaves that existing lifecycle contract unchanged rather than copying source ownership claims into a
new destination. It is a separate workspace follow-up, with live reproduction in `6640566529`. The
requirement that destination changes must not inherit proof of setup remains in force.

A workspace integration retry/reinit command remains deliberately excluded by the operator's
existing design. Retention preserves the owner's work and partial evidence; `workspace repair`
continues to handle access and Git identity only. Recovery guidance must identify retained owners,
existing-owner session bindings, the possibility of a running session after a failing command, and
the destructive nature of explicit workspace recreation. This round introduces no implicit setup or
new workspace command. Neither SDD is locked.

### Additional round validation and disposition

The integrated source at `3da18ff` passed the full local CI selection with **10,693 tests, 11
skipped**, with Claude Code 2.1.316 and Codex 0.166.0 available for real isolated native fixtures.
Configured Ruff lint/format passed across 934 files and strict mypy across 898 files. File lint,
Rulesync 7.14.0, locked-SDD checks, package wheel/source builds, Typer isolation and real isolated
CLI config/help/both concept guides/artifact inspection help passed. Website validation passed 160
Python tests, 103 Node tests and deterministic double builds at both site bases. The final
pushed-head CI and ready signal are recorded in the PR handoff.

All three private lanes reviewed the round and re-reviewed the corrected source clean. Initial
review exposed three additional cases: unreadable evidence after bundle declarations were cleared, a
partial first VM file prefix mistaken for confirmed routing, and control sequences reconstructing a
secret after raw matching. The fixes use the existing nullable input field and normalize secret
patterns before the final size bound. Twenty-two dedicated tests all failed on `11cc97a` and all
passed with the correction; user file operations run through real `NativeFiles`, while the VM
checkpoint failure is simulated. Follow-up focused reviews passed 142 project, 142 complexity and
164 independent correctness cases. Real local Git/POSIX ACL checks prove adoption, script execution,
group/mode preservation and an unchanged first repair; live acceptance across different users is not
inferred.

An isolated startup-banner check also caught a Python presence-probe regression. A line-delimited
marker now tolerates unrelated startup output while rejecting ambiguous or missing evidence; 131
focused cases cover local execution and first-party SSH command construction without a VM. The first
complete source run then caught a native settings interaction: retaining a marketplace for a project
plugin restored a user plugin's already-removed enable flag. Confirmed removals now take precedence
over snapshot protection. Five real-Claude cases cover no mapping and all four mapping policies,
repeated retirement, unchanged project bytes and stable bytes and inode in user settings. All three
lanes re-reviewed this final small correction clean; a deletion control reproduces the old failure.
The full suite was rerun without filtering failures and passed at `3da18ff`.

The unrelated website browser witness once failed while heavy checks overlapped. The complete
160-test website suite passed unchanged on a separate run, and CI website validation passed too. No
assertion, timeout or test-selection weakening was used. The additional round is now consumed; new
published findings require a later authenticated disposition rather than an automatic fifth
correction round. Both locks still await the final requirement-by-requirement live acceptance audit.

## Additional authorized feedback/fix round 1 of 2, 2026-11-05

The operator authorized up to two further public correction rounds and allowed unrelated fixes with
clear paths that do not conflict with ongoing Transport/SSH work. This is the fifth published
correction round, not a reset of the earlier historical allowances. The batch is frozen at
2026-11-05 16:32 UTC against `1e0d2fe`: critical reading PR comment `6651844728`, the final-head
live report `6646701174`, the instance-spec followup `6650616936`, and the expanded
Proxmox/workstation report `6652928141`. The start is recorded in PR comment `6663467162`.

- [x] Distinguish confirmed empty routing from unknown results without holding unrelated local
      application, including receiver-specific routes and inactive passthrough with opaque evidence.
- [x] Carry unavailable session routes through inactive intermediate facets, record terminal
      unhandled IDs/reasons and incomplete delivery, and keep optional inputs advisory. Preserve the
      old runtime's files before teardown and retire obsolete private runs afterward; retaining
      unused files would not deliver missing content and does not justify a history mechanism.
- [x] Normalize ambiguous older incomplete zero-deferral setup records through domain codec v4,
      using the same fields. Complete or explicit-route older records remain usable; early file
      checkpoints do not certify routing. No database schema, journal or native version floor added.
- [x] Support observed Claude file marketplaces, isolate named unknown native sources, preserve
      confirmed current-operation native mutations during snapshot restoration, and bound private
      native-error transfer even when its original stderr file is read-only.
- [x] Redact normalized guest errors and warnings before bounding them, preserve typed errors, frame
      native discovery and identity responses, and identify offending inventory paths. Actual
      per-path publication holds supply their own incomplete evidence; an unrelated upstream
      condition alone does not permanently fail a newly created workspace's local setup.
- [x] Isolate Git clones in unique private attempt paths and publish only confirmed completed
      checkouts. Preserve unexplained or occupied reservations instead of deleting them. Real Git
      controls reproduce replacement-work loss on the prior head and preserve committed, untracked
      and stashed work with the correction.
- [x] Correct the adjacent admin session-create log redactions and exact-pane output capture, with
      real command-log and local tmux checks. Explicit non-interactive confirmations decline without
      reading input or granting default consent. Add useful binding/corruption diagnostics and
      update permanent recovery guidance, including source changes needed before destructive
      recreation.
- [x] Correct adjacent Proxmox deletion and rollback classification using the exact structured
      provider missing-config response for the bound node/VMID. Locked, authentication, transport,
      task and malformed responses remain failures; direct delete preserves the core VM row.
      Rollback preserves its original failure/interrupt and warns. No Transport/SSH APIs changed.
- [x] Protect analogous admin session start/restart logs after resolving secret values. Preserve the
      successful core-owner status while disclosing incomplete native setup after soft holds or
      skips. Detach unsafe provider traceback causes, preserve safe typed domain causes, and forward
      missing explicit route IDs through fresh active receivers without fabricating payloads.
      Restore the capability import boundary and remove the redundant join wrapper.
- [x] Complete the three private reviews, required integrated gates and final collateral audit.
- [ ] Publish the exact green ready handoff without `review-requested`, then assess the separate
      full live result within the remaining one-round allowance.
- [ ] Finish the requirement-by-requirement acceptance and promotion audit and create both locks
      only after the remaining contracts are proven.

### Scope and remaining evidence

The integrated source before private review is `f0e6e39`. Local lane evidence is scoped: routing and
artifact/capability checks passed 1,365 cases with four skips; the broader native selection passed
327 cases, followed by 30 bounded-transfer/discovery cases; Proxmox/core-deletion checks passed 180
cases; workspace/session checks passed 153 cases. Lead diagnostic/session checks passed 166 cases,
and a later bounded diagnostic/readiness/tmux selection passed 55. These selections overlap, do not
constitute the full repository gate, and do not replace backend acceptance.

The new live report materially confirms prior routing-loss, unsupported preflight, workspace ACL,
native-only freshness, mapping and ref/sparse fixes. The subsequent report adds actual Proxmox 8/9
and workstation cells, lifecycle/rekey and cleanup evidence. Their new contradictory findings still
prevent a lock. Bookworm cannot currently be created by Agentworks, including through Proxmox, under
the existing release policy; an unavailable test cell is not silently a passing one.

Desired instance specs remain desired state rather than a last-known-good application snapshot.
Capture availability is not a structural declaration guarantee. This round reports retained template
bindings and keeps the existing desired-before-convergence policy; it adds no automatic rollback.
Malformed saved captures are not destructively replaced: their hints identify explicit state
recovery rather than an ineffective ordinary-reinit loop. Workspace copy/rehome lifecycle repair,
global Windows terminal/database classification, generalized remote-process cancellation, raw
SSH-log normalization/bounds and broader transport protocol cleanup remain outside this correction.
They need their owning contracts and current platform evidence. Unsafe publication through a link
remains a facet error; no-follow protection is retained. Proxmox hard-stop semantics remain
unchanged and are now documented; changing shutdown behavior would be a separate platform decision.
Certificate trust guidance now includes a verified CA path rather than offering only disabled
verification.

No final-head backend run or completion of either SDD is claimed by these local checks. The next
ready handoff must emphasize unknown versus confirmed-empty routes, first checkpoints and legacy
codec evidence, inactive passthrough, optional session omission/inspection, clone cancellation and
name reuse, native registration protection, bounded diagnostics, actual resolved-value logs, real
pane capture, and locked/outage/already-gone Proxmox deletion on both tested PVE generations.

### Corrected-head validation

The three private lanes reviewed corrected source `b78d0fe`, the collateral alignment at `4bde5ae`,
and the final test-only correction at `5f89ba1`, with no material findings remaining. Project review
passed 52 focused cases; Muntz passed 143 and verified deletion controls for missing-ID forwarding
and provider traceback secrecy; independent correctness passed 75, then the corrected final test.
These selections overlap. The project findings restored the original truthful session-create
checkbox and aligned the HLA, delivery LLD and durable artifact documentation with metadata-only
forwarding after known-input validation.

At `5f89ba1`, the full non-integration pytest selection passed 10,827 cases with 11 skips, exit 0.
An earlier run passed 10,826 with one obsolete assertion requiring missing-route metadata to remain
frozen; its replacement requires every missing identity to reach session deferrals while retaining
incomplete-state, skip, ownership and actual file-byte checks. The focused correction passed 56
cases. Ruff lint and format passed across 942 files; CI-exact mypy passed all 906 source/test files.
File lint, Rulesync drift, locked-SDD validation, Typer isolation, source/wheel builds, Python
website checks (160), Node website checks (103), deterministic builds for both site bases, and five
isolated CLI drives all returned 0. The CLI drives covered help, first config initialization,
artifact help and both packaged guides; the temporary home and site build roots were removed. These
are local and isolated-process results, not final-head backend acceptance. Neither lock is created
by them.

The closeout read also removed stale SDD statements about a shell file workaround and fatal native
identity collisions. Shell publishes directly; session identity conflicts leave affected inputs
unhandled while independent inputs continue. Unsafe filesystem overlaps remain errors.

## Additional authorized feedback/fix round 2 of 2, 2026-11-05

This sixth published correction round uses the final round of the operator's additional allowance.
The batch freezes at 2026-11-05 19:18:22 UTC against ready head `082437fe`, after the normal
one-hour reporting window. Muntz report `6666101942` identifies three material corrections; the
lead's critical reading `6666264141` independently reproduces the error paths and accepts all three.
Saga report `6665772379` is clean. Copilot's new review reports quota exhaustion, not findings.
There are no unresolved inline threads. Start comment `6667039978` records the batch, the current
main `c8d4dfb` and return to draft. The separate full live report was not published by the cutoff.

- [x] Preserve the typed workload failure and exit status through expected failures of main and
      alternate capture and server cleanup. Keep ordinary session-log capture strict and both pane
      targets exact; cancellation and unexpected internal errors retain their behavior.
- [x] Keep failed-command diagnostic transfer/read optional, preserving the native operation error
      without raw provider prose. Successful structured-response transfer remains required.
- [x] Use one new native setup domain codec version, 3, accepting released versions 1 and 2 and
      normalizing their ambiguous incomplete zero-deferral records. Preserve unknown future data.
- [x] Add a raw-before-normalized redaction regression with a failing deletion control. Keep the
      necessary pass; do not add a shared deferral abstraction or change missing-route logic merely
      for optional reason-text or counting differences.
- [x] Correct the related tmux liveness probe to target the exact pane and accept only valid native
      observations. Exercise an actual private server alive then dead at status 23; malformed
      successful output remains unverified rather than falsely alive.
- [ ] Complete the three independent private lanes and required integrated gates on the corrected
      head, and update permanent collateral wherever the correction changes an operator contract.
- [ ] Publish the exact green ready handoff without `review-requested`, reporting the exhausted
      correction allowance and the still-required independent live acceptance.
- [ ] Reconcile final acceptance against every retained requirement, promote any actual remaining
      guidance, and create both locks only when that evidence proves completion.

Supersession: the earlier completed branch-only codec work is truthful history; the final writer
combines those unreleased updates into version 3. No compatibility mechanism is introduced for the
intermediate branch-only versions. The current LLD and implementation describe the final format.

The two error paths are independently reproduced at the prior ready head. A failed main capture
loses the known workload status and skips cleanup; alternate capture and cleanup transport failures
also replace that primary error. A failed optional native diagnostic copy replaces the native
operation error. The redaction deletion control exposes a secret marker when terminal parsing
consumes the first character of a known multiline value. These are scoped local reproductions, not
final backend acceptance. The full live result remains necessary before either SDD locks.

### Integrated implementation evidence

Runtime correction `9bd3f369` integrates delegated commit `8c5fb5f3`. Its baseline produced 19
failures and 14 passes; the broader corrected selection passed 292 cases and the final focused
selection passed 37. Four private guard-deletion controls produced 5, 4, 4 and 6 failures. The
worker verified all 13 owned temporary roots absent and made no backend or model calls. The codec
correction initially produced 10 failures and 12 passes against the prior writer; the corrected
codec/routing/diagnostic selection passed 72. The new raw-redaction case passes with the guard and
fails after removing that pass; the original source was restored. Root mypy passed 906 files before
the runtime integration. These are scoped local checks; integrated gates and all three private lanes
remain required, and no final live acceptance is claimed.

The first integrated run at `295a6181` passed 10,858 tests with 11 skips and one stale legacy
fixture failure. The fixture labeled the earlier premature-ID writer as the now-current version 3;
`3baba4ae` uses released version 2 instead, preserving every file/ownership assertion. Its 28-case
selection passes. Private correctness review also proved a preexisting real tmux liveness target/
parser error that bypasses the corrected workload-failure path. The bounded prerequisite fix stays
in the same round; integrated gates and all three lanes will review the corrected head again.

Liveness correction `834463f4` integrates delegated `0f07c109`. The real-tool baseline produced 10
failures and 7 passes, including a dead pane reported as alive; the corrected session selection
passed 115 cases. Rolling back the target fails the real regression; rolling back parsing fails 13
cases. Root integration passed 93 focused cases including the legacy file-preservation fixture. The
lead independently verified all 19 reported worker test roots absent. Private reviewers will recheck
this delta and the corrected fixture at one common final candidate.

### Native test observation correction

The next integrated run at `cfe250ce` passed 10,879 cases with 11 skips and one new real-tmux
fixture failure. Its independent native query reported a dead pane with no exit status before
calling the production helper. The runtime contract already permits an unavailable native status. A
private diagnostic reproduced this with an inherited blocked child-exit signal, and an ordinary
parallel repeat also observed absent status. The failed full-run worker's signal mask was not
captured, so that run's cause is not asserted. Test-only correction `b317fb03` requires the intended
exit marker, actual native death, no reported signal, and agreement with native status observations
bracketing the helper. Explicit-status cases still require preserving status 23. Production code is
unchanged. The corrected 115-case selection and the masked real-tool case pass; target and parser
rollback controls still fail. The lead independently verified all 28 newly reported fixture and
diagnostic roots absent. Final integrated acceptance and the independent backend report remain
separate gates.

### Final local validation for round 6

All three private lanes cleared runtime candidate `cfe250ce` and final test correction `b317fb03`.
Their final narrow selections passed 40 project cases, one real-tool Muntz case and four independent
correctness cases. Earlier runtime selections passed 82, 82 and 78 respectively; these overlap. The
final full pytest log reports 10,880 passed and 11 skipped in 581.11 seconds. Its execution receipt
was lost during tool-output truncation, so no independently observed local process exit code is
claimed for that run; the exact pushed head's required CI remains a handoff gate.

Ruff lint and format returned 0 across 945 files, and CI-exact mypy returned 0 across all 909
source/test files. File lint returned 0 after formatting this record and spelling out the child-exit
signal name. Rulesync drift, locked-SDD validation, Typer isolation, source/wheel builds, Python
website checks (160), Node website checks (103), deterministic builds for both site bases and five
isolated CLI drives returned 0. The latter unchanged-source checks remain scoped to their recorded
candidate; no backend or model acceptance is inferred. The operator contract is unchanged by the
final test correction, and permanent collateral remains current.

The 26-requirement source and permanent-promotion audit finds no material documentation gap. The
remaining unchecked boxes retain actual-scope acceptance, final delivery, requirement reconciliation
and locks. Historical handoff evidence does not close composite live obligations. Both locks remain
absent pending the independent full report and final requirement-by-requirement reconciliation.


## Integration-report correction round 7, 2026-11-06

The operator directed continuation after report `6676063798` and increased the next integration
report window to six hours from its ready handoff. A report arriving earlier may be assessed and
acted on immediately. Start comment `6676418997` freezes the batch at 2026-11-06 03:53:58 UTC,
against `b14302bc`; main remains `c8d4dfb3`. The PR returned to draft before production edits.
This is new authenticated direction following the earlier exhausted allowance.

The report supplies the missing exact-head pytest exit receipt: 10,880 passed, 11 skipped, exit 0.
It also establishes substantial actual-scope acceptance across the reported Linux, macOS and Windows
beds. Unchanged positive cells retain their exact head/config; they do not certify changed failure
paths or waive remaining acceptance. Three blockers and the additional material findings prevent
both locks. The artifact companion tracks this same joint correction through this section.

- [ ] Correct routing evidence coverage for restored, added and unreadable routed inputs, and
      receiving-facet/inspection health without an ancestor freshness gate or new codec.
- [ ] Preserve recoverable managed session ownership after post-spawn uncertainty, keep socket and
      identity recovery coherent, and protect resolved secrets on create/start/restart diagnostics.
- [ ] Use definitive Proxmox destroy-task acknowledgement, refuse a different recorded guest name,
      and remove pre-delete guest mutations. Binding failure retains the row; force does not imply
      consent. Preserve the ongoing execution effort's separate trust and HTTP-error work.
- [ ] Preserve unavailable native entries, name durable diagnostic locations, and report unsupported
      interactive Grok personas as unhandled rather than claiming application.
- [ ] Make clone cleanup independent, retain occupied reservations and keep private attempts private
      during ACL repair. Surface residual-attempt recovery limits without automatic destructive
      reclamation or a new journal.
- [ ] Disposition every reported should-fix and incidental finding against its current caller,
      including exact Transport/SSH overlap, and update permanent collateral with actual behavior.
- [ ] Strengthen the identified behavioral regression boundaries and native CI prerequisite, run all
      three private lanes and required integrated gates, and publish one exact green ready handoff.
- [ ] Wait up to six hours for the next full integration report, assess earlier arrival promptly,
      and reconcile every retained requirement before final promotion and either lock.

Independent scouts confirm that input IDs include content digests, so an old non-null input table is
not confirmation of a changed capture. They also reproduce the dead-socket permission wedge and
unverified-create record removal. Proxmox's successful destroy task already confirms the operation;
its pool-scoped token can legitimately lose audit permission immediately afterwards. Existing
provider name evidence can guard different-name VMID reuse, but is not an immutable incarnation or
race-free identity promise. These scoped facts guide the corrections; local reproductions alone do
not complete the real-backend acceptance requirements.
