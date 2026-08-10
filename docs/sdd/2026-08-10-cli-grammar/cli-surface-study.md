# Agentworks CLI 0.14 Grammar Rework, Design Study

- Status: Input artifact with corrections; every vocabulary/structure judgment requires individual
  operator ratification in the design cycle (see frd.md). CORRECTION (operator ruling, 2026-08-10):
  deviation item 6 is WITHDRAWN and question 2 is answered NO. `reinit` and `repair` are
  deliberately distinct verbs: `reinit` means the resource supports full idempotent
  re-initialization (vm, agent); `repair` connotes partial idempotent reconciliation of what can be
  safely converged when full re-initialization would destroy live work (workspace: the git repo
  cannot be re-initialized). Item 7's console design must be re-derived under that taxonomy (a full
  console rebuild is closer to reinit semantics than a repair flag). The study's INVENTORY is
  code-verified; its VOCABULARY judgments are proposals only.
- Date: 2026-08-10
- Basis: origin/main @ 94c551c8; produced by a saga-lead-chartered deep study of every command's
  implementation. This document is the R1 input for the cli-grammar effort; the operator-blessed
  version becomes the verb contract the FRD builds on.

## 1. The verb contract

**The noun model the surface must teach.** Four planes: (a) **declarations**, registry resources
from manifests (`resource`, `secret`); (b) **live instances**, DB-backed things with lifecycles
(`vm`, `workspace`, `agent`, `session`, `console`); (c) **computed views**, derived, never stored
(`env`, `graph`); (d) **system verbs**, the installation itself (`doctor`, `guide`, `config`,
`completion`, `version`). Every verb below states which planes it applies to.

### Normative verbs

| Verb                                                                        | Contract (one line)                                                                                                                                                                                                                                                                                                                                                                                                       |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `create NAME`                                                               | Bring a new instance into existence; where the noun is a workload (session), created means started. Positional = identity; flags = anchors and variations.                                                                                                                                                                                                                                                                |
| `list`                                                                      | Enumerate a noun; filter flags (CSV=OR within, flags AND across, unknown name = hard error), `--names-only`, `--output json`. Render-only work skipped under `--names-only`.                                                                                                                                                                                                                                              |
| `describe TARGET`                                                           | The kind-aware card for ONE specific node: identity, declaring file:line (declarations) or DB origin (instances), origin, readiness, kind-specific facts via the per-kind detail renderer. NO relationship sections; exactly one pointer line to `graph`. `KIND/NAME` where not kind-locked; bare `NAME` where the group implies the kind; kind-locked spellings are thin sugar over the same renderer, pinned by a test. |
| `explain [KIND[/IMPL][.FIELD.PATH]]`                                        | Type documentation: what a kind (or one capability implementation, or one field) accepts. Config-independent, answers on a broken config. Bare invocation lists all kinds (absorbs `resource kinds`).                                                                                                                                                                                                                     |
| `graph [NODE ...]`                                                          | ALL relational views: whole graph, focal nodes, `--kind` filter, `--up/--down`, `--depth`, formats incl. named-consumers. Nodes span declarations and live instances under one `KIND/NAME` grammar.                                                                                                                                                                                                                       |
| `repair TARGET [SUBTARGET]`                                                 | Idempotently reconcile live state to declared state. Reports what it converged. Never silently destroys live work; destructive convergence is an explicit flag (`--rebuild`) behind confirm + `--yes`.                                                                                                                                                                                                                    |
| `verify [TARGETS]`                                                          | Read-only proof of a stated claim (connection works, secret resolves). Per-item outcomes in request order; exit 0 all pass, 1 if any failed. Defaults to refusing interaction (`--allow-interaction` opts in), a proof must not be satisfied by prompting the operator mid-proof.                                                                                                                                         |
| `start` / `stop`                                                            | Power semantics on the noun itself. No confirmation (reversible).                                                                                                                                                                                                                                                                                                                                                         |
| `resume`                                                                    | Restart a session workload preserving its logical identity, running harness resume hooks. Deliberately not `start` (section 4).                                                                                                                                                                                                                                                                                           |
| `attach`                                                                    | Join a live interactive surface. May build from _nothing_ (first attach); never repairs partial state, that is `repair`'s job.                                                                                                                                                                                                                                                                                            |
| `delete`                                                                    | Remove the instance and its owned state. Confirm + `--yes`; `--force` = dependency override, its ONLY meaning surface-wide.                                                                                                                                                                                                                                                                                               |
| `logs NAME`                                                                 | The named thing's own captured workload output, best available source (today: tmux scrollback; the observability wave later re-backs it with the event store). `--lines/-n`. A command that shows anything else may not be called `logs`.                                                                                                                                                                                 |
| `exec` / `shell` / `port-forward`                                           | Run-a-child commands; the only sanctioned exit-code passthrough (plus `attach`).                                                                                                                                                                                                                                                                                                                                          |
| `edit`                                                                      | Open the declaring file in `$EDITOR`, with the broken-config fallback scan (`resource edit`'s fix-it path is the pattern).                                                                                                                                                                                                                                                                                                |
| `sample` / `schema`                                                         | Authoring surfaces; `schema` is the machine twin of `explain`.                                                                                                                                                                                                                                                                                                                                                            |
| `backup`                                                                    | Export an archive of an instance. (`agw database backup/restore` arrives via safer-migrations; `vm restore` deferred, section 4.)                                                                                                                                                                                                                                                                                         |
| `sync`                                                                      | Regenerate workstation-local artifacts derived from live state (SSH config entries, VS Code workspace files). Idempotent; the local-plane cousin of `repair`.                                                                                                                                                                                                                                                             |
| `grant-X` / `revoke-X`                                                      | Relationship edits, always from the owning side (agent side for workspace access).                                                                                                                                                                                                                                                                                                                                        |
| `add-X` / `remove-X` / `reorder-X`                                          | Membership edits on composites (console), editing _declared_ membership; `repair` converges live state to it. Plural X = variadic operands.                                                                                                                                                                                                                                                                               |
| `guide` / `doctor` / `completion` / `version` / `config init\|edit\|sample` | System verbs; shapes unchanged (guide ruled on in section 4).                                                                                                                                                                                                                                                                                                                                                             |

### Cross-cutting rules

1. **Positional = identity, flags = variation.** Operands are variadic positionals; list-command
   filters are CSV-valued flags (existing rule, reaffirmed). The only sanctioned per-item
   mini-grammar is the console session spec `name+N` (an attribute of each operand, inexpressible as
   a flag).
2. **Inline dependency creation** is allowed only where the composite operation needs atomic
   rollback the operator cannot compose from two commands,
   `session create --new-workspace/--new-agent` qualifies (rollback machinery in
   `sessions/manager/_create_roll.py`); everywhere else, compose two commands.
3. **`--output json`** on every read-only inspector: all `list`/`describe`, `env`, `graph`,
   `doctor`, `secret verify`. Not on `explain` (schema is its machine form) except the bare kinds
   listing, and never on `guide` (markdown-only by operator ruling).
4. **`--names-only`** on every list surface, incl. bare `explain`, mutually exclusive with
   `--output json`.
5. **Exit codes**: 0 success, 1 domain failure (incl. any verify/doctor/guide failure), 2 usage.
   Child passthrough only for exec/shell/attach/port-forward.
6. **Destructive ops** confirm + `--yes` uniformly (delete, `remove-sessions`,
   `rehome --remove-old`, `repair --rebuild`).
7. **`--force`** = dependency override on delete, nothing else. Broken-session PID-kill = `--kill`.
8. **`--write` always takes a path**; a fixed-destination writer is named for what it does
   (`--install`).
9. **`KIND/NAME`** is the one node grammar across `describe`, `graph`, `env`, and completions (`/`
   is parse-safe; enforced at `Registry.add`).

## 2. The group sentences

| Group                 | Sentence                                                                                                          | Moves                                                                                                                                            |
| --------------------- | ----------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `vm`                  | Manage VM instances: lifecycle, access, and per-VM maintenance.                                                   | ,                                                                                                                                                |
| `workspace`           | Manage workspaces: shared project directories living on a VM.                                                     | ,                                                                                                                                                |
| `agent`               | Manage agents: isolated Linux users on VMs, and their workspace access.                                           | ,                                                                                                                                                |
| `session`             | Manage sessions: persistent tmux workloads run by an agent or the admin in a workspace.                           | ,                                                                                                                                                |
| `console`             | Manage named consoles: _declared_, curated tmux views over a VM's sessions (DB declares; attach/repair converge). | `restore-session` → `repair`                                                                                                                     |
| `config`              | The workstation's agentworks configuration: the settings file, plus regeneration of locally derived artifacts.    | `sync-vscode-workspaces` + `sync-ssh-config` → one `config sync`                                                                                 |
| `resource`            | The declarative resource model: registry-wide listing, type documentation, and authoring utilities.               | `describe` → top-level `agw describe` (Q1); `kinds` → bare `explain`; `describe-kind` → `explain`; `enable/disable` arrive via installer-plugins |
| `secret`              | Inspect and prove declared secrets and their source mappings, always value-free.                                  | ,                                                                                                                                                |
| `agw describe` (new)  | The card for one node, declaration or instance, `KIND/NAME`.                                                      | absorbs `resource describe`                                                                                                                      |
| `agw graph` (new)     | Relational views over the whole node universe.                                                                    | absorbs all relationship sections                                                                                                                |
| `agw env` (new shape) | The effective agentworks-managed environment for one instance anchor.                                             | `env show --session X` → `agw env session/X`                                                                                                     |
| `guide`               | Serve authored guidance plus safe live facts as markdown, for humans and agents alike.                            | ,                                                                                                                                                |
| `doctor`              | Diagnose this workstation's installation, read-only.                                                              | ,                                                                                                                                                |
| `completion`          | Generate or install shell completions.                                                                            | ,                                                                                                                                                |
| `version`             | Print the installed CLI version.                                                                                  | ,                                                                                                                                                |

## 3. The deviation worklist

Every rename also touches completions and gets an upgrade-guide entry unless the operator waives per
the compatibility posture. Price legend: CHEAP / MODERATE / DEEP.

### Fixed points, concretized

1. `resource describe-kind TARGET` → `resource explain [TARGET[.FIELD.PATH]]`, CHEAP; field-path
   drill-down is additive grammar later.
2. `resource kinds` → bare `agw resource explain` (kinds table; keeps `--names-only`/`--output json`
   for this listing arm only), MODERATE. Teaches the documentation ladder: nothing→kinds,
   kind→fields, kind/impl→impl fields.
3. `resource describe KIND/NAME` → top-level `agw describe KIND/NAME`; all group describes become
   kind-locked sugar over the same renderer, pinned identical by test; relationship sections removed
   in favor of one graph pointer line, DEEP; do together with `graph` (shared node universe). JSON
   envelope shape change for every `*.describe`, call out loudly in the upgrade guide.
4. New `agw graph`, DEEP. Nodes = registry resources + DB instances;
   `--up/--down/--depth/--kind/ --format`.
5. `vm verify-connection` → `vm verify`, CHEAP; docstring states exactly what claim it proves.
6. `vm reinit` → `vm repair`; `agent reinit` → `agent repair`, CHEAP each; the code already calls
   them the same convergence as `workspace repair`. `agent reinit --update-template` →
   `agent repair --set-template`.
7. `console restore-session NAME SESSION` → `console repair NAME [SESSION]`, MODERATE:
   - `console repair NAME` (whole console): tmux absent → build; present → per-window reconcile of
     every member. Surplus live panes / killed session-panes are **reported as drift, never killed**
     , repair's default is additive convergence because a live pane can hold work.
   - `console repair NAME SESSION`: same, scoped to one window (today's restore-session).
   - `console repair NAME --rebuild`: kill and rebuild from declaration, the honest replacement for
     `console attach --recreate`. Destructive ⇒ confirm + `--yes`.
   - `console attach` loses `--recreate`, keeps `--allow-nesting` and first-attach materialization.
8. `--force` split, CHEAP: `session stop/resume/delete --force` → `--kill`; delete-family keeps
   `--force` (dependency override).
9. `resource schema --write` (boolean, fixed destination) → `resource schema --install`, CHEAP; the
   fixed destination is justified (schema modeline comments reference it by path), so the flag is
   renamed for what it does.
10. `resource enable/disable` (installer-plugins) and `agw database backup/restore`
    (safer-migrations) arrive on their own schedules; the vocabulary reserves the spellings.

### Beyond the fixed points

1. **The logs ruling**: `logs` = the thing's own workload output. `session logs` already satisfies
   it (scrollback today; the observability wave re-backs the same spelling later). `vm logs`
   violates it, it prints agentworks' own SSH operation transcripts from the host-side log dir.
   Rename → `vm ssh-logs` (keeps `--all`), CHEAP. Reserves `vm logs` for genuine VM output if it
   ever exists.
2. `env show --vm/--workspace/--agent/--session` → top-level `agw env ANCHOR` (`KIND/NAME`),
   MODERATE. Four mutually-exclusive anchor flags collapse into the uniform node grammar. Keeps
   `--resolve`; gains `--output json`.
3. `secret verify`: add `--all` (prove the whole secret config) and `--output json` (the per-item
   outcome table is already the right data shape), CHEAP + CHEAP.
4. `config sync-vscode-workspaces` + `config sync-ssh-config` → `config sync` (one idempotent pass;
   `--only vscode|ssh` if ever needed), CHEAP. Fixes the `config ... config` stutter.
5. **session create reshape**, MODERATE. `--workspace`/`--agent` become the single name slot for
   both existing and ephemeral, with `--new-workspace`/`--new-agent` as booleans switching
   lookup→create (name defaults to the session name when omitted). Drops `--workspace-name`/
   `--agent-name`; 11 → 9 flags, one rule instead of two spellings per dependency. Inline creation
   itself is kept: atomic rollback is real value not composable from two commands.
6. `vm rekey --ignore-env`, help hard-codes the pre-sources backend model; re-spell against the
   synthesized-source model (working name `--prompt`), CHEAP, sequenced with the secrets wave.
7. `console reorder-sessions` help under-states (it bumps to front, not full reorder), CHEAP,
   docs-only.
8. New `console remove-shell NAME SESSION [POSITION]`, required for repair honesty: once repair
   re-adds killed panes, killing a pane is no longer a way to remove a shell; declared shells need
   an editable downward path, MODERATE.
9. Exit-code + confirmation audit across all commands against rules 5-7, CHEAP, mostly verification.

## 4. Declined symmetries and deliberate exceptions

- **`session resume`, not `session start`**: resume re-runs harness resume hooks preserving logical
  identity; the distinction is real. Recorded; do not "fix".
- **`secret verify` refuses interaction by default** while everything else is
  interactive-by-default: a proof that prompts you is not a proof. The posture extends verb-wide
  with the verify contract.
- **`guide`'s protocol shape is correct**: guide IS a protocol endpoint (the driving agent lives
  outside Agentworks; the #462 JSON action contract consumes its computed exit codes and
  `--evidence` replay). Bless and document; no grammar change.
- **`vm backup` without `vm restore`**: DEFER (restore is a provision-plus-rehydrate effort).
  Interim: document the archive layout so backups are honestly consumable by hand. Ledger, not 0.14.
- **One-sided workspace grants**: DECLINE, one owner per relationship edit; `graph` shows both
  directions, which was the only reader-side need.
- **No batch `--all` on vm/workspace/agent/console lifecycle ops**: DECLINE by cardinality, sessions
  are fleets; VMs are few and batch delete is a footgun; VM idle-stop belongs to the observability
  wave's auto-suspend.
- **`explain` has no `--output json`** (except the kinds listing): `resource schema` is its machine
  twin; two machine formats of one reference would drift.
- **Console spec grammar `name+N`** stays: the surface's one sanctioned per-operand mini-grammar.
- **Kind-locked describe sugar retained**: discoverability inside groups is worth the second
  spelling; the pinned-equality test makes it safe.
- **`console attach` first-attach materialization kept**: building from nothing is creation
  semantics under the declared-state model; only re-building was the awkward part, and that moved to
  `repair --rebuild`.
- **`repair --check` declined**: the read/write split is `verify`/`repair`, not a flag. A full-state
  `workspace verify` may arrive when someone actually needs drift-report-without-touch.
- **`vm add-git-credential` rename DEFERRED**: the harness-scope wave reshapes credential
  application ownership; renaming twice is worse than once.
- **`vm shell --platform`** stays (honest escape hatch); **`config init`/`edit` fixed paths** stay
  (the settings file is a singleton).

## 5. Open questions for the operator

1. **Top-level `agw describe KIND/NAME` over one node universe (declarations + instances), retiring
   `resource describe`?** The load-bearing structural call: describe and graph share one node
   universe, and the not-kind-locked grammar gets a home. Recommend: **yes**.
2. **`vm reinit`/`agent reinit` → `repair`?** Breaking rename of two muscle-memory commands for the
   one-verb convergence story. Recommend: **yes**.
3. **`agw env ANCHOR` top-level, dissolving the env group-of-one and its four anchor flags?**
   Recommend: **yes**.
4. **Console repair posture as specified** (additive by default, drift reported never killed,
   `--rebuild` as the destructive path, attach keeps first-attach build, new `remove-shell`)?
   Recommend: **as stated**.
5. **session create flag reshape** (drop `--workspace-name`/`--agent-name`; `--workspace`/`--agent`
   name the ephemeral when `--new-*` is set)? Breaking for scripts. Recommend: **reshape**.
