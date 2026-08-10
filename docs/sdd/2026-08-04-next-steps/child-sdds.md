# Child SDDs

- Status: Active ledger
- Last updated: 2026-08-10

This is the saga's tracking document, the analog of an ordinary SDD's `plan.md`. Completed
checkboxes are immutable records, per the standard rule. The saga SDD locks when every entry here is
locked.

## Ledger

### Wave 1: 2026-08-05-deprecation-removal

- [x] Seeded (FRD, PR #401, reviewed with findings applied)
- [x] FRD merged to `main` (2026-08-05)
- [x] Picked up by an effort lead (HLA, plan, residual inventory; PR #406)
- [x] Implementation complete, pre-roadmap SDD closeouts done (PR #406, 2026-08-05: all five
      pre-roadmap SDDs locked, plan 46/46, docs promoted including ADR 0020)
- [ ] Ships in 0.14.0 with phase 1 (cut held for the guide first slice per operator ruling,
      2026-08-06; see `phasing.md` release mapping)
- [x] Locked (`locked.md` on `main` via PR #406)

### Wave 2 (adopted child): 2026-07-31-declarative-schema

- [x] Phase 1 (TOML sunset) merged to `main` (PR #316, 2026-08-05)
- [x] Seed notes published for the effort lead (delivered as
      `docs/sdd/2026-07-31-declarative-schema/roadmap-seed-notes.md` via new-file message passing;
      the adopted-child analog of a seed FRD)
- [x] Effort lead launched (2026-08-05, seed notes merged via PR #411)
- [x] Phase 2 released from its hold (prerequisites landed via PRs #405 and #406; the effort lead's
      first commit records the release in that SDD's plan)
- [x] Phase 2 implementation complete (absorbs generic-discriminator removal and the migrator's
      fate)
- [x] Five-pass roadmap review of PR #414 delivered (2026-08-07): request changes, three blockers,
      structural core verified sound
- [x] Migrator deleted per the remediation-posture ruling (operator, 2026-08-07; see
      `target-state.md` compatibility posture): both halves of `migrate/` removed, error hints
      repointed at guide-led remediation, `locked.md` records the ruling
- [x] Surviving review findings fixed and re-verified (execution re-review 2026-08-07: all fourteen
      items fixed and pinned; final round closed the marker-refusal reachability gap and landed the
      settings references; two operator rulings recorded in place, settings hard errors and the
      config deprecation-channel keep; the manifest channel-gap item in `target-state.md` stands)
- [x] Merged to `main` and locked (PR #414, 2026-08-07; `locked.md` binds at merge)

### Design track: capability-kind descriptor contract

- [x] Contract artifact drafted (`capability-descriptor-contract.md`, 2026-08-05)
- [x] Reviewed and merged to `main` (PR #405, 2026-08-05)
- [x] Consumed by wave 2 seeding (seed notes delivered via PR #411; effort lead launched 2026-08-05)

### Design track: scope participation (formerly facet-model boundary)

- [x] Contract artifact drafted (`facet-boundary-contract.md`, 2026-08-05; gates the wave 4 and wave
      5 seeds)
- [x] Recast as `scope-participation-contract.md` per operator simplification (2026-08-05): setup
      pipeline, per-scope init methods, trust-based scope discipline, grants dropped, facet
      vocabulary retired
- [x] Facet returns as a plain noun (2026-08-06, operator ruling): the level a capability is driven
      at (vm, user, workspace, session), pairing methods and config; core owns the scope-to-facet
      mapping; the retired machinery meaning stays dead
- [x] Reviewed and merged to `main` (PR #407, 2026-08-06; staleness caught by integration review
      2026-08-08, reconciled in this round)

### Onboarding and discovery (destination 1): 2026-08-05-onboarding-and-discovery

- [x] Seeded (FRD, PR #413, 2026-08-05; runs parallel to wave 2 per `phasing.md`)
- [x] FRD merged to `main` (2026-08-06)
- [x] Picked up by an effort lead (HLA, plan, prior-art; design PR #421 merged 2026-08-06)
- [x] Phase 1 (guide core, safe projection, verification surfaces) reviewed: three-pass roadmap
      review of PR #428 (2026-08-07, request changes, six blockers), fix round re-reviewed by
      execution and approved (2026-08-07); all mutations pinned, gates green
- [x] Merge order ruled (operator, 2026-08-07, confirming the effort's D7): wave 2 merges first,
      #428 rebases once, re-homing its probe-suppression mechanism onto the descriptor-derived
      dispatch as anticipated by its phase 4
- [x] Phase 1 merged (PR #428, 2026-08-08): the post-wave-2 rebase carried all reviewed commits
      byte-identical, and the round-3 projection-growth follow-up was re-reviewed by execution with
      both new structural pins mutation-verified; the `guide-contributions` always-on rule is now in
      effect
- [x] Machine-readable output phase merged (PR #462, 2026-08-10) after an operator scope correction
      (2026-08-09: the doctor database-copy/hostile-filesystem snapshot subsystem removed as an
      unpriced contract — "totally not a good trade") and a clean-slate round; final shape is one
      domain fact record serialized for human and JSON doctor alike, errors on the ordinary stderr
      route, and non-migrating doctor kept as authorized behavior at essentially zero marginal cost
      (operator ruling, 2026-08-09). Multi-round saga-lead and integration reviews converged with
      mutation-verified fixes (UserAbort re-raise, malformed-schema fail-closed, restored VM-state
      warning); the plan's checked history restored per the plan-history ruling (see Standing
      process rulings)
- [ ] Remaining phases (bootstraps including the README bootstrap that gates 0.14, wave 2 adoption,
      closeout) per the effort's per-phase PR plan
- [ ] Locked

### Wave 3: 2026-08-07-secret-sources

- [x] Seeded (FRD, PR #443, 2026-08-07; unblocked by wave 2's landing, carries the map-keyed
      descriptor amendment as R8)
- [x] FRD merged to `main` (PR #443, 2026-08-08; ownership transferred to the effort lead, whose
      design review converged the same day on PR #453, the conventional-prefix successor of PR #452,
      recording the operator's 0.14 hard-break ruling for direct backend references)
- [x] Picked up by an effort lead (HLA, plan, LLDs; design converged on PR #453)
- [x] Implementation complete and verified by execution across multiple review rounds (two-level
      model, synthesized sources, resolution API, singleton exception removed, map-keyed hosting,
      relocation, 0.14 hard break round-tripped on the installed CLI)
- [x] Secret-memory contract narrowed by operator scope correction (2026-08-09): the
      frame-erasure/ownership-fence machinery unwound to main parity; the durable contract is
      no-persistence, no-argv/logs/exception-objects, late resolution, stdin delivery, with
      in-memory retention explicitly best-effort (process as trust boundary; recorded permanently in
      that SDD and `capabilities/vm_platform/README.md`)
- [x] Provider-retained payload class closed by enumeration (2026-08-10): all five platform surfaces
      (Lima YAML, WSL2 and Proxmox staging, Azure custom_data, AWS UserData) key-free or
      hygienically transient, each pinned by a retention test on the final submitted artifact;
      post-boot stdin join unified across azure/aws/lima/initializer/rekey
- [x] Live remote-Lima acceptance passed with a rotated key (at head 383c0050, 2026-08-10; zero
      residue, key deregistered; evidence pinned in that SDD's lock record)
- [x] Saga-lead approval on the honest lock (2026-08-10); plan-history restore per the ruling and
      the #462 conflict integration ride the final push
- [x] Merged (2026-08-10) with the plan-history restore and the #462 conflict integration; the
      saga-lead spot-check confirmed both seam suites (UserAbort pins, five-platform retention)
      green at the merged head. The breaking reference slice's 0.14 gate is satisfied
- [x] Post-merge follow-up delivered and merged (PR #469, 2026-08-10): the enforcement suite's
      hand-duplicated manifests replaced by signature-derived semantic discovery (root domain
      independent of the protected edge; seam matching by object identity, a same-named fake fails
      collection), and the suite parallelized (pytest workers with per-worker log isolation). Two
      review rounds (saga lead + integration, both mutation-verified); CI Python checks fell from
      6-7.5 minutes to about 2 (worst case 7m38s to 2m21s)
- [x] Locked (`locked.md` merged with PR #453, 2026-08-10; binds at merge per the sdd skill)

### Installer plugins (pre-0.14 core slimming): 2026-08-07-installer-plugins

- [x] Ruled into roadmap scope (operator, 2026-08-07): misc core installers move behind system
      plugins before the 0.14.0 cut, with a first-class disabled-error experience
- [x] Seeded (FRD, PR #440, 2026-08-07; launchable whenever, with the moves consuming wave 2's
      descriptor registration and the guide topics consuming onboarding's first slice)
- [x] FRD merged to `main` (PR #440, 2026-08-08; ownership transferred to the effort lead)
- [x] Picked up by an effort lead (inventory-first: R1 inventory draft PR #451 in phased artifact
      review, three saga-lead findings pending as of 2026-08-08)
- [x] Design set reviewed (2026-08-09, endorse with conditions at head c5696f54: HLA, plan,
      migration strategy, and the resource-disable LLD verified code-grounded; all three R1
      inventory findings genuinely resolved; conditions are the saga-rename absorption on rebase,
      two LLD self-contradictions, and naming the reserved-built-ins-become-replaceable consequence;
      the shadow-validation machinery question carries a trim-unless-justified presumption)
- [ ] Implementation complete (moves, disabled errors, guide topics, upgrade-guide step)
- [ ] Ships in 0.14.0 (gates the cut per `phasing.md` release mapping)
- [ ] Locked

### Dispatched task (not a child SDD): git-credential variant restructure

Dispatched via branch-seeded task brief rather than an SDD (the sdd skill's message-passing
mechanism); tracked here because its outcome gates the 0.14.0 cut per `phasing.md`.

- [x] Ruled and dispatched (operator ruling 2026-08-08: git-credential joins the variant contract
      before the cut; brief seeded on the task branch, 2026-08-08)
- [x] Survey delivered and re-evaluated under the three-tier refinement (2026-08-08; dissolutions
      and restructures proposed per item)
- [x] Implementation merged (PR #455, 2026-08-08): token union with stored-arm default and contract
      v2, github repos/owner dissolved to scope union, install-command multi-test AND semantics, env
      structural union with null-companion canonicalization shared by validation, extraction, and
      fill, the EnvEntry construction shim removed by operator ruling, and the three-tier rule
      codified in `cli/agentworks/capabilities/README.md`
- [ ] Ships in 0.14.0 (gates the cut per `phasing.md` release mapping)

### Dispatched task (not a child SDD): concise operator content

Operator-dispatched directly to the former wave 2 agent (2026-08-09, before the message-signature
and coordination rules landed; the coordination gap it exposed fed the operator-responsibilities
discussion, still open).

- [x] Reviewed by the saga lead (execution pass; fixes verified) and merged (PR #463, 2026-08-09)

### Dispatched task (not a child SDD): manifesto consolidation

Brief seeded on `docs/manifesto` (2026-08-09); picked up by the `agw-manifesto` session
(2026-08-10). Conviction-voiced content gets one home (`docs/manifesto.md`, renamed from
`why-agentworks.md`), a `concept-manifesto` guide topic, and a development-principles clause; the
operator personally rewrites the assembled text before merge (rough-cut/placeholder expectation
pinned in the brief). Merge-gated on wave 3; the rename updates the website's canonical-source
selection in lockstep (CI enforces).

- [x] Ruled and brief seeded (2026-08-09; website-coordination constraint updated 2026-08-09 after
      the website Phase 4B design landed; rough-cut expectation added 2026-08-10)
- [ ] Survey, rough cut, operator rewrite, merge

### Dispatched task (not a child SDD): safer migrations

Brief seeded on `feat/safer-migrations` (2026-08-10); picked up by the `agw-safer-migrate` session.
Table-stakes migration UX (operator ruling, 2026-08-09): pre-migration notice with backup offer
(auto-backup in non-interactive with config opt-out), backup and restore CLI commands, and
migration-failure messages that print the exact restore invocation. SQLite online backup API; the
FRD frames the value honestly (wrong-migrations and version rollback, not crash recovery). Scope
discipline pinned in the brief against the twin-correction failure mode; the CLI command home
coordinates with the pending resource-CLI grammar decision.

- [x] Ruled and brief seeded (2026-08-10)
- [x] SDD, implementation, review complete; merged before the 0.14.0 tag (design PR #472 and
      implementation PR #478, both merged 2026-08-10 after multi-round saga-lead and integration
      reviews: canonical version-shape qualification closing the late-inspector race, exact-shape
      restore validation, mandatory suppression-surviving stderr notices, non-mutating completion
      probes, and a mutation-pinned benign-contention witness; the effort's SDD locked at merge).
      The safer-migrations release gate is satisfied

### Not yet spawned

Planned children, seeded when their prerequisites land (see `phasing.md`):

- Wave 4: harness scope framework
- Wave 5: session observability phase 1
- Wave 6: agentic artifacts and distillation
- Wave 7: structured control
- Wave 8: external plugin API
- Pre-0.14 test-consolidation child (operator ruling, 2026-08-09): an aggressive trim pass runs just
  before the 0.14.0 cut, soft-gating the tag; the saga lead's accumulating dossier (including the
  wave 3 enforcement-suite structural pins, whose deferral comments name this child as owner) is its
  R1 input. The closeout-wave pass remains the final sweep.
- Resource-CLI grammar child (pending operator decision): `describe-kind` becomes `explain`
  (matching kubectl's verb), a top-level `agw graph` command owns all relational views with
  focal-node, kind-filter, direction, depth, and format axes, `describe`'s fate is an open A-or-B
  (remove, or rebuild as the kind-aware card with a per-kind detail hook), `--write` semantics
  unify, and the CLI-hygiene audit bundle rides along; breaking, so it shares the pre-0.14 window
- Security-architecture doc child: seeds after wave 3 merges; carries the per-platform
  durable-surface inventory (what each provider retains) so provider-boundary reviews check a list
  rather than rediscovering the class incident-by-incident (lesson from wave 3's three-round class
  discovery)
- Closeout wave: comprehensive security, test-consolidation, and cleanup review (gates the lock)

Not children of this saga (explicitly out of scope, see `target-state.md`): the
named-console-template selector SDD, the herdr effort, the companion-shell and resilient-attach
wins, and the agentworks.build website (`docs/sdd/2026-08-07-website/`, seeded 2026-08-07; the saga
lead seeds and reviews it like a child, but it does not gate the saga lock). They proceed
standalone. Also adjacent standalone (saga-lead-seeded and reviewed, not gating the lock): the `gcp`
vendor-bundle platform effort (PR #479; standalone by its seed ruling; vendor plugins may grow
beyond their first capability implementation per the operator ruling recorded in its FRD), the
bootstrap-fallback-removal effort it spawned (PR #475, merged and locked 2026-08-10, closing
[issue 471](https://github.com/WayfarerLabs/agentworks/issues/471): the generic secret-bearing Phase
A fallback deleted and all platforms cut to contract v2), and the continuous-lander website feature
(PR #486, stacked on #439). Website status (2026-08-10, updated at merge): PR #439 MERGED and the
interim release is live at the default Pages URL. The five-page topology (Home, generated Manifesto,
Security, 404, and the operator-authorized dedicated Lander page) shipped with the operator's
Chrome/Edge acceptance recorded and the residual deferred matrix explicitly accepted on the PR;
domain activation follows the pinned rollback-capable runbook at the operator's choosing.
Post-launch obligations (Firefox/WebKit, spoken screen reader, physical touch/device rows) stay
explicit and unchecked, required before that effort's final closeout. The manifesto task's atomic
rebase obligation (manifesto source path plus Home block re-pins) is now active, and the
continuous-lander stack entry moves to main.

## Standing process rulings

Permanent homes landed via PRs #473/#474/#477/#481: the sdd skill's supersession paragraph; the
dev-process draft/ready handoff, author-owned review-requested label, exact handoff definition,
subagent-review taxonomy, and commit/PR/stack layering with the cascade rule; and the saga-lead
review triggers matching them. (A previously recorded enforcement-calibration note was retracted by
the operator on 2026-08-10 as personal guidance to the saga lead, not process; the dev-process
poller rule stands as merged in PR #481, and PR #489 closed unmerged.)

- **Plan-history ruling (operator, 2026-08-10):** completed plan steps may never be removed — no
  exception for never-merged, superseded, or expunged work. Everything else strips clean under a
  scope correction (narration, correction framing, abandoned unchecked boxes, definitions of done,
  evidence prose, PR bodies). The sdd skill's checkbox rule stands as written; a clarifying sentence
  lands in the skill so this is not re-litigated.
- **Contract pricing (operator, 2026-08-09, from the twin scope corrections on #462 and #453):**
  adversarial verification verifies the contract — it never expands it. More than two or three fix
  rounds on one finding is a contract smell: stop and re-price the requirement with the operator
  instead of growing machinery. Reviewers price requirements, not just implementations.
- **Class sweeps (saga lead, 2026-08-10, from wave 3's provider-boundary rounds):** the first
  instance of a contract-violation class triggers enumeration of every implementation of the same
  seam, each verified on its durable surface, before the class is called fixed.

## Issue intake (2026-08-05)

A sweep of the open issue tracker pulled the following issues into the saga. Each rolled-in issue
received a comment pointing back here so it is not worked out-of-band (posted 2026-08-05); issues
not listed stay standalone.

| Issue      | Lands in                                                                            |
| ---------- | ----------------------------------------------------------------------------------- |
| #76        | Wave 1 SDD closeouts (stale lockfile half only; the derive audit stays standalone)  |
| #165       | Not rolled in: its own pre-saga SDD proceeds standalone (out of saga scope)         |
| #170       | Wave 2 rider (expires metadata belongs in the envelope/metadata modeling)           |
| #205, #212 | Design track: instance-state store and per-instance specs; future living-graph SDD  |
| #214       | Wave 2 (live samples, uniform validation; unknown keys go hard-error there)         |
| #242       | Waves 4 and 5 (harness-owned adoption plus resume re-pointing)                      |
| #257       | Onboarding-and-discovery child (machine-readable output contract)                   |
| #311       | Wave 2 (structural reference extraction from annotated models)                      |
| #370       | Wave 3 (resolution-API evolution owns the batching question)                        |
| #373       | Wave 4 (environment-appropriate defaults belong to per-scope integration config)    |
| #374       | Design track descriptor plus wave 3 (capability mandate; see `target-state.md`)     |
| #387       | Waves 4 and 6 (workspace-scope integration hooks and features own post-clone setup) |
| #390, #391 | Onboarding-and-discovery child (samples and plan A onboarding)                      |

Notes: #242 also picks up the 0.14 rename (it lands as `session resume --update-template`); the
capability-contract sibling cluster #368 through #374 splits, with #370/#373/#374 rolled in and the
platform-specific siblings staying standalone; #362 (codex enhancements) stays standalone as an
open-ended research placeholder.

## Immediate next actions

1. Done (2026-08-05): PR #316 merged; wave 0 complete.
2. Done (2026-08-05): saga PR #400 and wave 1 seed #401 merged; harvested
   `feat/harness-transcripts-sdd` branch deleted.
3. Done (2026-08-05): issue-intake comments posted.
4. Done (2026-08-05): descriptor contract merged (PR #405).
5. Done (2026-08-05): wave 1 implementation, closeouts, and lock merged (PR #406). The 0.14.0
   release ships when cut.
6. Done (2026-08-05): wave 2 seed notes merged (PR #411) and effort lead launched.
7. Done (2026-08-05): onboarding-and-discovery seeded (FRD, PR #413).
8. Done (2026-08-06): onboarding seed merged (PR #413). Launch its effort lead when ready.
9. Saga lead reviews wave 2 and onboarding PRs as they arrive.
10. Done (2026-08-06): scope participation contract merged (PR #407). Next design-track items: the
    instance-state store schema and the event vocabulary's first slice.
11. Done (2026-08-08): installer-plugins FRD merged (PR #440); its effort lead is active (R1
    inventory PR #451 in phased review).
12. Pre-0.14 gates in flight (2026-08-08, post-#455): the installer-plugins implementation (R1
    inventory PR #451 in phased review), the wave 3 breaking slice (design PR #453 converged,
    awaiting merge and implementation), and the onboarding README bootstrap (later onboarding
    phase). The git-credential restructure gate is satisfied (PR #455, merged 2026-08-08). The cut
    waits for the three open gates.
13. Gate status (2026-08-10, post-#462): onboarding's machine-readable output phase is merged (PR
    #462); the README bootstrap gate stays open. Wave 3 (PR #453) is saga-lead-approved with the
    lock honest, pending the #462 conflict integration, the plan-history restore, and the tester's
    final pass. Installer-plugins design is endorsed with conditions; implementation is next. Two
    new pre-0.14 gates joined: the safer-migrations dispatched task (operator ruling) and the
    test-consolidation child (soft gate). The resource-CLI grammar break shares the window pending
    the operator's describe decision.
14. Wave 3 merged (2026-08-10): the breaking-slice gate is satisfied. Unblocked by it: the manifesto
    dispatched task's merge-order constraint is cleared (it awaits its rough cut and the operator
    rewrite), and the security-architecture doc child can seed (carrying the per-platform
    durable-surface inventory). The wave 3 lead owes the post-merge follow-up (enforcement-suite
    consolidation plus the unit-test runtime fix). Open 0.14 gates: installer-plugins
    implementation, the README bootstrap, safer migrations, and the test-consolidation soft gate.
15. Safer migrations merged (2026-08-10): that gate is satisfied. Open 0.14 gates: the
    installer-plugins implementation (design endorsed 2026-08-09; no implementation activity since,
    the current long pole), the README bootstrap (the onboarding effort's Phase 3 design converging
    on PR #480 under the operator's lifecycle-assistance lens), and the test-consolidation soft gate
    (seeds when the hard gates land).
16. The onboarding bootstrap design gate closed (2026-08-10, PR #480 at 89b41755) after six
    convergence rounds: the release choreography adopts the normal feat-merge shape, the
    consent-teaching reconciliation sweep covers every shipped guide topic family, and the
    operator's R16 ruling keeps one full canonical assistance body as the only source. The last 0.14
    hard gate is in implementation.
