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
migration review units before the combined handoff. This remains a draft implementation checkpoint
until live acceptance is complete; the earlier design-only merge intent no longer applies.

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
output use the same cached selected model. A no-config answer permits a name-only attachment and
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
- [x] Reconcile removed attachments using prior ownership with absent desired config, including
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
      administrator attachment placement and multiple configured integrations.
- [x] Document custom silent-session-lineage migration and existing instance recreation remedies;
      never infer an integration from its stored conversation namespace.
- [x] Update capability, harness integration, env, instance-state and idempotency documentation with
      implemented contracts; remove obsolete core-Claude ownership descriptions.
- [x] Update generated schema/reference/completion and guide surfaces through their owning
      mechanisms, with behavior evidence for each new field rather than prose-pinning tests.
- [x] Promote load-bearing architectural conclusions into permanent homes with their implementation.

## Acceptance and closeout

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
- [ ] Exercise repeat setup, one-entry removal, whole-attachment removal, interruption, concurrent
      mutation, native drift, unknown codec versions, failed cleanup and owner deletion (R9).
- [ ] Prove failed workspace setup unwinds under standalone and session-created paths and retry
      refuses unexplained residue (R13).
- [ ] Run full repository gates and focused native Windows tests from the current CI selection;
      record actual commands, exit codes and unreachable live surfaces.
- [x] Obtain independent project, complexity and generic correctness/security reviews, resolve
      material findings, and validate the final integrated head.
- [ ] Publish a coherent implementation handoff and consume published feedback only within operator
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
