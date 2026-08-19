# Child SDDs

- Status: Active ledger
- Last updated: 2026-08-19

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
- [x] Shipped in 0.14.0 (released 2026-08-18; the cut had been held for the guide first slice per
      operator ruling, 2026-08-06)
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
- [x] Assistance phase merged (PR #480, 2026-08-13): always-available Agentworks assistance, with
      the operator's own simplification rounds on it and the R16 ruling that one full canonical
      assistance body is the only source
- [x] Simplification and trail-sign phase complete (operator rulings 2026-08-12, recorded in
      `target-state.md`; design PR #519 and implementation PR #537, both merged 2026-08-15): the
      no-topic guide is a catalog-free trail sign, `concept-onboarding` owns the walkthrough,
      selected topics fail soft on environmental failure with doctor as the health authority, and
      the release-test fix (PR #539) fixed every future release PR's CI
- [x] One-wave guide-value deletion complete (PR #556, merged 2026-08-16; ruling of 2026-08-15 via
      survey PR #543): all 141 command-duplicating blocks, the dormant kind fallback, runtime
      resource topics, and the directly orphaned machinery removed at net -2,948 lines, with the
      bounded parity-preserving projector replacing `_dynamic_topic`/generic-view assessment use.
      Tester passed the exact wheel; the consolidated edge round (collateral rules, 0.14 upgrade
      guide, locked-SDD discharge entry, structural consent tests, `assert_never`) landed at the
      merged head. The temporary gap stands on unreleased `main` only; the CLI grammar rewrite
      remains the hard 0.14.0 release gate
- [x] Corrected guide model shipped, satisfying its 0.14.0 gate (PR #579, merged 2026-08-17; ruling
      2026-08-16, full text in `target-state.md`): one shared eight-topic catalog, presentation-only
      mode differences, `AgentContract` replaced by one optional `AgentNote` carrying the operator's
      dozen journey hints, the assistant topic ordinary among nine, and the bootstrap at 107 words
      of install-and-handoff. All lanes plus the tester's exact-wheel pass converged; the round
      restored the secrets don't-display teaching and told the truth of the startup-posture drop
- [x] Markdown concept shells (operator ruling 2026-08-17, recorded in `target-state.md`; design
      checkpoint on draft PR #580): the typed guide model is replaced by auto-discovered Markdown
      shells with a closed two-addition list (agent fences and the bounded README-consuming import;
      the live projections were removed by same-day direction, leaving shells fully static); typed
      blocks, actions, consent/evidence replay, the onboarding assessment, and manual registration
      go. Saga checkpoint review posted with two review items (charter the outdated always-on rule
      updates per keep-collateral-in-sync; state the plugin-topic story). No 0.14 gate is minted for
      this implementation absent direction. Shipped as PR #587 (merged 2026-08-17) with the
      nested-container fix PR #591 (merged 2026-08-17; the remaining theoretical CommonMark edges
      closed by operator declaration on that PR)
- [x] Derived index and grammar-native guide shipped, satisfying its 0.14.0 gate (PR #593, merged
      2026-08-18; operator ruling 2026-08-17 in `target-state.md`): the no-topic response derives
      from the shell catalog through a reserved `_index.md`, and the public grammar is `agw guide`,
      `agw guide list`, and `agw guide show TOPIC` with no compatibility alias. The closing rounds
      carried the operator's content pass over every concept shell, a structural test validating
      authored command paths against the real CLI spec, and the restored data-versus-direction
      control in the assistant shell's agent-only fence (three lanes converged on it twice before it
      landed in full operational form with the self-reflective case). All lanes passed at the merged
      head
- [ ] Remaining phases (wave 2 adoption, closeout) per the effort's per-phase PR plan. The README
      bootstrap shipped with the assistance phase (PR #480), so the bootstraps gate is satisfied;
      the generated block pins version 0.14.0 or newer and resolves when the release ships
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
- [x] Scope cut back to the bucketing (operator, 2026-08-13; see `target-state.md`): "I literally
      just wanted to bucket the existing installers. Nothing more." The disabled-error experience is
      deferred as a nice-to-have. The design set endorsed above is therefore larger than the work;
      the effort lead trims the plan to the moves before implementing
- [x] Implementation complete (PR #536, 2026-08-15): all sixteen rows moved byte-identical into the
      opt-in `apt` and `install-command` plugins, the AC4 composite gate pinned through the real
      recipe path, guide loading request-scoped per the fail-soft contract. **This lane is closed**
- [x] Shipped in 0.14.0 (released 2026-08-18)
- [x] Locked (`locked.md` rode PR #536 and binds on `main`)

### Simplification pass (pre-0.14): 2026-08-12-simplification-pass

Adopted as a child of this saga on 2026-08-13 (operator). A seven-lane study of the 2026-08-06..12
merge window (68 PRs) found the core models sound and a large scaffolding tax around them: 119,584
lines of tests against 83,151 lines of `cli/` code, adversarial validation of first-party content,
tests that police form rather than behavior, and inert generality. The effort ships subtraction
only, on two doctrines: validate at boundaries and trust the interior, and tests assert behavior
while agreement is derived.

Adopting it collapses ledger structure rather than adding it. The pre-0.14 test-consolidation child
and the prose-test-purge child are both absorbed here, and the closeout wave's test-consolidation
item shrinks to a verification sweep.

- [x] Seeded by the effort lead (findings, FRD, HLA, plan, migration strategy; PR #509 in draft
      artifact review). The first draft was a 35-item coordinated program, cut by operator direction
      to three steps: rule adjustment, deletion waves, reassess
- [x] Prose-test-purge child absorbed (operator, 2026-08-12): the supersession header and directory
      lock landed on `main` with PR #509. The seed FRD's survey and guardrails carry forward into
      this effort's wave 1 deletion charter
- [x] Artifacts merged and authoritative (PR #509, 2026-08-14), with two known one-line nits
      accepted by operator disposition rather than a sixth review round: `VMIssueCode` actually
      lives in `vms/manager/inspect.py`, and the two absence checks read as scoped to production and
      test surfaces, this SDD's own findings excluded. The implementing lead folds both on first
      touch; the plan is mutable until lock
- [ ] Wave 1: nine deletion work items (per the pass's plan at HEAD; the W1 website-workflow
      replacement stands alone beside the sweep), unordered by design, each judged locally and green
      on the full suite. How many PRs they land as is the child's call: its plan already allows the
      prose sweep to span several, and the saga lead has recommended batching by domain to about
      three so the round count matches the work rather than the item count
- [x] Wave 0 closed on branch (a) (PRs #515/#518, 2026-08-14): the twelve broad rules deliver
      unconditionally, verified by fresh-session and worktree probes; issue #511 closed, with the
      recorded limit that rules propagate at session boundaries, not merge boundaries
- [ ] Wave 1 in progress: the phase7 corpus deleted with the caller-supplied boundary check landed
      (PR #523), criteria sharpened and executable (PR #524), fifth trust boundary ruled (PR #533);
      remaining items run against the merged criteria
- [x] Wave 1 interior-secrets item closed (PR #546, merged 2026-08-16): the trust-boundary reduction
      with the two exact interaction-policy entry checks kept as their own reach-buying category
      (the locked note's position-scoped three stand), both retained name screens now
      mutation-protected, and the S2/S7 findings corrected in place; `ResolutionPreview.name`'s
      ordering fragility filed as #554 rather than absorbed
- [x] Wave 1 descriptor-generality item closed (PR #560, merged 2026-08-16): `registry_policy`,
      `kind_strategy`, `manifest_section`'s optionality, and the dead fallbacks deleted; the
      tester's boundary-5 catch closed at the conformance seam with a typed registration refusal of
      non-callable `config_for`; `contract_version` withdrawn from all waves by ruling 12; the dead
      discriminator-branch family closed around one type-enforcing guard. This merge, with #531,
      satisfies the operator's harness-integration precondition: the window is open until the
      grammar rewrite's breaking PRs land. The saga's descriptor-contract amendment rides this round
- [x] Wave 1 guide and machine-output items closed (PR #548, 2026-08-16): G8 dead surface, G2
      typed-to-dict round trips, G6 defensive surface, and the G11 parity gap, with the stdout write
      loop restored as real short-write protection and struck from the finding that had
      misclassified it. The onboarding effort's guide-contract LLD supersession landed as its own PR
      #551 through that artifact's owner. The website batch unblocked when PR #486 merged
- [x] Wave 2 complete (PR #570, merged 2026-08-16, closing the skills remainder after
      #521/#535/#538/#540, with #540's follow-up absorbed by #549): the transcribed review protocol
      collapsed to a pointer with the drifted blocker clause resolved on the qualified side, six
      journey passages and seven defensive negations trimmed, principle 1 compacted with the
      consistency review restoring "complexity is what makes software useful," the Not-"program"
      exclusion kept in its permanent home, and R3.2 satisfied at -1,442 always-on bytes against
      baseline. With #521/#535/#538/#549 this closes R3.1 through R3.3
- [x] Sweep inventory merged with known defects (PR #573, 2026-08-16): the map's judgment layer is
      strong (the operator's keep/delete rubric in the HLA; 55 convert recipes verified by execution
      with eleven corrected; the nine-PR topology measured), but it merged incomplete, and a
      corrective PR gates the first execution PR. The corrective's charter, from the tester's
      post-merge audit: row the omitted `test_claude_code_orchestrated.py` and
      `test_codex_orchestrated.py` (ten authored launch-note assertions between them); reconcile the
      map, plan, and PR-body counts to one source (inventory says 1,146 executable plus 25 deferred
      and nine PRs; the plan says 1,160 and six); close the three convert rows still permitting "or
      delete"; tag the three untagged unverified rows; correct the 31-versus-30 migration-phrase
      count; fix the D-006 row (as printed it deletes the guard it says to keep); and carry the
      22-site screening as a gating condition on the deferred conversions
- [ ] Reassessment delivered; surviving findings proposed individually or dropped
- [ ] Locked

### Dispatched task (not a child SDD): 0.14 breaking-truth items

Ruled out of the simplification pass and into its own vehicle (operator, 2026-08-13): folding them
into the grammar rewrite would grow an already massive effort. Four contract-truth fixes that are
free only while 0.14 is unreleased: `[secret_config].backends` names sources, not backends (S5); the
one-arm `TokenAcquisition` union's stored arm renames to `mode: secret`, the union kept by operator
ruling because token minting is imminent (C3); the `canonicalize_null_companions` compat flag stops
re-advertising the spelling it just broke (C4); and the four compat layers are deleted or given
recorded expiries (C7). Migration guidance flows through `BREAKING CHANGE:` footers, the packaged
changelog, and the guide release-notes topics rather than compat code. The strategy artifact behind
that (`migration-strategy.md` in the simplification pass's directory) is on draft PR #509 rather
than on `main`, so the brief names it and says when it becomes readable; it is not carried on the
task branch. Runs in parallel with the deletion waves; it owns `env/entry.py` and the token union
while the pass owns the inert descriptor fields.

- [x] Brief seeded (2026-08-13, on `refactor/breaking-truth-0-14`); awaiting an assignee
- [x] Implemented, reviewed, merged before the 0.14.0 tag (PR #531, 2026-08-15): all four
      contract-truth items enforced, 23 net files simpler, the released `token: null` break taught
      correctly, and the C3 keep-the-union ruling recorded on the PR. **This lane is closed**; its
      session can retire and follow-ons live as issues

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
- [x] Shipped in 0.14.0 (released 2026-08-18)

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
- [x] Survey, rough cut, operator rewrite, merge (PR #470, merged 2026-08-10 following the
      operator's direct approval posted on the PR: docs/manifesto.md is the single conviction-voiced
      home, the website consumes it with the Home identity re-pinned atomically per the landing
      order, the kernel-boundary disclosure relocated to SECURITY.md, the concept-manifesto guide
      topic teaches by pointing, and the development-principles clause names the destination)

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

### CLI grammar child (0.14 gate): 2026-08-10-cli-grammar

- [x] Seeded (surface study and focused FRD on draft PR #491; ruled in and gating, operator,
      2026-08-12; `resource describe` removal ruled with 0.14 waiver, 2026-08-15)
- [x] FRD converged through operator review rounds (depth/direction selectors, typed edges from the
      declared data, explain's schema-space rule, the verb glossary; the operator's five review
      lines plus the complexity-critic pre-HLA pass absorbed at `f200f9a3`)
- [x] HLA checkpoint approved (mixed-direction `both` ruled deliberate product scope by the
      operator; saga-lead and complexity-critic lanes clean at `92422b6a`)
- [x] Final artifact gate discharged at `fb899586` after two operator rulings (registry-only
      `--names-only` completion authorized with the byte-identical acceptance; the 39 assertion
      migrations moved ahead of the atomic commit)
- [x] Implementation complete through the reviewed phases: additive primitives and graph service
      (checkpoint `921eea01`), one collateral-complete cutover commit (`b22df512`, 84 files, no
      alias), directed hint-coverage fix (`51149bf1`), all lanes clean at every checkpoint
- [x] Live acceptance passed on the installed CLI with zero residue (graph traversal, depth demand
      against real SQLite, retired spellings exiting 2, names-only byte-identical under missing and
      malformed databases, secret safety)
- [x] Merged and locked (PR #491, 2026-08-16; `locked.md` binds at merge). The 0.14.0 grammar gate
      is satisfied

### Child SDD: 2026-08-17-resource-show

- [x] Seeded, designed, and implemented on one vehicle (draft PR #597, the grammar session as
      successor lead; FRD/HLA/plan in `docs/sdd/2026-08-17-resource-show/`), per the operator's
      direction in that session with acceptance completing at merge
- [x] Focused-superset ruling incorporated (operator, 2026-08-18): `resource show` carries the
      selected resource's list facts, direct graph relationships, live usage, resource-attributable
      doctor checks, and the normalized declaration, built through shared fact producers (the doctor
      extraction into five per-check producers with the bulk sweep rewired onto them); relationships
      stay with `graph show`, traversal absent by construction
- [x] Merged and locked (PR #597, 2026-08-18) after the tester's JSON-boundary finding was closed at
      the shared machine-output writer (full unsafe-category escaping, fixing the class for every
      machine-output command), with live acceptance and all lanes passed at the final head.
      Additive; no 0.14 gate was minted

### Field evidence intake: real-world 0.14.0 runs (2026-08-18)

Two unsolicited field reports from assistant agents on the operator's workstations arrived through
the message channel and merged as `message-2026-08-18-agentic-onboarding-run.md` (PR #601, a full
plan-A onboarding run to a running session) and `message-2026-08-18-guide-tire-kick.md` (PR #602, an
adversarial pass at the guide surface on Windows). The saga lead triaged both at the operator's
direction; the fixable findings were dispatched as PRs the same day, and the rest are routing items
tracked by the open boxes below until each has a delivered recipient-side artifact.

- [x] Fixes dispatched as PRs: #604 (missing SSH key files become a workload-gated config issue;
      nine read-only and diagnostic commands tolerate them, doctor now reaches its own key checks),
      #605 (entrypoint stream hardening: legacy-codepage consoles never crash on Unicode, stdout is
      LF for machine consumers, session-log payloads write verbatim UTF-8 bytes), #606 (guide index
      discloses the versioned release-topic address; changelog evidence rendered fence-safe instead
      of inertly escaped), #607 (`agw --version`, `agent list` on the shared table renderer with a
      granted-implicitly legend, truthful `secret describe` fall-through summary, onepassword
      timeout guidance naming the pending-approval cause)
- [x] The non-TTY secret-resolution finding produced two solution attempts, both abandoned unmerged
      by operator direction (a generalized `--allow-interaction` flag, stopped in review; the
      interaction-channel split, PR #608, closed): the operator did not like the solution. The
      problem restarts from its statement alone, `task-2026-08-18-non-tty-secret-resolution.md`,
      deliberately carrying no solution shape
- [ ] Non-TTY secret resolution becomes a new child SDD adopted into this saga (operator direction,
      2026-08-19): the problem statement (PR #611) is its seed, a fresh effort designs from it with
      no shape carried over from the abandoned attempts, and its own ledger section opens when that
      SDD seeds
- [x] Issue #603 filed: move SSH key-file existence from config-load to use time; the workload-gated
      parameter is the bridge and retires when that lands
- [x] Fix rounds closed and all four fix PRs merged (2026-08-19): every lane finding on the
      dispatched PRs was answered at exact heads. #607 merged (its final round reshaped the timeout
      guidance into a closed core-owned identifier after the tester proved hostile backend text
      could reach rendered output; backends now select prose, never author it). #604 grew to ten
      commands when the tester found `resource edit` still walled (the fix-it path, broken in
      exactly the fresh-init state). #605 gained two rounds on the raw session-log boundary:
      verbatim UTF-8 bytes (no legacy-codepage aliasing) and a shared write-until-done contract (no
      silent truncation on non-blocking pipes). #606 made the index disclosure load-bearing (a
      structural test validating the disclosed address against the live CLI spec and the real
      version parser) and the LLD staleness flag became an operator-directed message to the
      onboarding lead, PR #614. With that, the intake's only outstanding fix-class item is non-TTY
      secret resolution; the routing boxes below stay open until each has a delivered recipient-side
      artifact
- [ ] Routed to the wave-2 discovery surfaces: an agent cannot state a mutation's infrastructure
      effect before making it (effective spec after `inherits` composition and platform defaults
      appears in no CLI surface; first revealed by provisioning). Pairs with schema emission;
      `vm create --dry-run` is the alternative shape
- [ ] Routed to the onboarding child as one decision item: agent mode is nearly a no-op outside the
      index (one topic differs; either more agent-only context is worth writing or the per-topic
      capability is speculative), the non-TTY mode heuristic hands agent content to redirecting
      humans, and no reliable Codex harness signature exists (verified against Codex docs and
      source: sandbox variables are conditional; explicit `agw guide --agent` stays the sanctioned
      path). The tire-kick's verified do-not-regress list (command drift zero, tight topic lookup
      boundary, posture content that changes behavior) is that child's regression charter
- [ ] Flagged to the onboarding lead (ownership): `guide-contract-lld.md` still says release
      evidence is "escaped inert evidence" and lists only `CLAUDECODE=1` in mode precedence; both
      went stale with #606 and the signature verdict. Escalated to an operator-directed message (PR
      #614, merged 2026-08-19, so the direction is delivered) after the tester found the stale
      contract could cause a later round to restore the escaping; checks off when the onboarding
      lead records the fence-containment contract
- [ ] Small follow-ups, unowned: `vm list` still uses the legacy hand-rolled table layout with the
      same overrun class #607 fixed for `agent list`; transport decode replaces truly malformed
      remote bytes before the session-log boundary (pre-existing, narrower than the fidelity fix)

### Not yet spawned

Planned children, seeded when their prerequisites land (see `phasing.md`):

- Wave 4: harness scope framework. Seed material on record: the capability config shape note
  (`message-2026-08-16-capability-config-shape.md`, merged PR #562 with the integration tester's
  factual corrections incorporated as a recipient's note per operator direction), carrying the
  `config_at(level)` shape sketch, the three preserved `base.py` constraints, the
  who-constructs-versus-who-calls-in caution, and the pre-design call-site discovery walk. The
  capability-API reevaluation is chartered into this wave's seed, not scheduled sooner
- Wave 5: session observability phase 1
- Wave 6: agentic artifacts and distillation
- Wave 7: structured control
- Wave 8: external plugin API
- CLI grammar child: spawned, completed, and locked; see its ledger section above.
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
rebase obligation (manifesto source path plus Home block re-pins) was discharged by PR #470's merge
on 2026-08-10, and the continuous-lander stack entry now targets main.

Two operator rulings on the Lander (2026-08-11): its continued expansion is intentional rather than
creep, so the saga lead's proportionality question is closed rather than dormant; and **the Lander
and onboarding both gate the final custom-domain cutover**.

That second ruling **changes** the website contract rather than ratifying it, and the change is owed
by the website effort, whose artifacts these are. As written, the website plan's Phase 6 activates
the interim domain with onboarding still pending, and `website/README.md`, FRD R10, and HLA D9 all
permit that pre-onboarding cutover. Those definitions of done, the runbook, and the README now need
to state both prerequisites; the saga lead has flagged it on PR #486 rather than editing another
effort's artifacts. Onboarding's own remaining gate is the live acceptance carried on the
regenerated 0.14 release PR, so the cutover sits behind that too. The ruling is recorded in
`target-state.md`, which now qualifies its no-structural-coupling statement: one bounded coupling
exists between two adjacent efforts, and it creates no wave gate, no reverse dependency in
`phasing.md`, and no saga-lock edge.

GCP scope extraction (operator ruling, 2026-08-11): PR #479 had absorbed a shared install-predicate
contract change and an AWS installer rewrite discovered during its live acceptance. The shared
predicate change left the PR. The ruling generalizes: outside of bug fixes for the work in question,
core-logic modifications are their own efforts. Its permanent home merged as PR #497 (dev-process
section 1a plus reviewer check 12b, 2026-08-12). Two follow-ups were filed rather than absorbed:
[issue 492](https://github.com/WayfarerLabs/agentworks/issues/492) (SSH known-hosts state escapes an
isolated `HOME` at eleven call sites, every platform) and
[issue 496](https://github.com/WayfarerLabs/agentworks/issues/496) (the install-command predicate
contract plus transactional AWS installer completion, carrying the extracted commits and seven open
findings, among them the predicate test that asserted CI hosts provide zsh and reddened every matrix
job). The AWS CLI recipe and its install-command teaching came out too, on the operator's
instruction, after it emerged that the framework already ships declarative `snap` support
(`vm_template.snap`), which the saga lead's three reviews of that machinery never checked for:
craftsmanship verified, existence never priced. The replacement needs classic-confinement support in
the `snap` template field, itself a shared-machinery change and so its own small effort. **PR #479
merged 2026-08-12** with the extraction complete.

Filed the same day but from a different source, so not a GCP follow-up:
[issue 495](https://github.com/WayfarerLabs/agentworks/issues/495), a doctor TTY color-test flake
observed on docs-only PR #494, where the tests faked a terminal by patching `isatty` on a stream
that capture replaces, so parallel CI decided colorization differently from run to run. Fixed and
closed by PR #499 (2026-08-12): the two independent color readers are now tested directly instead of
through the capture path.

## Standing process rulings

Permanent homes landed via PRs #473/#474/#477/#481: the sdd skill's supersession paragraph; the
dev-process draft/ready handoff, author-owned review-requested label, exact handoff definition,
subagent-review taxonomy, and commit/PR/stack layering with the cascade rule; and the saga-lead
review triggers matching them. (A previously recorded enforcement-calibration note was retracted by
the operator on 2026-08-10 as personal guidance to the saga lead, not process; the dev-process
poller rule stands as merged in PR #481, and PR #489 closed unmerged.)

- **Only the operator directs (operator, 2026-08-11; permanent homes merged as PRs #500, #501 and
  #510):** a published review informs, it never authorizes. The author posts a reading, applies
  `awaiting-direction`, and stops; the fix round begins on the operator's authenticated direction,
  and the label drops only once every reading has a disposition. The line is drawn by channel, not
  by reviewer identity: a session's own private pre-handoff reviews keep their fix loop.
  Bot-maintained lanes (dependabot, release-please) are exempt from the handoff mechanics, since
  their head moves are server state. The `operator-authority` rule generalizes it past GitHub: one
  operator, one authority chain, and everything else is input.
- **Sagas label their PRs (2026-08-13, PR #510):** a `saga:<name>` label on every PR a saga lead
  seeds or reviews, so concurrent sagas can each enumerate their own surface rather than infer it.
  This saga uses `saga:next-steps`. Open question filed as issue #511: whether always-on rules
  reliably reach an agent before its first consequential action. It applies to the twelve rules
  delivered path-conditionally rather than to any one of them (see the simplification pass's wave 0
  entry for the inventory), so fixing one in isolation would only make it inconsistent with the
  rest.
- **Requirements belong to the operator (operator-directed, PR #549, merged 2026-08-16):**
  requirements are the FRD plus any document it designates as carrying requirements, owned by the
  operator with the merge as consent; everything else is response and the effort lead's, whoever
  produced it. Authorship is never ownership, amendments travel as requests, and a drafting lead
  applies findings directly only until its draft merges. Ledger entries checked before this date
  that say "ownership transferred to the effort lead" record the superseded transfer model
  truthfully for their time and are not current teaching; the `sdd` skill is authoritative.
- **Use the database, not its sidecars (operator, 2026-08-12, from issue #502):** read state through
  SQLite itself; do not inspect WAL files or other on-disk artifacts to guess whether a read is
  safe. The completion path had vetoed itself whenever any process held the database open, which
  broke completions outright. Fixed in PR #503, with the busy-versus-malformed misclassification it
  exposed fixed in PR #504. Four follow-ups filed rather than absorbed: issues #505 (the remaining
  database-open seams), #506 (`GuideResponse` should carry the typed system error), #507 (doctor
  pays the busy wait three times) and #508 (`BusyStateError` documentation truth).
- **Plan-history ruling (operator, 2026-08-10):** completed plan steps may never be removed, with no
  exception for never-merged, superseded, or expunged work. Everything else strips clean under a
  scope correction (narration, correction framing, abandoned unchecked boxes, definitions of done,
  evidence prose, PR bodies). The sdd skill's checkbox rule stands as written; a clarifying sentence
  lands in the skill so this is not re-litigated.
- **Contract pricing (operator, 2026-08-09, from the twin scope corrections on #462 and #453):**
  adversarial verification verifies the contract; it never expands it. More than two or three fix
  rounds on one finding is a contract smell: stop and re-price the requirement with the operator
  instead of growing machinery. Reviewers price requirements, not just implementations.
- **No prose policing (operator, 2026-08-11; permanent home merged as PR #493):** we do not
  unit-test the wording of prose we author. Asserting a sentence is present, blacklisting forbidden
  phrasings, normalizing prose to compare it, and pinning a body verbatim are all out; wording is a
  review concern and behavior is a test concern. The one exception is prose arriving from outside
  the repository, pinned narrowly at the token the code branches on. Development principle 3 carries
  a clarifying paragraph so "enforce invariants" is never read as "assert the sentence", and the
  reviewer asks for deletion rather than a stronger pin. Note for future rounds: a stronger pin is
  the same mistake one size larger, which the saga lead proposed twice before it stuck.
- **Findings outside an effort's machinery (operator, 2026-08-11; permanent home merged as PR #497,
  2026-08-12):** outside of bug fixes for the work in question, core-logic and shared-contract
  modifications are their own efforts even when another effort found the bug. The test is whether
  the fix touches machinery this effort owns, settled by asking whether the bug reproduces with the
  effort reverted and whether the fix changes behavior for consumers who never asked for it. File
  the finding with root cause and call sites; move already-written commits to their own branch
  rather than merging them. Reviewers are bound symmetrically: asking for an out-of-scope fix is how
  scope grows.
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
17. Gate status (2026-08-13, post-#480): PR #480 merged, so the assistance phase is done. The cut
    now has a longer runway by ruling, not by slip: **0.14.0 waits for the CLI grammar rewrite**
    (operator, 2026-08-12), and the simplification pass is adopted as a child and runs before it.
    The serial spine completed on 2026-08-16: wave 0, corrected PR #548 (with the #551 LLD
    supersession), the one-wave guide-value deletion (PR #556), and the grammar rewrite (PR #491,
    merged and locked). The hard-gate ledger: the grammar gate closed with #491, the breaking-truth
    task and installer-plugins moves closed their lanes earlier ("This lane is closed" on each entry
    above), the README bootstrap gate is satisfied (PR #480), and the corrected-guide-model gate is
    satisfied (PR #579, merged 2026-08-17; see the onboarding entry). Two 2026-08-17 rulings
    (recorded in `target-state.md`) reshaped the remaining gates: the **grammar-native guide (PR
    #593) gates 0.14.0**, since the release does not ship the retired guide grammar, and the
    **simplification pass no longer gates**, its remaining work (sweep, gcp dedup, reassessment,
    lock) continuing on its own merits as repository-internal test quality invisible in the shipped
    artifact; the test-consolidation soft gate resolves as cut with it. The other artifact-facing
    release item is the changelog repair (issue #589, executed as a manual release-branch edit after
    the final merge), and the operator holds a personal edit pass over guide content and website
    wording before the cut. A 2026-08-16 operator clarification is recorded here because a checked
    entry above says the harness-integration window ran "until the grammar rewrite's breaking PRs
    land": the operator's actual constraint was an opening edge only (the integration waits for the
    surfaces it sits on, all landed), command grammar is not such a surface, and no closing edge
    ever existed. The integration may be built at any time.

    Besides wave 2, the lanes still running in parallel with the spine are the remaining wave 1
    deletion items and the grammar rewrite's design and seeding (only its implementation waits).
    Wave 1's nine deletion work items are unordered by design and are themselves the largest
    parallelism available. The previously recorded #486 coordination hazard is discharged: #486
    merged on 2026-08-16, so the pass's website items are unblocked.

    Discharged in this round: the breaking-truth brief is seeded on `refactor/breaking-truth-0-14`,
    the trail-sign message to the onboarding effort rides this PR, and the supersession note for
    #504's classifier change is on the safer-migrations lock. Each still needs a session pointed at
    it; seeding is not staffing.
