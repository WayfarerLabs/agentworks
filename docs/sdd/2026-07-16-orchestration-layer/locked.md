# Orchestration layer: locked

**Date:** 2026-07-18

This SDD is complete and locked. The artifacts in this directory (FRD, HLA, plan, spike findings)
are now immutable historical records; the permanent record lives in
`docs/adrs/0019-orchestration-layer-command-plans-over-node-graphs.md` (the decision),
`cli/agentworks/capabilities/README.md` (the capability lifecycle and declare/receive contract), and
the code itself (`cli/agentworks/orchestration/`, the per-domain `nodes.py` modules, and the
orchestrators in the domain managers).

## What shipped

Every VM-touching command in the CLI now runs as a plan over a derived node graph, composed by a
bespoke per-command orchestrator: `vm add-git-credential` (the tracer), `vm create` / `reinit` /
`start` / `stop` / `delete` / `describe` / `rekey` / `shell` / `exec` / `port-forward` / `backup`,
`workspace create` / `reinit` / `rehome` / `delete` / `copy`, `agent create` / `reinit` / `delete` /
`shell` / `exec` / `grant-workspaces` / `revoke-workspaces`, `session create` / `restart` and the
singular and batch session ops, and the console attach paths. The deprecated `workspace shell` /
`workspace console` pair was retired by deletion.

The shared surface, all emerged rather than designed up front:
`cli/agentworks/orchestration/{node,walk,secrets,readiness,activation,unwind}.py`. Node
implementations live in their domains (`vms/nodes.py`, `git_credentials/nodes.py`,
`agents/nodes.py`, `workspaces/nodes.py`, `sessions/nodes.py`). The `agents/manager.py` split
(`agents/grants.py`, `agents/initializer.py`) landed with the agent-op migration.

The per-instance secret resolver is fully retired: `Capability` construction binds `(name, config)`
and touches no secret machinery; the walk union over declared `secret_refs` is the boundary's only
source; resolvability prediction is central at the node preflights
(`orchestration.secrets.predict_resolution` / `require_predicted_refs`); the one boundary resolve
delivers through scoped readers, and `ctx.secret(name)` scoped delivery is the only way an instance
ever sees a secret value (VM-platform power ops take the op-start `RunContext`; gate-driven ops read
the gate's scoped reader). `OperationScope` levels landed for all five levels with
constructor-enforced field rules; every command passes the level of the entity it is ABOUT.

Deleted with their last callers: `bind_platform`, `bind_platforms`, `keep_actives`,
`vms.sites.platform_for`, `preflight_vm_template`, the `Capability` `resolver` parameter and
construct-time registration, `Resolver.predict`, proxmox's op-client bridge, the deprecated
workspace shell/console pair with their backends and completions entries.

## Requirements and architecture as landed

All FRD requirements are met; the FRD's rulings and the HLA's shapes landed as written except where
a phase's checked box records an as-landed divergence. The load-bearing divergences, each recorded
where it happened:

- **AGENT scope rules**: `vm` + `agent` required, `workspace` FORBIDDEN (agents are VM-scoped; a
  workspace relationship is a grant, never identity), correcting the HLA table's sketch.
- **The session is not realization-log-tracked**: the imperative command never rolled back a
  completed session, so `mark_realized` flips the session node directly and unwind covers
  agent-then-workspace (parity over the HLA sketch).
- **Console instances never became nodes**: no migrated command forced one (attach provisions
  nothing console-shaped; the graph is the live VM alone). The emerge-when-forced rule stands: a
  future console command that needs one introduces it then. The session-template node was likewise
  never forced (its only readiness rides the held required-commands check).
- **The gate's timing**: the activation gate opens BEFORE the preflight sweep (the imperative roots
  bound first and gated after); sanctioned as pre-walk-away, see the R7 ledger below.

## Phase history, briefly

- **Phase 0**: foundations forced by the tracer (Readiness/Node protocols, RunContext
  field-to-accessor flip, scope enum).
- **Phase 1**: the tracer, `vm add-git-credential`: derived graph, fatal-runup policy, scoped
  delivery, the activation gate, with the three documented interim seams (construct-time
  registration, the op-client bridge, instances'-own prediction).
- **Phase 2**: `vm create` / `vm reinit`: pending nodes, `RealizationLog` unwind, skip-and-degrade
  runup policy.
- **Phase 3**: `session create` (incl. `--new-agent`) and `session restart`: the realization
  choreography split (phase-free bodies), the ephemeral rollback onto node teardowns, the session
  node and required-commands check.
- **Phase 4**: `agent create` / `agent reinit`.
- **Phase 5**: everything else in seams (workspace create; vm lifecycle trio; shell/exec/console
  roots; agent delete/grant/revoke with the manager split; workspace lifecycle with the deprecated
  pair's deletion; sessions singular/batch; the stragglers describe/rekey/port-forward/backup), then
  the resolver retirement (constructor drop, central prediction, op-ctx convergence, the AGENT-scope
  lift, the never-again sweep).
- **Phase 6**: ADR 0019 promoted; permanent docs confirmed at HEAD; this lockfile.

The full test suite was the behavior oracle throughout (2008 tests at lock, all green); every phase
and seam landed as a complete, green, shippable unit.

## R7 exception ledger (sanctioned behavior records)

The named, sanctioned behavior records (amended 2026-07-18, closeout review round: entries 9 through
11 were in the phases' checked records but missing from this ledger's first writing); everything not
named here landed as parity under the suite oracle:

1. **Gate-before-boundary** (every gated seam): the gate opens before the preflight sweep and
   boundary resolve where the imperative roots bound (preflight + resolve) first and gated after. A
   stopped VM sees two prompt bursts (gate, then boundary), both before the walk-away point; nothing
   resolves or prompts twice. Reachable VMs cost nothing.
2. **Gate spans are equal-or-superset holds**: the console paths' and `describe_session`'s held
   spans cover more of the command than the imperative holds did (a no-op everywhere but WSL2).
3. **No-Tailscale guard hoists** (console attach, session singular ops): moved pre-gate; identical
   command outcome, but the hoist forgoes an accidental heal (a post-gate start's rejoin could
   repopulate the empty address row and let a RETRY succeed; retries now fail until an explicit
   `vm start` or reinit).
4. **`workspace create`**: the final "Workspace created" info prints once, from the body, and now
   precedes the VS Code launch (the imperative command launched first and printed last).
5. **`workspace copy`**: two sequential gate+boundary pairs (per VM), exactly as the imperative
   command ran two binds; the second pair's prompts still land mid-command, as before.
6. **Resolver retirement**: within the single boundary prompt session, prompting order follows the
   walk's deterministic first-encounter order rather than construction order; the set, session
   count, and walk-away point are unchanged.
7. **Zero-shift seams, recorded as such**: the vm lifecycle trio, rekey, describe, and the session
   batch ops (which keep the imperative boundary-then-gates order exactly).
8. **Behavior quirk pinned, not healed**: `agent revoke-workspaces --all` deletes explicit rows
   before its granted-workspaces snapshot, so an explicitly-only-granted workspace never gets its
   on-VM membership removed on that path (issue #189; fixing it is a behavior change outside this
   parity-bound effort).
9. **Error-shape shift at the sweep** (the tracer, then every derived-graph sweep): the preflight
   set is a strict superset of the imperative one (the sweep preflights every participating node),
   and an unresolvable token now fails at the sweep as a `ConfigError` with the secret-describe hint
   instead of the imperative `SecretUnavailableError` at the boundary resolve; accepted knowingly,
   buying cross-command consistency with the shape `vm reinit` already produced.
10. **Probe-to-preflight moves** (session create/restart): the required-commands probe moved to
    PREFLIGHT for realized targets where the imperative flow probed post-resolve and
    post-mutation-start, so a missing binary or a pre-rollout agent's SSH refusal now surfaces
    BEFORE the BROKEN/--force and confirm gates (bail-earlier error precedence), and at create the
    existing-agent SSH probe precedes the workspace realization (less to unwind); the nested
    creates' second gate probes consolidated into the one held gate.
11. **Rollback-failure message shape** (Phase 2, carried through Phase 3): the rollback-failure
    warning is the `RealizationLog`'s generic "rollback: teardown of <key> failed" line rather than
    each command's bespoke wording, an accepted message-shape shift on a failure-of-the-rollback
    path.

Permanent design exceptions (not migration artifacts): the gate's just-in-time secrets are the one
sanctioned resolution outside the boundary pass, and the Tailscale rejoin key keeps its
conditional-need late resolve on the repair path (documented at `_ensure_tailscale` and in the
activation module).

## Surviving riders and their owners

- **The imperative `ensure_active` / `keep_active` pair survives with exactly three recorded interim
  holds**: the `delete_agent` / `delete_workspace` nested-teardown paths (handed a bound platform
  plus its op-start context from the pending nodes' orchestrator-supplied callable; they close when
  the session-create unwind hands a node instead of a platform) and `initialize_vm`'s whole-init
  share-wait hold (closes when the initializer internals orchestrate). Owner: the effort that does
  the session-unwind node handoff and/or orchestrates the initializer.
- **`port_forward_vm`'s service-layer `sys.exit`** (pre-existing, now test-entrenched): belongs to
  whichever effort next touches port-forward.
- **Issue #189** (revoke --all membership drift): tracked; a deliberate behavior fix for a future
  change.
- **Recorded smells, not acted on**: the `workspaces.last_seen_at` column is write-dead since the
  deprecated pair's deletion (schema changes were out of scope); the tmuxinator workspace-console
  materials are still generated with no agw consumer.

## Where the permanent record lives

- `docs/adrs/0019-orchestration-layer-command-plans-over-node-graphs.md`: the decision, standalone.
- `cli/agentworks/capabilities/README.md`: the capability model and the completed declare/receive
  contract.
- `docs/guides/resources.md`: the operator-facing resource/secret surface (unchanged by this effort
  beyond parity).
- The code and its docstrings: `cli/agentworks/orchestration/` and the domain `nodes.py` modules
  carry the contracts; the orchestrators carry each command's composition.

Nothing under `docs/sdd/2026-07-16-orchestration-layer/` is load-bearing for the current system;
this directory can be deleted per the SDD lifecycle once its history stops informing current work.

## Amendments

- **2026-07-18 (closeout review round, same day as the lock, before push):** the R7 ledger gained
  entries 9 through 11 (the sweep error-shape shift, the probe-to-preflight moves, the
  rollback-failure message shape), which the phases' checked records carried but the ledger's first
  writing omitted, and its "everything else is verbatim parity" framing was softened to match. In
  the permanent docs, ADR 0019's node taxonomy gained the resolved-template species, its gate
  section was qualified (no-gate commands and boundary-first gates that serve the cached pass), and
  its "one prompt session" phrasing was replaced by the proven invariant (all interactivity
  pre-walk-away, nothing resolved or prompted twice, one boundary pass per composition root);
  `capabilities/README.md` and `docs/guides/resources.md` had the same prompted-exactly-once
  overstatement corrected to that invariant (the guide's claim was equally inaccurate before this
  effort, e.g. cross-VM copy's two passes, and was fixed under the docs-reflect-HEAD tiebreaker).
