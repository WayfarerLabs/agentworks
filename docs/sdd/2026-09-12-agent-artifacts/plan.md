# Agent artifacts: implementation plan

## Current stage and ownership

The operator approved the FRD and HLA on 2026-09-13 and authorized the complete implementation on
PR 794. This includes workstation/Git acquisition, the native support matrix and a launch error for
artifacts still unhandled at the session. The effort lead owns architecture, sequencing and this
plan. Implementation proceeds as one complete delivery on the existing branch; local work units and
commits provide internal checkpoints, with one completed implementation push before handoff.

Keep predecessor closeout separate. Its implementation is merged, but seven acceptance/cleanup
checkboxes remain open in the
[harness-scope-framework plan](../2026-09-06-harness-scope-framework/plan.md#acceptance-and-closeout).
Complete and record that acceptance before locking it, unless the operator explicitly changes its
remaining obligations. A historical lock does not prevent this effort from changing framework code.
Do not change the saga's existing ledger or contracts; route coordination to their owner.

## Delivery

Operator decision, 2026-09-12: keep requirements, research, HLA and the entire implementation on
branch `sdd/agent-artifacts` and [PR 794](https://github.com/WayfarerLabs/agentworks/pull/794). This
supersedes the earlier plan and handoffs that treated the seed as a separate merge increment. No
separate design merge is planned; the operator does not see a coordination need for one here.

Keep the PR draft while work remains. Use `review-requested` for a coherent HLA checkpoint on this
same PR, and remove it before incorporating changes or continuing implementation. Requirements and
HLA approval remain explicit checkpoints; sharing a PR does not approve either. Carry
`sdd:agent-artifacts` and `saga:next-steps` throughout. Mark ready without `review-requested` only
when the implementation and required validation are complete and the whole PR is ready to land.

## Requirements and research

- [x] Carry forward operator decisions on hints, package normalization, origin, facet ownership,
      lazy passthrough, VM-to-session fallback, shell placement and idempotent cleanup.
- [x] Identify predecessor acceptance separately from future framework changes and record the
      successor's proposed first delivery and open decisions.
- [x] Obtain operator acceptance of the FRD scope and resolve first-delivery source choices.
- [x] Incorporate operator terminology corrections and research the agent persona naming proposal;
      record the recommendation in [prior-art research](prior-art-research.md). Persona naming
      remains pending agreement, and this narrow pass does not complete the research below.
- [x] Record the operator's terminology decision: **agents** is shorthand for **agent personas**.
      This supersedes the pending-agreement status recorded by the preceding research milestone.
- [x] Recover the predecessor's acquisition safeguards into the FRD and prior-art research, with the
      historical commit reference and concrete byte-preservation cases. This preserves design
      context; it does not claim implementation or completion of acquisition research.
- [x] Write `prior-art-research.md`: evaluate Rulesync's model and generators, Agent Skills package
      conventions, source acquisition options, and the actual native artifact mechanisms of each
      shipped harness. Cite primary sources and mark unsupported claims explicitly.
- [x] Resolve the final-session disposition with the operator and present the native support matrix.
      State the handling criteria for shell publication and each native artifact delivery mechanism.

## Design checkpoint

- [x] Write an HLA covering bundle resources, acquisition and capture, normalized representation,
      ownership and lazy routing, integration APIs, native placement, final disposition and cleanup.
      It includes `agw artifacts show`, the core-to-integration pipeline and the later producer
      boundary.
- [x] Include worked manifests and the VM/user/workspace/session flow, with inactive facets,
      multiple consumers, duplicate-route prevention and session name reuse.
- [x] Check the saga's `session_uuid`/`run_id` contract and implementation ownership with the saga
      lead before selecting durable session artifact ownership in the HLA. Do not introduce a
      separate artifact identity to fill an unowned session-identity dependency.
- [x] Record migration from the merged facet API and existing native setup without reintroducing
      speculative reconciliation machinery or source-specific propagation formats.
- [x] Run independent project and complexity reviews, complete the applicable checks, and publish
      the HLA checkpoint for operator and saga review.
- [x] Obtain HLA approval, then replace the initial implementation outline with concrete work units,
      necessary LLDs, acceptance scenarios and delivery boundaries.

## Implementation sequence

All units ship together on PR 794. The lead integrates bounded developer branches locally, updates
this plan as evidence completes each unit, and keeps `review-requested` off during implementation.
Do not push delegate branches or partial implementation checkpoints. The next published head must
contain the full implementation and collateral, followed by standard ready handoff.

### 1. Shared contracts and migration

This effort owns the early identity prerequisite under the existing saga contract, which explicitly
permits that slice to land early. Main has no identity implementation, and PR 770 remains a design
checkpoint. The lead owns identity and session publication together; no containment or event-stream
contract is adopted here. The
[coordination message](../2026-08-04-next-steps/message-2026-09-13-agent-artifacts-identity-implementation.md)
records this implementation assignment for the saga and dependent efforts.

- [x] Resolve the existing session-identity ownership dependency against current saga and child
      work; record the implementing slice without changing another effort's requirements.
- [x] Write [source and codec LLD](source-codec-lld.md): declarations, source selection, capture
      bounds, text/byte rules, typed inputs, identity and versioned persistence.
- [ ] Write [native delivery LLD](native-delivery-lld.md): typed facet results, native
      paths/options, ownership claims, private session publication and compatibility checks for each
      integration.
- [ ] Implement the agreed durable `session_uuid` and per-incarnation `run_id` slice, including
      migration of existing rows, create/restart/name reuse and backup/restore preservation. Keep
      native conversation identity independent; do not add containment or event-stream machinery.
- [ ] Bump the harness integration contract and update first-party hooks together. Old hook returns
      cannot silently acknowledge artifact inputs; artifact-free stored evidence remains readable.

### 2. Bundle resources, capture and normalization

- [x] Register `artifact-bundle` as an ordinary declared resource and add `artifacts.bundles` to VM,
      admin, agent, workspace and session declarations, resolved templates, reference discovery and
      schema generation. Omission inherits, a supplied list replaces, duplicates fail, and resolving
      a declaration never acquires its source.
- [x] Extend shared source handling for bounded workstation and Git package capture. Use immutable
      commit/object reads with workstation auth, consistent per-operation Git resolution and
      complete selected packages. Do not run hooks, filters or bundled scripts, or omit
      export-ignored files.
- [x] Preserve standard skills and persona metadata, supporting paths, executable intent and binary
      bytes. Normalize designated UTF-8 text to LF; make `preserve_bytes` concrete and exclude
      `SKILL.md` from that override. Reject unsafe paths, portable collisions, links, special files,
      local Git metadata, selected submodules, unresolved LFS pointers and observed local mutation.
- [x] Deliver one immutable normalized input representation and lossless bounded state codec with
      content identity independent of source provenance and consuming origin.
- [x] Prove source/normalization contracts with local fixtures: mutable refs vs commit pins,
      repository subpaths, complete packages, CRLF/lone CR, executable scripts, text-like binary
      fixtures including ASCII-only PDF, byte preservation, capture failure and temporary cleanup.

### 3. Core capture, routing and applied state

- [ ] Capture owner-local inputs in owning operations even without activated integrations. Commit
      new-owner captures with their rows; replace existing captures only after successful capture.
      Session consumers never fetch or initialize ancestors. Workspace refresh requires recreation.
- [ ] Extend the existing instance-state domain and codecs for core captures, applicable input
      identities, typed deferrals and session applied evidence; preserve existing plugin claims.
- [ ] Prepare completed env and applicable artifact inputs before each integration. Validate plugin
      results for unchanged origin, input membership, one legal next facet and no duplicate routes.
- [x] Implement lazy resolution through the actual VM, user, workspace and session path. An inactive
      VM routes directly to session; inactive intermediate facets pass through; handled payloads do
      not reach descendants. Results remain reusable independently for every descendant owner.
- [ ] Base freshness only on supplied inputs and existing config/env dependencies. Diagnose missing,
      stale or interrupted active results and retained artifact effects awaiting retirement without
      changing unrelated required/recommended setup semantics or blocking parent deletion.
- [ ] Prove routing with simple test integrations: all VM destinations, inactive/unimplemented
      distinctions, the diamond, multiple integrations/consumers, origin preservation, malformed
      results, branch-specific freshness, interrupted setup and removed activations.

### 4. Native delivery and owned cleanup

- [ ] Implement idempotent whole-file publication/retirement using existing transport and applied
      state. Preflight destination/name collisions, preserve unowned or drifted content, checkpoint
      confirmed effects, and retry partial work without an acknowledgment ledger or generic engine.
- [ ] Implement shell publication for all types at user/workspace/session, with fixed ancestor
      locations and the session-only index exposed through `AGENTWORKS_ARTIFACTS_DIR`.
- [ ] Implement Claude user/project rules, skills and agents; session additive prompt content,
      private plugin skills with explicit namespace, and agent definitions through native options.
- [ ] Implement Codex user/project skills and agents, with hints/rules deferred to session; compose
      session developer instructions and private persona config. Refuse private session skills
      explicitly and keep primary-persona limitations honest.
- [ ] Implement Grok Build user/project rules, skills and agents, plus session rules/personas; honor
      discovery exclusions and refuse unsupported interactive session skills explicitly.
- [ ] Validate artifact-specific native options and reject launch overrides that disable or replace
      generated carriers. Verify native discovery and resume behavior against actual executables,
      without model calls or external marketplace dependencies.

### 5. Session lifecycle and inspection

- [ ] Integrate prospective session/run identities, completed env and prepared artifact inputs into
      the launch decision. Reject final deferrals before stopping an existing workload.
- [ ] Persist ownership before publication; stage each restart in a distinct private run directory,
      preserve the old workload on preparation failure, retire old files after it stops, and clean
      failed creates/deletions idempotently without adopting same-name replacement files.
- [ ] Implement `agw artifacts show` with actual-lineage selectors and optional integration filter.
      Include local and ancestor declarations, upstream-handled artifacts, capture provenance,
      recorded native identity/placement and clear missing/stale/interrupted evidence.
- [ ] Prove inspection does not acquire sources, invoke integrations, contact remotes, expose bodies
      or secrets, or mutate state. Reject contradictory selectors and preserve admin/agent
      separation.
- [ ] Exercise create, restart/resume, failure before teardown, deletion, source/reference removal,
      name reuse and two actual users sharing one workspace. Verify private paths and safe cleanup
      from observed state, not only return codes.

### 6. Collateral, review and delivery

- [ ] Update permanent source/capability/state docs, operator guides, sample bundle/owner manifests,
      CLI help/completions, generated resource schemas and migration guidance with shipped behavior.
      Include the native support matrix, capture/refresh rules and VM-plus-user activation example.
- [ ] Run required repository/CLI gates and relevant behavioral tests, package/build checks and
      deterministic website builds. Record actual exits and distinguish local, CI and live evidence.
- [ ] Run independent project, Muntz and general correctness/security reviews on isolated pinned
      work trees; critically assess and resolve material findings before handoff.
- [ ] Run scoped live acceptance using an operator-provided inventory and budget; snapshot first,
      exercise the public CLI and native placement boundaries, and independently verify cleanup.
- [ ] Audit R1-R10 and the complete approved matrix against evidence, complete owned cleanup and
      record remaining limitations honestly. Lock this SDD only when its work is actually complete.
- [ ] Push the complete implementation once, update the PR around shipped behavior and evidence, and
      mark ready without `review-requested`. Do not merge or enable automatic merging.
- [ ] Monitor the ready handoff under the standard feedback window. Up to three PR feedback/fix
      rounds are authorized by the 2026-09-13 implementation direction; each changed head repeats
      private review, validation and proper handoff. Escalate scope changes or unavailable evidence.

## HLA checkpoint evidence

The operator approved the FRD/HLA at `b2ba177a`, following one published feedback/fix iteration and
the requested artifact-only diagram amendment. Project and Muntz private reviews inspected the
rendering; saga and Muntz PR reviews were clean with no unresolved threads. Local document gates and
rendering passed. GitHub CI remained queued and was explicitly disclosed under the operator's
direction to continue the design checkpoint. That exception does not claim implementation
validation.

## Seed review evidence

Independent project and complexity reviews of `7c744828..5fff8a0c` found no material issues. The
project review's minor signature request was incorporated into the saga coordination message. Both
reviews confirmed that new scope remains proposed and the predecessor's seven acceptance/cleanup
obligations remain open. No code or live resources were changed. The seed handoff records repository
checks and final-head CI separately; this is not artifact implementation or acceptance evidence.
