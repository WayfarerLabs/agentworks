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

- [x] `orchestration/node.py`: the `Readiness` protocol (`preflight`, `runup`) and the `Node`
      protocol (`Readiness` + `key`, `deps`, `secret_refs`); the `<kind>/<name>` key convention; the
      creatable-node `teardown` surface (declared here, first implemented in Phase 2).
- [x] Capability instances stay `Readiness`-ONLY on `capabilities/base.py` (R1): no `key`, no
      `deps`, so they are structurally not nodes. The `git-credential` and `vm-site`
      consuming-resource nodes (their `deps()`/`secret_refs()` and a `preflight`/`runup` that
      composes the held instance) land with the tracer in Phase 1. LLD decides whether that
      composition is a one-line per-kind fan-in or a shared held-instances hook (neither protocol
      exposes a held-instances accessor today); the same decision governs `secret_refs` aggregation
      over a map of held instances.
- [x] `orchestration/walk.py`: memoized, cycle-checked, deterministic multi-root walk
      (`walk(*roots)` from day one, per spike finding 2).
- [x] `orchestration/secrets.py`: `secret_union(nodes)`; central resolvability prediction from
      DECLARED references (preserving `preview_resolution`'s exact semantics, including the
      optimistic interactive-backend answer); the scoped delivery reader (`ctx.secret(name)` view).
- [x] `OperationScope` + `ScopeLevel` on `capabilities/base.py`: full five-level enum; frozen value
      object; `__post_init__` ENFORCING the level-to-fields invariant. Implement SYSTEM and VM
      levels' rules now (the tracer's needs); the deeper levels' rules land with their commands.
      (Implementation note: the un-ruled levels, WORKSPACE / AGENT / SESSION, REFUSE construction
      with a typed error until their rules land, so no scope with an unenforced invariant can
      exist.)
- [x] `RunContext`: add the `OperationScope` field; convert `admin_target` / `agent_target` /
      `secrets` from bare fields to PLAIN accessor methods (`ctx.agent_target()` / `ctx.secret()`),
      pass-through, no requester binding, no gating (deferred to the plugin SDD). (Implementation
      note: constructor keywords kept their public names, so the fourteen construction sites needed
      no diff; the reader migration covered every `ctx.secrets` consumer, and the
      no-resolved-secrets guard centralized into `ctx.secret()`'s typed `ConfigError`.)
- [x] `orchestration/readiness.py`: the preflight SWEEP over a walk's output (the skip-and-degrade
      policy helper is Phase 2).
- [x] `orchestration/activation.py`: the gate STRUCTURE (`ensure_active` + the held `vm_active` span
      relocated here), the `operator_stopped` refusal, and the just-in-time gate-secret resolution.
      (The gate-to-boundary SEED of resolved values moves to Phase 1, so it is designed against the
      tracer's real proxmox `status`-needs-token caller rather than speculatively here, reviewer
      question 2026-07-17.) (Implementation note: the gate landed as `ensure_active` /
      `activation_gate` over a narrow `GateTarget` protocol, the power-state slice the live VM node
      implements in Phase 1, keeping the helper domain-blind per the HLA's layering rule; the
      refusal is raised from the target's own `auto_start`, per "the node is the authority". The
      imperative `vms.manager.ensure_active` / `keep_active` pair keeps serving the un-migrated
      commands and retires as they migrate, per R8's coexistence rule; oracle parity between the two
      is asserted when the tracer wires the real VM node in Phase 1.)

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

- [x] `vms/nodes.py`: the live VM node factory (from `VMRow`); the `vm-site` node (holding the
      platform instance) and the `git-credential` node (holding the provider instance) enter as its
      declared dependency edges.
- [x] The reference-graph-to-node-graph TRANSLATION RULE implemented in each node kind's `deps()`
      (registry references by kind, secrets as inputs not nodes, row fields to live edges), and the
      tracer's graph DERIVED from real declared references and the DB row with ZERO hand-wired edges
      (the tracer's defining obligation).
- [x] The gate-to-boundary SEED (moved from Phase 0): designed here against the tracer's real
      caller, proxmox's gate `status` needs the API token before the boundary, so the token resolved
      at the gate must pre-seed the boundary `Resolver` (which refuses post-pass registration)
      rather than resolve again. This is the likely LLD spin-out. (Implementation note: landed as
      `Resolver.seed(values)`, small enough that no LLD was spun out. Seeded names register on the
      resolve set, serve through `get` immediately, pre-pass, which is what lets proxmox's `status`
      read the bound resolver at the gate, and are excluded from the boundary pass's backend loop;
      seeding after the pass raises, mirroring the post-pass registration refusal. The
      orchestrator's gate resolve callback resolves through the normal backend chain and seeds as it
      goes.)
- [x] The `add-git-credential` orchestrator: build graph -> open the activation gate (resolving its
      just-in-time credential, which seeds the boundary) -> preflight sweep at VM level -> resolve
      -> git-credential `runup` under the FATAL policy -> the materials-write op reading its scoped
      secret.
- [x] Proof-point assertions (reviewer carry): (a) the derived graph reproduces the imperative
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
      The gate parity assertion also covers the operator-stopped RE-READ race guard: the VM node's
      `auto_start` must re-read the intent flag at start time, as HEAD's `ensure_active` does
      (reviewer carry, 2026-07-17). (Implementation note, 2026-07-17, recording two facts the
      checked wording glosses and one ruling. FIRST, the derived preflight SET is a strict SUPERSET
      of the imperative one: the sweep preflights every participating node, including the
      git-credential provider, which the imperative command constructed but never preflighted; the
      secret UNION matches exactly. SECOND, that superset shifts one error shape, a knowing R7
      exception: an unresolvable token now fails at the sweep as the capability base preflight's
      `ConfigError` with the secret-describe hint, instead of HEAD's `SecretUnavailableError` at the
      boundary resolve; the new shape matches what `vm reinit` already produces for the same
      failure, so the exception buys cross-command consistency. THIRD, the prompt-session ruling
      (2026-07-17): the checked "exactly ONE prompt session" phrasing overstates the invariant; what
      is promised and proven is ALL interactivity strictly before the walk-away point, and no secret
      resolved or prompted twice. Contiguity is not promised: on a stopped VM with prompt-backed
      secrets there are two interactive moments, the gate then the boundary, separated by the VM
      start, which is the gate-first ordering the model itself requires so readiness probes a live
      target.)
- [x] The imperative `add_git_credential` is retired (or reduced to a thin call into the
      orchestrator); the interim seam to any not-yet-migrated machinery is documented here. (Seam
      catalog for this command, also stated in the orchestrator's docstring: (1) capability
      instances are still constructed against the operation's resolver, so construct-time
      registration coexists with the walk-derived union, which the orchestrator registers alongside
      and the tracer tests assert equal; (2) the platform's power ops still read their API token
      through the bound resolver, proxmox's op-client bridge, which is why the gate seeds it; both
      close with the resolver retirement in Phase 5. (3) The node's auto-start reuses the imperative
      repair machinery via `_ensure_tailscale(auth_key_source=...)`: the key still arrives through
      the gate's lazy reader (nodes never resolve), and the parameter's default keeps today's
      internal late resolve for the imperative callers. (4) Resolvability PREDICTION still runs
      through the instances' bound resolvers: the sweep composes the instances' own preflight
      predictions, preserving their exact error shapes, and the central `predict_resolution` helper
      (Phase 0) has no production caller until the Phase 5 retirement, where it takes over; the HLA
      Secrets section carries the matching as-landed note. The imperative `ensure_active` /
      `keep_active` pair keeps serving un-migrated commands; the VM node's gate surface mirrors it
      case for case in `tests/vms/test_vm_nodes.py`, the oracle-vs-gate parity assertion.)
- [x] `capabilities/README.md` (lockstep, R9): the first consuming-resource node (`git-credential`,
      `vm-site`) with a composing `preflight` makes the README's thin-case guidance "do not grow a
      preflight on a consuming resource; construct the instance and call the instance's" false, so
      REVERSE it here and introduce the `Readiness`/`Node` split, rather than letting the README
      self-contradict.

Definition of done: `agw vm add-git-credential` runs through the orchestrator; the full suite is
green; the five assertions pass; no regression in output, prompt timing, or the fatal-rejection
error.

## Phase 2: `vm create` / `vm reinit`

Goal: pending nodes, unwind, and the skip-and-degrade runup policy, on a multi-capability graph
(vm-template node + platform + git-credential providers). `vm reinit` is the existing-VM case (the
gate applies); `vm create` provisions.

- [x] `orchestration/unwind.py`: `RealizationLog` (append on `mark_realized`, read backwards on
      `unwind`); creatable node `teardown` implementations relocate today's rollback bodies
      (`create_vm`'s row delete) onto the nodes. (Implementation note: `CreatableNode` gained the
      `realized` / `mark_realized` surface Phase 0 deliberately deferred here;
      `PendingVMNode.mark_realized` enforces the one-way flip loudly. The rollback-failure warning
      is now the log's generic "rollback: teardown of <key> failed" line rather than create's
      bespoke delete-record wording, an accepted message-shape shift on a failure-of-the-rollback
      path.)
- [x] `readiness.py` gains the skip-and-degrade POLICY helper (today's
      `git_credentials.runup_and_filter` generalized): a rejected credential skips its materials op,
      logs, and degrades the command to PARTIAL. (Implementation note: `runup_skip_and_degrade` is
      `Readiness`-typed, so instances and nodes both fit; `runup_and_filter` is now its
      git-credential-messaging face and keeps serving the write-step call sites, agents included,
      unchanged. The write-step runup stays INSIDE the shared initializer machinery, an interim
      seam: both orchestrators call `initialize_vm` / `run_initialization`, which invoke the policy
      at the materials write; the policy MEANING now lives in the shared helper even though the call
      site has not moved.)
- [x] The pending VM node and `vms/templates.py`'s `preflight_vm_template` relocated onto the
      vm-template node's `preflight`. (Implementation note: `preflight_vm_template` remains as a
      thin delegate constructing the node, for the not-yet-migrated `rekey_vm` and direct tests; it
      retires with that command's migration. The template node's `secret_refs` carry ONLY the
      Tailscale key: the template's env-block secret references are runtime inputs, so folding them
      in would break provisioning's hermeticity; pinned by test.)
- [x] The `vm create` / `vm reinit` orchestrators, expressing today's proven phase order and
      rollback semantics. (Implementation notes: reinit's gate opens BEFORE the preflight sweep, the
      same sanctioned timing shift as the tracer, where HEAD's `keep_active` wrapped only the init.
      The complete R7-exception record for that shift: the operator-stopped refusal now fires before
      any init work (HEAD refused mid-command, post-resolve), and its corollary, an auto-stopped VM
      now auto-starts BEFORE a preflight or resolve failure would surface, where HEAD failed those
      without starting it; the start is idempotent declared-state maintenance, never
      rollback-tracked (the "maintenance, not plan mutation" stance recorded in `activation.py`), so
      the VM staying up after such a failure is accepted, not a leak. Reinit's graph deliberately
      has NO vm-template node, since the Tailscale key is not part of its planned ops and must not
      join the boundary union; the rejoin stays on the gate's conditional repair path. `vm create`
      has no gate (nothing exists to converge). The realization point is the DB row, the artifact
      `teardown` deletes: `mark_realized` fires when the row exists, the unwind window covers
      exactly the provisioning span, and initialization failures keep the VM, as at HEAD. Neither
      graph shares a node between two consumers yet, so the cross-factory memo the Phase 1 notes
      reserved is STILL not built; the first true multi-consumer graph owns it.)
- [x] Parity assertions: UNWIND set and order reproduce `create_vm`'s rollback; SKIP-AND-DEGRADE
      reproduces `runup_and_filter`'s partial-degradation behavior. (Where:
      `tests/orchestration/test_unwind.py` (order, best-effort, `UserAbort`),
      `tests/vms/test_create_reinit_orchestrated.py` (row unwound on provisioning failure and
      interrupt, kept on init failure, teardown failure warns without masking),
      `tests/orchestration/test_readiness.py` plus the unchanged
      `tests/test_git_token_verification.py` `runup_and_filter` suite (partial-degradation behavior,
      now through the shared helper).)

Definition of done: both commands orchestrated; the full suite green; unwind and skip-and-degrade
parity asserted against HEAD behavior.

## Phase 3: `session create` (incl. `--new-agent`) and `session restart`, EARLY

Goal: the harness's landing pad (FRD R8/R10). The nested ephemeral fan-out becomes ordinary graph
behavior; the phase-free realization choreography is factored so it is callable by both this
orchestrator and Phase 4's. This phase provides the session/workspace/agent live-and-pending nodes
and the session orchestrator; the HARNESS node itself is delivered by the re-scoped harness SDD.

Cross-SDD independence (reviewer carry, 2026-07-17): Phase 3 must be a green shippable unit whether
or not PR 168 has landed, so the harness instance is NOT a hard prerequisite. If it has not landed,
this phase migrates `session create` / `restart` to the orchestrator while invoking TODAY's
imperative harness path (the command string that `_build_session_command` produces) through a
documented interim seam, and the level-skip proof uses a harness-LIKE required-commands node (doctor
at SYSTEM level). When PR 168 lands, the real harness instance replaces the seam with no
orchestrator change. This keeps R8's "pausable, always green" across the SDD boundary rather than
assuming lockstep landing.

Phase 3 lands in TWO pushes at the coordinator-sanctioned seam (2026-07-17): first the nodes, the
SESSION scope level, and the four-way fork (the two boxes checked below); then the phase-free
realization choreography and the two orchestrators.

- [x] `sessions/nodes.py` and `workspaces/nodes.py`: live-and-pending session and workspace nodes;
      `agents/nodes.py`: the live-and-pending agent node with intrinsic (row-carried) identity. The
      session factory MUST pass the SAME agent-node object as both the session's dep and the held
      harness's `target` field (one memoized object), so the harness's `target.realized` observes
      the node the orchestrator flips via `mark_realized`; two constructions would make the harness
      defer forever (first-consumer note, 2026-07-17). (Implementation note: with the harness
      capability not yet landed, the held machinery is the relocated required-commands check
      (`RequiredCommandsCheck`, `Readiness`-only, composed by the session node), carrying the
      four-way fork and probing through the context's target accessors; the harness instance
      replaces it, fork semantics unchanged. The one-object contract is enforced by the factory
      signature (nodes are handed in, never re-constructed) and proven by a
      defer-then-probe-after-the-flip test on the same object. The pending session node's `teardown`
      deliberately raises until the orchestrators land (its realizing slice defines its artifacts);
      the pending agent/workspace teardowns are today's `_rollback_ephemerals` bodies (forced
      deletes through the VM's bound platform), with the reverse-order oracle test proving
      agent-before-workspace. The SESSION `OperationScope` level rules landed with this box
      (requires vm/workspace/session, exactly one of agent/admin); WORKSPACE and AGENT stay
      non-constructible until their commands.) (Two further records, review round 2026-07-17: the
      SESSION-TEMPLATE node the HLA walkthrough lists is DEFERRED, not built: its only candidate
      readiness lives on the held required-commands check, and session env secrets ride the
      SecretTarget seam the push-two orchestrator keeps, so the node would be inert; it emerges if
      push two finds it forced, and the HLA carries the matching as-landed note. RULING: factories
      that internally construct their deps (`agent_template_node`'s credentials, `live_vm_node`'s
      site) may do so only while single-consumer; the first graph that reaches one such node from
      two factories moves construction up to the orchestrator, because handed-in nodes ARE the
      memo.)
- [x] Push two parity carries (review round 2026-07-17):
  - [x] restart parity asserts the required-commands probe fired AT PREFLIGHT, before the kill
        (matching HEAD's pre-kill guard), not merely "fired once"; (Where:
        `tests/sessions/test_create_restart_orchestrated.py` asserts the strict order, probe index
        before kill index, plus the missing-binary abort with the old session's row untouched.)
  - [x] an explicit session-teardown parity test lands with the orchestrators, so the pending
        session node's `NotImplementedError` placeholder cannot survive them (the unwind's
        warn-and-continue would otherwise swallow it silently); (Where: unit tests on the teardown
        body in `tests/sessions/test_session_nodes.py` (row delete, implicit-grant revoke, group
        removal only when no grant remains, admin mode, warn-never-raise) and the end-to-end order
        test in `tests/sessions/test_create_restart_orchestrated.py`: session slice cleans first,
        then agent, then workspace.)
  - [x] when the imperative `_assert_required_commands` retires, its docstring knowledge (the no-PTY
        "no job control" stderr note and the TTY-gated-PATH residual gap) migrates onto the check's
        probe documentation. (Done on `RequiredCommandsCheck._probe`; with the imperative copy gone
        the check is the only probe, so its result access hardened from
        `getattr(probe, "ok",     False)` to typed `.ok` at the same time, one shape, one copy.)
- [x] The PHASE-FREE realization choreography per creatable kind (the agent-realization body: agent
      ops plus the git-credential nodes' materials ops), factored as domain code with no phases and
      no resolve of its own, replacing the `git_tokens` + `own_root` nesting hack. (Implementation
      note: landed as `workspaces/realize.py` / `agents/realize.py`, the nested-call slices of
      `create_workspace` / `create_agent` at HEAD verbatim (messages, error wrapping, internal
      partial-state cleanup, the workspace body's grant-all reconciliation), minus the nested
      command roots' registry rebuild, re-validation, and re-gate. `git_tokens` arrive pre-resolved
      (the orchestrator reads each credential node's token through scoped delivery) and the
      materials ops keep running inside `_create_agent_on_vm` via `runup_and_filter`, the write-step
      skip-and-degrade seam Phase 2 recorded; `show_phases` is pinned False inside the body, so a
      body never resolves and never frames phases. The SESSION path's nesting hack is fully gone.
      The STANDALONE `agent create` / `agent reinit` / `workspace create` keep their imperative
      slices untouched (the sanctioned no-touch option), so the choreography is duplicated between
      each body and its standalone command until Phase 4 (agents) / Phase 5 (workspaces) retire the
      copies; `create_agent`'s now-caller-less `platform` / `git_tokens` parameters are likewise
      left for Phase 4 to remove with that migration, recorded here rather than half-migrated now.)
- [x] The `session create` / `session restart` orchestrators: build; gate; preflight-all (harness
      defers on its pending target); resolve; dependency-ordered roll-forward with
      `log.mark_realized` after each bespoke mutation; the command-shaped restart ordering (kill
      before the `restart` op) and its pinned non-rollbackable window. (Implementation notes.
      FLAG/PROMPT flow untouched; only the composition moved. BUILD: one shared `LiveVMNode`;
      live-or-pending workspace/agent via the push-one factories; the pending session factory now
      takes db/config (its teardown addresses through them) and wires the SAME agent object as dep
      and check target. The walk union and the pre-create `SecretTarget` both register on the ONE
      resolver, so what prompts at create is unchanged (no R7 note needed for hermeticity); restart
      keeps its recorded post-confirm `resolve_for_command` env resolve. GATE: `activation_gate` on
      the VM node replaces `ensure_active` plus BOTH `vm_active` holds; create preserves the
      reloaded-row semantics after the gate (rejoin may update `tailscale_host`), restart
      deliberately does not reload, matching HEAD. R7 exception records, all the same sanctioned
      pre-walk-away bucket as reinit's: (1) the gate now opens BEFORE the boundary resolve (HEAD
      resolved at bind, then gated), with just-in-time values seeding the resolver so nothing
      resolves or prompts twice (RULING, 2026-07-17: on a stopped VM this yields TWO prompt bursts,
      the gate's then a boundary or env-chain pass, which shares the Phase 1 prompt-session ruling:
      the invariant is all interactivity strictly pre-walk-away and nothing resolved or prompted
      twice; contiguity is not promised; pinned per command by the gate-prompt parity tests in
      `tests/sessions/test_create_restart_orchestrated.py`, mirroring the tracer's); (2) the
      required-commands probe moves to PREFLIGHT for realized targets, where HEAD probed
      post-resolve and post-mutation-start; the corollary at restart is that a missing binary or a
      pre-rollout agent's SSH refusal now surfaces BEFORE the BROKEN/--force and confirm gates
      (error-precedence shift, bail-earlier), and at create the existing-agent SSH probe now
      precedes the workspace realization (less to unwind on failure); (3) the nested creates' second
      gate probes are consolidated into the one held gate. ROLL-FORWARD: `RealizationLog`; workspace
      body, mark; agent body, mark; `log.unwind()` replaces `_rollback_ephemerals` (reverse order
      reproduces agent-then-workspace, proven end to end; the rollback-failure warning is now the
      log's generic teardown line, the Phase 2-accepted message shift). SESSION SLICE RULING: the
      completed session is deliberately NOT log-tracked. At HEAD nothing ever rolled back a
      completed session (a post-tmux failure unwound only the ephemerals and left the row and server
      standing), so the pending session node's `teardown` is a PARTIAL-state cleaner (the imperative
      session-internal rollback body: best-effort, warns, never raises) driven by the slice's own
      failure path, and `mark_realized` flips the node directly after the slice, outside the log,
      pinning the completed-session window as non-rollbackable; restart's post-kill window is pinned
      by no log existing at all. Seam catalog for these commands: (1) the harness seam is
      `_build_session_command` + `sessions.tmux.create_session` (the pane command string; the
      harness SDD's instance replaces both with `start`/`restart` ops, no orchestrator change); (2)
      the realize bodies duplicate the standalone command slices (previous box); (3) construct-time
      registration coexists with the walk union, as in the vm commands; (4) the session-template
      node stays deferred, nothing forced it (env secrets ride the target seam, readiness lives on
      the held check); (5) `_prepare_vm` and the imperative gate keep serving the un-migrated
      session commands (stop, delete, attach, batch ops). Oracle set: the existing session suites
      (`test_session_create_ephemeral`, `test_session_transport`, `test_error_wrapper`,
      `test_secrets_eager_resolve`, `test_sessions_tmux_create`) now drive the orchestrated path,
      with their stubs mechanically moved to the orchestrated seams (`resolve_site`, the
      reachability probe, the realize bodies, the resolver boundary); new proof tests live in
      `tests/sessions/test_create_restart_orchestrated.py`.)
- [x] The level-driven SKIP branch's first real exercise: a doctor scan reaching a harness (or a
      harness-like required-commands node) at SYSTEM level, asserting it NO-OPS rather than erroring
      (the branch the spike structurally could not prove). (Proven in `tests/test_session_nodes.py`:
      a SYSTEM-level context reaching the session node's required-commands check no-ops, even with
      no target at all, while the same check at SESSION level defers on pending, probes on realized,
      fires once, and is loud on an absent target.)
- [x] Coordinate with the re-scoped harness SDD: this pad drops the threaded `OperationIdentity` and
      `to_create` in favor of intrinsic layer-1 identity, the per-command operation scope, and
      pending nodes; the harness instance reads only the LEVEL off the operation scope and addresses
      via its own `session_name`. (Implementation note: the landing pad is live and the coordination
      contract is satisfied structurally: the orchestrators address the harness seam ONLY through
      the session node's own construction-time name and the scope's LEVEL. The held
      `RequiredCommandsCheck` (the harness-like machinery) is built with `session_name` and the
      target node, probes through `ctx.agent_target()` / `ctx.admin_target()`, and reads nothing off
      the scope but `level`; no `OperationIdentity` and no `to_create` exist anywhere in the path.
      What the harness SDD replaces: the held check becomes the harness instance's own readiness
      (fork semantics unchanged, same held slot on the session node), and `_build_session_command`'s
      command string becomes the instance's `start` / `restart` ops returning the pane command; the
      orchestrators themselves need no change.)

Definition of done: session create/restart orchestrated (against the real harness instance when the
harness SDD has landed, else the documented interim seam to today's imperative harness path); the
nested fan-out reproduces the `git_tokens` fold; the realization body is shared (Phase 4 consumes
it); the skip branch is proven; the full suite green.

## Phase 4: `agent create` / `agent reinit`

Goal: the standalone agent orchestrator wraps the Phase 3 realization body in its own phases,
retiring the last of the nesting hack.

- [x] The `agent create` / `agent reinit` orchestrators calling the shared realization body inside
      their own build/preflight/resolve/unwind. (Implementation notes. BUILD: create roots at the
      pending agent node (template edge carrying the credential nodes, VM edge from the row); reinit
      is the live-agent path with the template node as a second walk root, since the live row
      carries no template edge but the materials rewrite needs the tokens in the boundary union.
      Both gate on the VM via the shared `orchestration.activation.gate_secret_resolver`, frame the
      same Preflight / Resolving Secrets / Agent Initialization banners the imperative roots did,
      and read each token through scoped delivery. The AGENT `OperationScope` level rules landed
      with this box: required vm + agent, workspace FORBIDDEN, a deliberate correction to the HLA
      table's "vm, workspace, agent" sketch, because agents are VM-scoped in the current model (a
      workspace relationship is a grant, never identity); the HLA carries the matching as-landed
      note, WORKSPACE stays non-constructible, and a future workspace-rooted agent operation
      re-rules the field when it migrates. UNWIND RULING, from the imperative oracle: this command
      never unwinds a REALIZED agent (the body cleans its own half-configured user and re-raises
      before the row exists; failures after the row keep the agent, exactly as at HEAD), so no
      `RealizationLog` exists and the orchestrator flips `mark_realized` directly, the same
      completed-artifact pin the session slice recorded. `reinit` calls `_create_agent_on_vm`
      directly rather than the body: the body's row insert makes it create-shaped, and reinit shares
      the mutation beneath it, not the insert. SEAM CLOSURES from the Phase 3 catalog: the
      realize-body vs standalone duplication is CLOSED for agents (the standalone create now calls
      `realize_agent`, which gained the `grant_all_workspaces` parameter so the grant reconciliation
      keeps its imperative place between the row insert and the SSH-config refresh; the Phase 3
      note's "grant_all stays with the standalone command" is superseded by this box);
      `create_agent`'s dead `platform` / `git_tokens` parameters are removed as recorded;
      `_preflight_resolve_agent_git` and `vms.manager._resolve_git_tokens` retire with no callers
      (their behavior pins moved onto the node-based token fold in
      `tests/test_git_credentials_token_resolve.py` and the orchestrated suite). R7 exception
      records, the same sanctioned pre-walk-away bucket: the gate now opens before ANY resolve (HEAD
      create bound the site first and resolved tokens second; HEAD reinit resolved tokens first and
      site config second, two resolver instances), so both commands now run ONE boundary pass plus
      the gate's seeded just-in-time values, and a stranded site fails at BUILD, before any token
      prompt, where HEAD's reinit failed after them. Where proven:
      `tests/agents/test_create_reinit_orchestrated.py` (derived graph and union, per-command
      gate-prompt parity in the tracer's mirror shape, banner parity, mutation-failure cleanup with
      no row, reinit keeps the agent, AGENT scope reaching provider readiness, grant-all riding the
      body); `tests/test_operation_scope.py` carries the AGENT level's both-direction violation
      tests.)
- [x] Confirm the `git_tokens` / `show_phases` special-casing is fully gone (a body never resolves
      and never frames phases). (Confirmed structurally: `create_agent` lost the `git_tokens` /
      `platform` parameters and the `own_root` fork entirely; `_create_agent_on_vm` lost
      `show_phases` (the Agent Initialization banner is the orchestrators' framing now) and its
      `git_tokens` parameter is required, never Optional, so a caller that has not resolved cannot
      exist. The realization-body seam-contract test
      (`test_realize_bodies_take_domain_shaped_kwargs_only`) and the hermeticity inspection tests
      (`test_agent_create_does_not_eager_resolve_operator_env` and its reinit mirror) pin it.)

Definition of done: both commands orchestrated; the nesting hack removed; the full suite green.

## Phase 5: Remaining commands + resolver retirement

Goal: migrate the rest opportunistically, then remove the now-dead per-instance resolver.

- [x] `workspace create` orchestrated (this phase's first seam), with the WORKSPACE `OperationScope`
      level rules landed exactly as the HLA table sketched (required vm + workspace beyond the slug;
      forbidden agent, session; no table correction needed, unlike the AGENT row's); no level is
      left non-constructible. Composition, mirroring agent create: build (one `live_vm_node`, one
      pending workspace node, walk; the union registered on the resolver is site-only, because a
      workspace template's env secrets are runtime inputs, the hermeticity pin) -> gate -> preflight
      sweep -> one boundary resolve -> the shared realize body as the mutation -> `mark_realized`
      flipped DIRECTLY. No realization log, the same completed-artifact ruling as agent create: this
      command never unwinds a realized workspace (the body cleans its own partial files pre-row; a
      failure after the row keeps the workspace, as at HEAD). SEAM CLOSED from the Phase 3 catalog:
      the realize-body vs standalone duplication for workspaces; `realize_workspace` is now the
      SINGLE copy of the slice and returns the VS Code stub path for the standalone command's
      open-in-VS-Code tail (the session orchestrator ignores the return). `create_workspace`'s
      now-caller-less `platform` parameter is removed at the moment its docstring recorded. The
      realize body takes the RESOLVED template (mirroring the agent body): cheap validation,
      template resolution, the repo advisories, and the VM init-status guard, is the calling
      orchestrator's PRE-GATE duty at both call sites (the standalone command in its base order;
      session create at BUILD for a new workspace, an earlier surface than the nested call's
      mutation-time checks, the bail-early direction), so a bad template or an init-incomplete VM
      fails with zero prompts and zero VM starts, matching every migrated sibling's precedence
      (review ruling, 2026-07-18, reversing this box's first-landed shape which had relocated the
      validation into the body, post-gate; the reinit corollary about auto-starting before a
      preflight or resolve failure covers only checks that are INHERENTLY post-gate, and does not
      extend to validation relocated there). R7 exception records, all in the sanctioned
      pre-walk-away bucket: (1) the gate opens BEFORE any preflight or resolve (HEAD guarded, bound,
      and only then held `keep_active`); (2) the final "Workspace created" info prints exactly once,
      from the body, and now precedes the VS Code launch (HEAD launched first and printed last). The
      command frames NO phase banners: it never did, and bodies never frame. Where proven:
      `tests/workspaces/test_create_orchestrated.py` (derived graph and site-only union with the
      template-env hermeticity pin, an end-to-end hermeticity run against an env-bearing template,
      gate-prompt parity on stopped and reachable VMs in the tracer's mirror shape, the bad-template
      bail with zero resolve calls and zero gate events, mutation-failure cleanup with no row, the
      WORKSPACE scope reaching platform readiness); `tests/test_operation_scope.py` carries the
      WORKSPACE level's both-direction violation tests (the not-constructible-yet pin retires with
      the rules landing).
- [x] `vm start` / `vm stop` / `vm delete` orchestrated (2026-07-18), one green shippable unit
      superseding the vm-lifecycle portion of the next box (whose remaining scope is the shell /
      exec roots, the console commands, and the agent delete/grant/revoke migration with the
      `agents/manager.py` split). RULING: none of the three opens the activation gate. For start and
      stop the power op IS the command's operation (a command whose op is the state change does not
      converge state first); start CLEARS `operator_stopped` and stop SETS it, so the gate's intent
      flag is these commands' mutation, never their input, and the four-way relationship holds: stop
      records operator intent, start is the explicit operator start that clears it, the gate's
      auto-start elsewhere keeps respecting it. For delete the no-gate ruling is HEAD-derived: the
      imperative body never called `ensure_active` / `keep_active` (an operator-stopped VM would
      refuse, and broken states are what delete exists to clean up), used a hold-only `vm_active`
      span for the Tailscale logout, and never started a stopped VM to delete it; the never-gates
      oracle test pins zero status probes on an operator-stopped VM. All preserved exactly.
      COMPOSITION: the three commands share one composition root (`vms.manager._live_vm_boundary`),
      because their graphs are identical: the live VM node from the row (the site edge holds the
      platform), walk union = the site's config secrets only, VM-level scope, preflight sweep, ONE
      boundary resolve; the ops drive through `vm_node.site.platform`. Delete keeps its entire
      build-and-boundary inside the best-effort span (warn and skip backend cleanup; UserAbort never
      downgraded), and its child-count guard and confirm gate stay pre-boundary (zero prompts, zero
      resolves on a refused delete). Operator-stopped semantics verbatim: stop records intent BEFORE
      the already-stopped short-circuit and keeps the auto-vs-manual message fork; start clears the
      flag BEFORE the status probe; messages unchanged. R7: NO timing shifts, sanctioned or
      otherwise: the imperative bodies already ran preflight-then-resolve (`bind_platform`) at the
      exact point the orchestrated boundary now sits, and the sweep adds only the live VM node's
      no-op readiness to the imperative preflight set. Interim seams: construct-time registration
      beside the walk union, and the op-client bridge (both close with the resolver retirement);
      start's rejoin repair keeps `_ensure_tailscale`'s internal late resolve (no gate exists to
      hand a lazy reader through; the same conditional-need exception as HEAD); the imperative
      `ensure_active` / `keep_active` / `bind_platform` / `bind_platforms` / `keep_actives`
      machinery still serves every un-migrated VM-touching command and retires as they migrate.
      STILL-OPEN SEAM CATALOG (corrected 2026-07-18, review round: the first-landed list was a
      subset), the caller inventory the resolver-retirement box drains, re-derivable with
      `grep -rn "bind_platform\|ensure_active\|keep_active" cli/agentworks` (call sites, not
      definitions or docstrings; the pattern covers all five names by substring), grouped by module:
      `vms/manager.py` (`describe_vm`, `shell_vm`, `exec_vm`, `rekey_vm`, which is also
      `preflight_vm_template`'s last production caller, and `port_forward_vm`); `vms/backup.py`
      (`backup_vm`); `vms/initializer.py` (`initialize_vm`'s share-wait hold);
      `workspaces/manager.py` (`reinit_workspace`, `rehome_workspace` via `_rehome_vm`,
      `delete_workspace`, `copy_workspace`, and the deprecated `shell_workspace` /
      `console_workspace`); `agents/manager.py` (`delete_agent`, `shell_agent`, `exec_agent`,
      `grant_workspaces`, `revoke_workspaces`); `sessions/manager.py` (`_prepare_vm` serving the
      singular session ops `stop_session`, `delete_session`, `describe_session`, `attach_session`,
      and `session_logs`; `bind_platforms` + `keep_actives` serving the batch ops
      `stop_all_sessions`, `restart_all_sessions`, and `list_sessions`'s status pass);
      `sessions/console.py` (`attach_console`); `sessions/multi_console.py`
      (`_prepare_vm_target_for_attach`, the console attach/restore path). Where proven:
      `tests/vms/test_lifecycle_orchestrated.py` (the shared derived graph and union, per-command
      boundary bursts in the tracer's mirror shape with the stopped / running / already-stopped
      short-circuits, the flag semantics end to end against the real commands, the VM scope reaching
      platform readiness); `tests/vms/test_delete_vm_gating.py` (extended, never weakened: the
      no-gate boundary-burst pin, the stranded-site degrade with the manifest hint, both UserAbort
      pins, now driven through the orchestrated composition against the real registry/resolver and
      backend loop).
- [x] The shell / exec roots and the console attach paths orchestrated (2026-07-18), three green
      shippable units draining the next box further: `shell_vm` / `exec_vm`, `shell_agent` /
      `exec_agent`, and the console attach pair (`sessions/console.attach_console`,
      `sessions/multi_console._prepare_vm_target_for_attach` serving named-console attach and
      restore). SEAM SCOPE RULING (handing-off dev): the Phase 5 remaining-commands grouping "shell
      / exec roots and console commands" is exactly these six paths. EXPLICITLY LEFT imperative: the
      deprecated `shell_workspace` / `console_workspace` (dying code retires by deletion, not
      migration), `describe_vm` (read-only, do-not-over-orchestrate), `rekey_vm` (its migration is
      what retires `preflight_vm_template`; both untouched here), `port_forward_vm`, `backup_vm`,
      the initializer share-wait, workspace reinit/rehome/delete/copy, agent delete/grant/revoke
      (the later seam that also splits `agents/manager.py`), and all session singular/batch ops.
      GATE RULING: all six paths DO open the activation gate (HEAD: `keep_active` on shell/exec,
      `ensure_active` + caller-opened `vm_active` holds on the console paths). COMPOSITION: one
      shared gate-command root, `vms.manager.gated_vm_boundary` (public; agents and sessions import
      it like `bind_platform` before it), deliberately separate from the no-gate
      `_live_vm_boundary`: live VM node from the row, walk union AND the command's env-chain
      SecretTarget on the ONE resolver (`register_targets`, exactly the targets HEAD passed to
      `bind_platform`), VM-level scope for all six (the graph carries no agent or console node;
      agent shell/exec provision nothing agent-shaped), then
      `activation_gate(vm_node, gate_secret_resolver(...))` wrapping preflight-all, the one boundary
      resolve, env composition (`compose_env` still reads `resolver.values`), and the interactive /
      streaming span. ENV-TARGET SEAM: env secrets join the boundary via target registration, never
      the walk union (the hermeticity counterpart of the provisioning pins; union = site config
      secrets only, pinned per domain). CONSOLE RULINGS: NO console node (the plan said lazily;
      attach provisions nothing console-shaped, so the graph is the live VM alone and introducing
      one would over-orchestrate); no env-chain target registers on attach (HEAD passed none; the
      build panes keep their documented conditional-need late resolve);
      `_prepare_vm_target_for_attach` becomes a context manager yielding `(vm, target)` inside the
      gate span, and the gate's held-active span replaces the callers' own `vm_active` holds
      (keep-active parity across the SSH-heavy bodies and interactive attaches). Validation
      precedence preserved verbatim (exec's dash rejection, workspace resolution incl. the agent
      authz chain, `_guard_failed_vm` with `allow_failed_init` on shell/exec, tailscale guards) with
      ONE deliberate hoist: the console paths' no-Tailscale row guard moves PRE-GATE (HEAD checked
      it after `ensure_active`; the gate cannot populate the already-loaded row, so the command's
      own outcome is identical, the same bail-early direction as this phase's workspace-create
      ruling; hoists later would be the unsanctioned direction; amended 2026-07-18, review round:
      the first-landed "only removes a wasted prompt-and-start" over-claimed, because HEAD's
      post-gate order could start the stopped VM and its rejoin repopulated an empty address row,
      letting a RETRY of the command succeed; the hoist forgoes that accidental heal, and retries
      now fail until an explicit `vm start` or reinit). R7 records, all in the sanctioned
      pre-walk-away bucket: (1) the gate opens BEFORE preflight/resolve where HEAD bound
      (preflight + resolve) first and gated after, so a stopped VM sees two prompt bursts, gate then
      boundary, nothing resolved or prompted twice, contiguity not promised (the recorded
      prompt-session ruling); (2) the consoles' hold now also spans the preflight/resolve boundary
      and pre-attach body (a superset of HEAD's hold, the gate-span property every gated seam
      carries). STILL-OPEN CATALOG entries CLOSED from the vm-lifecycle box: `vms/manager.py`
      `shell_vm` + `exec_vm`; `agents/manager.py` `shell_agent` + `exec_agent`;
      `sessions/console.py` `attach_console`; `sessions/multi_console.py`
      `_prepare_vm_target_for_attach`. REMAINING: `describe_vm`, `rekey_vm`, `port_forward_vm`,
      `backup_vm`, `initialize_vm`'s share-wait, `reinit_workspace`, `rehome_workspace`,
      `delete_workspace`, `copy_workspace`, the deprecated pair, `delete_agent`, `grant_workspaces`,
      `revoke_workspaces`, `sessions/manager.py` `_prepare_vm` and the `bind_platforms` /
      `keep_actives` batch ops. Where proven: `tests/vms/test_shell_exec_orchestrated.py`,
      `tests/agents/test_shell_exec_orchestrated.py`,
      `tests/sessions/test_console_attach_orchestrated.py` (per-domain: graph + union with the
      env-target distinction pin, gate-prompt parity on reachable and stopped VMs in the tracer's
      mirror shape, pre-gate validation with zero resolves and zero gate events, the held-span order
      pin, VM scope reaching readiness); existing oracle suites mechanically re-seamed, assertions
      preserved (`test_vm_shell_provisioner.py`, `test_workspace_rooted_shells.py`,
      `test_secrets_eager_resolve.py`, `test_session_transport.py`).
- [ ] `vm delete`, `vm start` / `vm stop`, the shell / exec roots, and console commands (console
      nodes introduced lazily here), each a green shippable unit. The agent delete/grant/revoke
      migration in this phase also splits the overgrown `agents/manager.py` at that natural seam
      (deferred there deliberately, review ruling 2026-07-17, rather than splitting mid-migration).
- [ ] RESOLVER RETIREMENT once no migrated command depends on the bound resolver: drop the
      `resolver` constructor parameter from `Capability`; close the `preflight_vm_template` resolver
      seam (prediction is central now); kill proxmox's op-client bridge so `_api` reads the token
      from the context (`ctx.secret`) rather than the bound resolver, completing PR #182's
      direction. The caller inventory to drain first is the vm-lifecycle seam box's still-open seam
      catalog above.

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
