# Orchestration layer: implementation plan

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

Tracks the work behind the FRD and HLA in this directory. The shape is fixed by two FRD rules and
they govern every phase below:

- **Behavior parity, suite as oracle (R7).** The full existing test suite passes at every merge
  point; behavioral assertions do not change, only internals that move are mechanically adjusted.
- **Incremental, always green (R8).** Old (imperative roots) and new (orchestrated) composition
  coexist; commands migrate one at a time; each phase is a complete, shippable, green unit. No
  big-bang cutover exists anywhere in this plan.

Helpers EMERGE (they are not designed up front); each phase lists the exact `orchestration/` files
it adds, and a file appears only in the phase whose command first forces it. Permanent docs ride the
commit that makes their claim true (SDD lockstep rule), so `capabilities/README.md` and the model
narrative update INSIDE the phases that land the behavior, not in a closeout pass.

Checkbox discipline: a completed box is an immutable record (SDD rule); if the plan changes, add new
boxes rather than editing done ones.

## Phase 0: Foundations (the surface the tracer forces)

Goal: the minimal shared node/context/walk/secret/gate surface `vm add-git-credential` needs,
nothing more. This is "build exactly the tracer's dependencies," not "build the framework", with one
honest exception: the `RunContext` field-to-accessor conversion below is a repo-wide MECHANICAL
migration (every capability reader, e.g. proxmox `runup`, plus all fourteen `RunContext`
construction sites), not tracer-local. It lands here because flipping it once up front is cleaner
than per-command (the HLA open question "flip all three at once?" is answered yes); it is green-able
and behavior-neutral.

- [ ] `orchestration/node.py`: the `Node` protocol (`key`, `deps`, `secret_refs`, `preflight`,
      `runup`); the `<kind>/<name>` key convention; the creatable-node `teardown` surface (declared
      here, first implemented in Phase 2).
- [ ] `Capability` implements `Node` directly on `capabilities/base.py` (R9): add `key` (from
      `owner_kind`/`owner_name`), `deps`, `secret_refs` (from `validate_config`); no adapter class.
- [ ] `orchestration/walk.py`: memoized, cycle-checked, deterministic multi-root walk
      (`walk(*roots)` from day one, per spike finding 2).
- [ ] `orchestration/secrets.py`: `secret_union(nodes)`; central resolvability prediction from
      DECLARED references (preserving `preview_resolution`'s exact semantics, including the
      optimistic interactive-backend answer); the scoped delivery reader (`ctx.secret(name)` view).
- [ ] `OperationScope` + `ScopeLevel` on `capabilities/base.py`: full five-level enum; frozen value
      object; `__post_init__` ENFORCING the level-to-fields invariant. Implement SYSTEM and VM
      levels' rules now (the tracer's needs); the deeper levels' rules land with their commands.
- [ ] `RunContext`: add the `OperationScope` field; convert `admin_target` / `agent_target` /
      `secrets` from bare fields to PLAIN accessor methods (`ctx.agent_target()` / `ctx.secret()`),
      pass-through, no requester binding, no gating (deferred to the plugin SDD).
- [ ] `orchestration/readiness.py`: the preflight SWEEP over a walk's output (the skip-and-degrade
      policy helper is Phase 2).
- [ ] `orchestration/activation.py`: the gate STRUCTURE (`ensure_active` + the held `vm_active` span
      relocated here), the `operator_stopped` refusal, and the just-in-time gate-secret resolution.
      (The gate-to-boundary SEED of resolved values moves to Phase 1, so it is designed against the
      tracer's real proxmox `status`-needs-token caller rather than speculatively here, reviewer
      question 2026-07-17.)

Definition of done: unit tests for the walk (dedup/cycle/order), the secret union and prediction,
`OperationScope` enforcement (a mis-leveled scope cannot construct), the scoped reader, and the gate
(auto-start vs operator-stopped refusal, span open/close, just-in-time gate resolve). No command is
migrated yet; the full suite is green. LLD spun out only if the node/context/walk contract reveals a
decision these boxes gloss (the translation rule below is the likely trigger).

## Phase 1: Tracer bullet, `vm add-git-credential`

Goal: one real command end to end through an orchestrator, discharging the obligations the spike
could not (FRD R8 + the reviewer's proof points). This command touches an EXISTING VM with a FATAL
runup and no pending nodes, so it exercises derivation, the gate, and scoped delivery without
unwind.

- [ ] `vms/nodes.py`: the live VM node factory (from `VMRow`); the vm-site platform capability node
      and the git-credential provider node enter as its declared dependency edges.
- [ ] The reference-graph-to-node-graph TRANSLATION RULE implemented in each node kind's `deps()`
      (registry references by kind, secrets as inputs not nodes, row fields to live edges), and the
      tracer's graph DERIVED from real declared references and the DB row with ZERO hand-wired edges
      (the tracer's defining obligation).
- [ ] The gate-to-boundary SEED (moved from Phase 0): designed here against the tracer's real
      caller, proxmox's gate `status` needs the API token before the boundary, so the token resolved
      at the gate must pre-seed the boundary `Resolver` (which refuses post-pass registration)
      rather than resolve again. This is the likely LLD spin-out.
- [ ] The `add-git-credential` orchestrator: build graph -> open the activation gate (resolving its
      just-in-time credential, which seeds the boundary) -> preflight sweep at VM level -> resolve
      -> git-credential `runup` under the FATAL policy -> the materials-write op reading its scoped
      secret.
- [ ] Proof-point assertions (reviewer carry): (a) the derived graph reproduces the imperative
      preflight set and secret union; (b) the runup rejection is fatal, matching HEAD; (c) a
      SCOPED-DELIVERY test that the materials node receives ONLY its declared secret names (guarding
      the whole-cache fallback from becoming permanent); (d) the operation scope reaches the node's
      readiness; (e) GATE-PROMPT parity, stated precisely because the gate's TIMING legitimately
      shifts (HEAD opens `keep_active` AFTER its single resolve and wraps only the write; the model
      opens the gate BEFORE preflight-all and holds it through the command, so the gate's credential
      is resolved earlier): the assertion is "exactly ONE prompt session, entirely before the
      walk-away point, and no secret resolved or prompted twice", NOT literal timing parity. The
      tracer genuinely exercises the just-in-time gate resolve plus boundary seed (proxmox's
      `status` needs the API token before the boundary), which must demonstrably not double-prompt.
- [ ] The imperative `add_git_credential` is retired (or reduced to a thin call into the
      orchestrator); the interim seam to any not-yet-migrated machinery is documented here.

Definition of done: `agw vm add-git-credential` runs through the orchestrator; the full suite is
green; the five assertions pass; no regression in output, prompt timing, or the fatal-rejection
error.

## Phase 2: `vm create` / `vm reinit`

Goal: pending nodes, unwind, and the skip-and-degrade runup policy, on a multi-capability graph
(vm-template node + platform + git-credential providers). `vm reinit` is the existing-VM case (the
gate applies); `vm create` provisions.

- [ ] `orchestration/unwind.py`: `RealizationLog` (append on `mark_realized`, read backwards on
      `unwind`); creatable node `teardown` implementations relocate today's rollback bodies
      (`create_vm`'s row delete) onto the nodes.
- [ ] `readiness.py` gains the skip-and-degrade POLICY helper (today's
      `git_credentials.runup_and_filter` generalized): a rejected credential skips its materials op,
      logs, and degrades the command to PARTIAL.
- [ ] The pending VM node and `vms/templates.py`'s `preflight_vm_template` relocated onto the
      vm-template node's `preflight`.
- [ ] The `vm create` / `vm reinit` orchestrators, expressing today's proven phase order and
      rollback semantics.
- [ ] Parity assertions: UNWIND set and order reproduce `create_vm`'s rollback; SKIP-AND-DEGRADE
      reproduces `runup_and_filter`'s partial-degradation behavior.

Definition of done: both commands orchestrated; the full suite green; unwind and skip-and-degrade
parity asserted against HEAD behavior.

## Phase 3: `session create` (incl. `--new-agent`) and `session restart`, EARLY

Goal: the harness's landing pad (FRD R8/R10). The nested ephemeral fan-out becomes ordinary graph
behavior; the phase-free realization choreography is factored so it is callable by both this
orchestrator and Phase 4's. This phase provides the session/workspace/agent live-and-pending nodes
and the session orchestrator; the HARNESS node itself is delivered by the re-scoped harness SDD.

Cross-SDD independence (reviewer carry, 2026-07-17): Phase 3 must be a green shippable unit whether
or not PR #168 has landed, so the harness node is NOT a hard prerequisite. If it has not landed,
this phase migrates `session create` / `restart` to the orchestrator while invoking TODAY's
imperative harness path (the command string that `_build_session_command` produces) through a
documented interim seam, and the level-skip proof uses a harness-LIKE required-commands node (doctor
at SYSTEM level). When PR #168 lands, the real harness node replaces the seam with no orchestrator
change. This keeps R8's "pausable, always green" across the SDD boundary rather than assuming
lockstep landing.

- [ ] `sessions/nodes.py` and `workspaces/nodes.py`: live-and-pending session and workspace nodes;
      `agents/nodes.py`: the live-and-pending agent node with intrinsic (row-carried) identity.
- [ ] The PHASE-FREE realization choreography per creatable kind (the agent-realization body: agent
      ops plus the git-credential nodes' materials ops), factored as domain code with no phases and
      no resolve of its own, replacing the `git_tokens` + `own_root` nesting hack.
- [ ] The `session create` / `session restart` orchestrators: build; gate; preflight-all (harness
      defers on its pending target); resolve; dependency-ordered roll-forward with
      `log.mark_realized` after each bespoke mutation; the command-shaped restart ordering (kill
      before the `restart` op) and its pinned non-rollbackable window.
- [ ] The level-driven SKIP branch's first real exercise: a doctor scan reaching a harness (or a
      harness-like required-commands node) at SYSTEM level, asserting it NO-OPS rather than erroring
      (the branch the spike structurally could not prove).
- [ ] Coordinate with the re-scoped harness SDD: this pad drops the threaded `OperationIdentity` and
      `to_create` in favor of intrinsic layer-1 identity, the per-command operation scope, and
      pending nodes; the harness node reads only the LEVEL off the operation scope and addresses via
      its own `session_name`.

Definition of done: session create/restart orchestrated (against the real harness node if PR #168
has landed, else the documented interim seam to today's imperative harness path); the nested fan-out
reproduces the `git_tokens` fold; the realization body is shared (Phase 4 consumes it); the skip
branch is proven; the full suite green.

## Phase 4: `agent create` / `agent reinit`

Goal: the standalone agent orchestrator wraps the Phase 3 realization body in its own phases,
retiring the last of the nesting hack.

- [ ] The `agent create` / `agent reinit` orchestrators calling the shared realization body inside
      their own build/preflight/resolve/unwind.
- [ ] Confirm the `git_tokens` / `show_phases` special-casing is fully gone (a body never resolves
      and never frames phases).

Definition of done: both commands orchestrated; the nesting hack removed; the full suite green.

## Phase 5: Remaining commands + resolver retirement

Goal: migrate the rest opportunistically, then remove the now-dead per-instance resolver.

- [ ] `vm delete`, `vm start` / `vm stop`, the shell / exec roots, and console commands (console
      nodes introduced lazily here), each a green shippable unit.
- [ ] RESOLVER RETIREMENT once no migrated command depends on the bound resolver: drop the
      `resolver` constructor parameter from `Capability`; close the `preflight_vm_template` resolver
      seam (prediction is central now); kill proxmox's op-client bridge so `_api` reads the token
      from the context (`ctx.secret`) rather than the bound resolver, completing PR #182's
      direction.

Definition of done: every command orchestrated; `Capability` constructs without a resolver; proxmox
ops read the context; the full suite green.

## Phase 6: Decision record and lock

Goal: finalize the permanent record. (The `capabilities/README.md` rewrite and the model narrative
are NOT here; they ride their phases per the lockstep rule.)

- [ ] Confirm `capabilities/README.md`, `docs/guides/resources.md`, and the top-level model
      narrative reflect HEAD (the node protocol, orchestrator ownership of traversal/secrets, the
      completed declare/receive contract, the two-layer identity + operation scope), each having
      landed with its phase.
- [ ] Promote and NUMBER the ADR from this feature directory into `docs/adrs/` (it references ADR
      0016, `0016-yaml-resource-manifests.md`, for the capability collapse and
      `capabilities/README.md` for the lifecycle contract; its own number is assigned at promotion,
      0019+ given 0018 is current). The ADR notes in one line that best-effort reverse-order unwind,
      rather than Terraform-style taint-and-leave, is a conscious PARITY-driven choice (preserving
      today's rollback behavior under R7), not a fresh design decision (reviewer note, 2026-07-17).
- [ ] Write `locked.md` summarizing the final state.

Definition of done: the ADR is numbered and promoted; permanent docs are accurate at HEAD; the
lockfile is written.

## LLDs

No LLDs are pre-committed. The likely spin-outs, generated when a phase's boxes reveal a real
decision rather than a mechanical edit:

- The reference-graph-to-node-graph translation rule and node-construction factories (Phase 1), if
  the one-registry-resource-to-many-nodes and no-registry-resource-node cases prove subtle.
- The gate-to-boundary secret seeding against a resolve pass that refuses post-pass registration
  (Phase 0/1).
- The realization-choreography factoring and the interim seam catalog (Phase 3), if the shared body
  and the coexisting imperative roots need a pinned contract.

Any LLD lands in this directory and is linked from the phase that generates it.
