# Agentworks CLI 0.14 Grammar Rework, Design Study

<!-- cspell:words Graphviz -->

- Status: Revised study input. Inventory statements are code-verified; vocabulary and structure
  statements are proposals until individually ratified through the verb-contract review.
- Date: 2026-08-10
- Basis: Seed produced from `origin/main` at `94c551c8`; re-audited on `docs/cli-grammar-seed` at
  `284b447b`. Its merge-base with the observed `origin/main` at `e2bf898e` is `4d010d42`. See
  `current-surface-audit.md`, `node-model-study.md`, and `prior-art-research.md` for the supporting
  studies.

The operator ruled on 2026-08-10 that `reinit` and `repair` are deliberately distinct. `reinit`
means full idempotent re-initialization where the resource supports it. `repair` means partial
idempotent reconciliation where full re-initialization would destroy live work. The withdrawn rename
and stale open question have been removed from this revision. Console lifecycle vocabulary must be
derived under this distinction; a full console rebuild is closer to reinit than repair.

## 1. Candidate verb contract

**The noun model the surface must teach.** Four planes: (a) **declarations**, registry resources
from manifests (`resource`, `secret`); (b) **live instances**, DB-backed things with lifecycles
(`vm`, `workspace`, `agent`, `session`, `console`); (c) **computed views**, derived, never stored
(`env`, `graph`); (d) **system verbs**, the installation itself (`doctor`, `guide`, `config`,
`completion`, `version`). Every verb below states which planes it applies to.

### Candidate verbs

| Verb                                                                        | Contract (one line)                                                                                                                                                                                                                                                                                                                                                         |
| --------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `create NAME`                                                               | Bring a new instance into existence; where the noun is a workload (session), created means started. Positional = identity; flags = anchors and variations.                                                                                                                                                                                                                  |
| `list`                                                                      | Enumerate a noun; filter flags (CSV=OR within, flags AND across, unknown name = hard error), `--names-only`, `--output json`. Render-only work skipped under `--names-only`.                                                                                                                                                                                                |
| `describe TARGET`                                                           | The kind-aware card for ONE specific node: identity, declaration origin or defined instance provenance, readiness, and kind-specific facts. No relationship sections; one pointer to `graph`. `KIND/NAME` where not kind-locked and bare `NAME` where a group implies the kind. Whether group describe commands remain, and what equality they promise, are open decisions. |
| `explain [KIND[/IMPL][.FIELD.PATH]]`                                        | Type documentation: what a kind (or one capability implementation, or one field) accepts. Config-independent, answers on a broken config. Bare invocation lists all kinds (absorbs `resource kinds`).                                                                                                                                                                       |
| `graph [NODE ...]`                                                          | Relational views over an approved read boundary spanning declarations and live instances under one `KIND/NAME` grammar. Direct versus derived edges, architecture, direction vocabulary, depth, filtering, and output encodings require explicit rulings.                                                                                                                   |
| `repair TARGET [SUBTARGET]`                                                 | Idempotently reconcile what can be safely converged without destroying live work. Reports what it converged and what it could not. A full destructive reset is reinit semantics where the noun can support it, not a repair mode.                                                                                                                                           |
| `verify [TARGETS]`                                                          | Read-only proof of a stated claim (connection works, secret resolves). Per-item outcomes in request order; exit 0 all pass, 1 if any failed. Defaults to refusing interaction (`--allow-interaction` opts in), a proof must not be satisfied by prompting the operator mid-proof.                                                                                           |
| `start` / `stop`                                                            | Power semantics on the noun itself. No confirmation (reversible).                                                                                                                                                                                                                                                                                                           |
| `resume`                                                                    | Restart a session workload preserving its logical identity, running harness resume hooks. Deliberately not `start` (section 4).                                                                                                                                                                                                                                             |
| `attach`                                                                    | Join a live interactive surface. May build from _nothing_ (first attach); never repairs partial state, that is `repair`'s job.                                                                                                                                                                                                                                              |
| `delete`                                                                    | Remove the instance and its owned state. Confirm + `--yes`; `--force` = dependency override, its ONLY meaning surface-wide.                                                                                                                                                                                                                                                 |
| `logs NAME`                                                                 | The named thing's own captured workload output, best available source (today: tmux scrollback; the observability wave later re-backs it with the event store). `--lines/-n`. A command that shows anything else may not be called `logs`.                                                                                                                                   |
| `exec` / `shell` / `port-forward`                                           | Run-a-child commands; preserve the child status. `attach` and editor-launching commands share that status rule.                                                                                                                                                                                                                                                             |
| `edit`                                                                      | Open the declaring file in `$EDITOR`, with the broken-config fallback scan (`resource edit`'s fix-it path is the pattern).                                                                                                                                                                                                                                                  |
| `sample` / `schema`                                                         | Authoring surfaces. `schema` emits machine-readable manifest schemas for the kinds it supports; it does not cover every capability implementation that `explain` can document.                                                                                                                                                                                              |
| `backup`                                                                    | Export an archive of an instance. (`agw database backup/restore` arrives via safer-migrations; `vm restore` deferred, section 4.)                                                                                                                                                                                                                                           |
| `sync`                                                                      | Regenerate workstation-local artifacts derived from live state (SSH config entries, VS Code workspace files). Idempotent; the local-plane cousin of `repair`.                                                                                                                                                                                                               |
| `grant-X` / `revoke-X`                                                      | Relationship edits, always from the owning side (agent side for workspace access).                                                                                                                                                                                                                                                                                          |
| `add-X` / `remove-X` / `reorder-X`                                          | Membership edits on composites (console), editing _declared_ membership; `repair` converges live state to it. Plural X = variadic operands.                                                                                                                                                                                                                                 |
| `guide` / `doctor` / `completion` / `version` / `config init\|edit\|sample` | System verbs; shapes unchanged (guide ruled on in section 4).                                                                                                                                                                                                                                                                                                               |

### Cross-cutting rules

1. **Positional = identity, flags = variation.** Operands are variadic positionals; list-command
   filters are CSV-valued flags (existing rule, reaffirmed). The only sanctioned per-item
   mini-grammar is the console session spec `name+N` (an attribute of each operand, inexpressible as
   a flag).
2. **Inline dependency creation** is allowed only where the composite operation needs atomic
   rollback the operator cannot compose from two commands,
   `session create --new-workspace/--new-agent` qualifies (rollback machinery in
   `sessions/manager/_create_roll.py`); everywhere else, compose two commands.
3. **`--output json`** on approved read-only inspectors. Existing `list`/`describe` and `doctor`
   have v1 contracts. Proposed `env`, `graph`, and verify output requires a schema-version ruling;
   `env --resolve` also requires an explicit secret-safety rule. Not on `explain` (schema is its
   machine form) except a possible bare kinds listing, and never on `guide` (markdown-only by
   operator ruling).
4. **`--names-only`** on every list surface, incl. bare `explain`, mutually exclusive with
   `--output json`.
5. **Exit codes**: 0 success, 1 domain failure (including any verify/doctor/guide failure), 2 usage,
   and 130 for Ctrl-C. Commands whose contract is to run a child preserve its status, including
   exec, shell, attach, port-forward, and editor launchers.
6. **Destructive ops** confirm + `--yes` uniformly. The contract needs a reviewed matrix covering
   deletes, membership removal, rehome, console recreation, schema/completion installation, and
   completion uninstall rather than assuming each current confirmation boundary is correct.
7. **`--force`** = dependency override on delete, nothing else. Broken-session PID-kill = `--kill`.
8. **`--write` always takes a path**; a fixed-destination writer is named for what it does
   (`--install`).
9. **`KIND/NAME`** is the candidate node grammar across generic `describe`, `graph`, eligible `env`
   anchors, and completions. `/` is parse-safe for registry resources. Live-kind names and
   config-free completion behavior still need a contract.

## 2. The group sentences

| Group                 | Sentence                                                                                                       | Moves                                                                                                                    |
| --------------------- | -------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `vm`                  | Manage VM instances: lifecycle, access, and per-VM maintenance.                                                | ,                                                                                                                        |
| `workspace`           | Manage workspaces: shared project directories living on a VM.                                                  | ,                                                                                                                        |
| `agent`               | Manage agents: isolated Linux users on VMs, and their workspace access.                                        | ,                                                                                                                        |
| `session`             | Manage sessions: persistent tmux workloads run by an agent or the admin in a workspace.                        | ,                                                                                                                        |
| `console`             | Manage named consoles: declared, curated tmux views over a VM's sessions.                                      | Candidate lifecycle changes require Q7.                                                                                  |
| `config`              | The workstation's agentworks configuration: the settings file, plus regeneration of locally derived artifacts. | Candidate: combine the two sync commands, subject to Q11.                                                                |
| `resource`            | The declarative resource model: registry-wide listing, type documentation, and authoring utilities.            | Candidate homes for `describe`, `kinds`, and `explain` require Q1 and Q5; `enable/disable` arrive via installer-plugins. |
| `secret`              | Inspect and prove declared secrets and their source mappings, always value-free.                               | ,                                                                                                                        |
| `agw describe` (new)  | The card for one node, declaration or instance, `KIND/NAME`.                                                   | absorbs `resource describe`                                                                                              |
| `agw graph` (new)     | Relational views over the whole node universe.                                                                 | absorbs all relationship sections                                                                                        |
| `agw env` (new shape) | The effective agentworks-managed environment for one instance anchor.                                          | Candidate: `env show --session X` becomes `agw env session/X`.                                                           |
| `guide`               | Serve authored guidance plus safe live facts as markdown, for humans and agents alike.                         | ,                                                                                                                        |
| `doctor`              | Diagnose this workstation's installation, read-only.                                                           | ,                                                                                                                        |
| `completion`          | Generate or install shell completions.                                                                         | ,                                                                                                                        |
| `version`             | Print the installed CLI version.                                                                               | ,                                                                                                                        |

## 3. The deviation worklist

Every rename also touches completions and gets an upgrade-guide entry unless the operator waives per
the compatibility posture. Price legend: CHEAP / MODERATE / DEEP.

### Settled and candidate changes

1. `resource describe-kind TARGET` becomes `resource explain [TARGET[.FIELD.PATH]]`, CHEAP;
   field-path drill-down is additive grammar later.
2. Candidate: `resource kinds` becomes bare `agw resource explain` (kinds table; keeps
   `--names-only`/`--output json` for this listing arm only), MODERATE. Prior art keeps type listing
   separate from explanation, so this remains a local discoverability decision.
3. Candidate: `resource describe KIND/NAME` becomes top-level `agw describe KIND/NAME`, with group
   commands either retired or adapted to shared fact records, DEEP. Removing relationship sections
   and adding graph should be one responsibility change. Existing describe commands have different
   live probes and versioned JSON command IDs; renderer equality cannot be promised until the JSON
   compatibility boundary is chosen.
4. Candidate: new `agw graph`, DEEP. Nodes = registry resources + DB instances. A request-scoped
   inspection snapshot is one architecture candidate; composing existing domain query services is
   the other credible baseline. Direction, depth, direct/derived edges, filtering, output encodings,
   ordering, and cycle behavior all require day-one consumer decisions.
5. `vm verify-connection` becomes `vm verify`, CHEAP; docstring states exactly what claim it proves.
6. **Settled: keep `vm reinit` and `agent reinit`.** Correct workspace command and manager prose
   that falsely calls repair their analog. The meanings differ by the safety of full
   re-initialization.
7. Candidate console lifecycle split, DEEP:
   - Preserve a scoped non-destructive operation equivalent to today's
     `console restore-session NAME SESSION`; decide whether its honest name is repair or restore.
   - Design whole-console additive reconciliation only if a named operator workflow needs it.
   - Treat kill-and-rebuild as reinit semantics, not a repair flag. If retained, move it out of
     `attach --recreate`, confirm it, and accept `--yes`.
   - Keep first-attach materialization unless the lifecycle design explicitly replaces it.
8. `--force` split, CHEAP: `session stop/resume/delete --force` becomes `--kill`; delete-family
   keeps `--force` (dependency override).
9. `resource schema --write` (boolean, fixed destination) becomes `resource schema --install`,
   CHEAP; the fixed destination is justified (schema modeline comments reference it by path), so the
   flag is renamed for what it does.
10. `resource enable/disable` (installer-plugins) and `agw database backup/restore`
    (safer-migrations) arrive on their own schedules; the vocabulary reserves the spellings.

### Beyond the fixed points

1. **The logs ruling**: `logs` = the thing's own workload output. `session logs` already satisfies
   it (scrollback today; the observability wave re-backs the same spelling later). `vm logs`
   violates it, it prints agentworks' own SSH operation transcripts from the host-side log dir.
   Candidate rename to `vm ssh-logs` (keeps `--all`), CHEAP. Reserves `vm logs` for genuine VM
   output if it ever exists.
2. Candidate: `env show --vm/--workspace/--agent/--session` becomes top-level `agw env ANCHOR`
   (`KIND/NAME`), MODERATE. The design must define eligible live kinds, reject declaration nodes,
   preserve config-free completion behavior where possible, and rule whether JSON rejects
   `--resolve` or remains value-redacted. It may not serialize resolved secret values by accident.
3. `secret verify`: add `--all` (prove the whole secret config) and `--output json` (the per-item
   outcome table is already the right data shape), CHEAP + CHEAP.
4. Candidate: combine `config sync-vscode-workspaces` and `config sync-ssh-config` as `config sync`,
   MODERATE. This needs a service boundary, independent-subtask ordering, partial-failure semantics,
   and a ruling on whether one invocation always writes both artifacts. It fixes the
   `config ... config` stutter but combines two current failure scopes.
5. **session create reshape**, MODERATE. `--workspace`/`--agent` become the single name slot for
   both existing and ephemeral, with `--new-workspace`/`--new-agent` as booleans switching lookup or
   create (name defaults to the session name when omitted). Drops `--workspace-name`/
   `--agent-name`; 11 flags become 9, one rule instead of two spellings per dependency. Inline
   creation itself is kept: atomic rollback is real value not composable from two commands.
6. `vm rekey --ignore-env`, help hard-codes the pre-sources backend model; re-spell against the
   synthesized-source model (working name `--prompt`), CHEAP, sequenced with the secrets wave.
7. New `console remove-shell NAME SESSION [POSITION]`, required if whole-console repair/reinit
   restores missing panes: once reconciliation re-adds killed panes, killing a pane is no longer a
   way to remove a shell; declared shells need an editable downward path, MODERATE.
8. Add machine-output decisions for `vm verify` alongside `secret verify`; do not assume all verify
   payloads have the same cardinality or fact shape, CHEAP after the JSON contract is settled.
9. Exit-code + confirmation audit across all commands against rules 5-7, MODERATE. It includes
   editor passthrough, Ctrl-C, completion uninstall, rehome, membership edits, and console recreate.

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
- **`explain` JSON is open**: `resource schema` covers machine-readable manifests but not all
  capability implementations that explain can document. Omit JSON absent a named consumer, not
  because schema is an exact twin.
- **Console spec grammar `name+N`** stays: the surface's one sanctioned per-operand mini-grammar.
- **Kind-locked describe commands**: open. Discoverability argues for retaining them, while a single
  generic home argues for retirement. If retained, equality must be defined compatibly with the
  versioned JSON command-ID contract.
- **`console attach` first-attach materialization kept**: building from nothing is creation
  semantics under the declared-state model. Destructive recreation must move out of attach if kept;
  its destination and spelling remain open under the reinit/repair distinction.
- **`repair --check` declined**: the read/write split is `verify`/`repair`, not a flag. A full-state
  `workspace verify` may arrive when someone actually needs drift-report-without-touch.
- **`vm add-git-credential` rename DEFERRED**: the harness-scope wave reshapes credential
  application ownership; renaming twice is worse than once.
- **`vm shell --platform`** stays (honest escape hatch); **`config init`/`edit` fixed paths** stay
  (the settings file is a singleton).

## 5. Architecture and downstream constraints

1. The frozen registry graph is declaration-only. Database rows must not be registered into it and
   the command-scoped orchestration graph is an incomplete execution view. Generic describe and
   graph need an approved read boundary. A request-scoped snapshot and a facade composing existing
   domain query services are the architecture candidates. See `node-model-study.md`.
2. Existing live describe commands expose materially different facts and some perform live probes. A
   shared target resolver does not imply a single undifferentiated card schema.
3. The operational JSON v1 contract has closed command IDs and exact payload shapes. A new generic
   card does not make existing group envelopes byte-identical. Preservation, a version bump with a
   compatibility period, or explicit removal must be chosen before HLA.
4. Graph must distinguish direct relationships from derived reachability and implicit/effective
   configuration. Edge kind, direction, provenance, ordering, cycles, and deterministic rendering
   are part of its contract.
5. Environment is an effective projection for eligible live anchors, not a universal node. JSON
   output must not serialize `--resolve` secret values. The interaction and redaction rules are
   security requirements.
6. Dynamic completions are hand-mapped for every supported shell. Cross-plane identities require a
   new source with defined config-failure, database-failure, side-effect, and cost behavior.
7. Every final move needs a command-reference, guide, upgrade-guide, completion, test, and sample
   configuration disposition. "No change needed" is an acceptable explicit disposition.

## 6. Open questions for the operator

Each question is a discrete verb-contract ruling. The recommendations are study conclusions, not
pre-approval.

1. **Generic describe home and group commands.** Add top-level `agw describe KIND/NAME` over the
   shared node universe? If yes, retire group describe commands or retain them as kind-locked
   adapters? Recommend: add the generic home and retain group discovery, but share typed fact
   records rather than promise byte-identical versioned envelopes.
2. **JSON migration.** Preserve every existing v1 describe command ID and payload, introduce a v2
   compatibility transition, or remove the old shapes under the 0.14 breaking posture? Recommend:
   preserve v1 while adding new command IDs unless #462 explicitly chooses a coordinated v2.
3. **Graph truth.** Show only direct stored/declared edges by default, or include derived secret
   reachability, implicit grants, and effective template selection? Recommend: direct edges by
   default, with derived relationships explicitly selected and visibly labeled.
4. **Graph traversal and output.** Use `--dependencies/--dependents` or `--up/--down`; define depth,
   multiple roots, cycles, and ordering. Ship deterministic human tree plus `--output json`; add DOT
   only if Graphviz is a named supported consumer. Recommend: self-describing direction names and no
   DOT on day one absent a consumer.
5. **Resource kind discovery.** Replace `resource kinds` with bare `resource explain`, retain kinds,
   or make bare explain show help? Recommend: retain `resource kinds`; external precedent and the
   existing config-free fast path favor separating inventory from type documentation.
6. **Environment home and safety.** Replace `env show` anchor flags with `agw env KIND/NAME`? Which
   live kinds are eligible, and does JSON reject `--resolve` or remain redacted? Recommend: adopt
   the positional anchor for the current four eligible instance kinds and reject JSON plus
   `--resolve` together.
7. **Console lifecycle.** Keep scoped `restore-session`; rename it to repair; or design a full
   reconciliation surface? Does destructive recreation become `reinit`? Recommend: first define the
   named workflows. Do not grow whole-console reconciliation solely for symmetry, and remove
   destructive recreation from attach.
8. **Logs.** Reserve `logs` for a noun's workload output and rename host-side VM SSH transcripts?
   Recommend: yes; working replacement `vm ssh-logs` needs observability-owner review.
9. **Verify.** Rename `vm verify-connection` to `vm verify`; add JSON to it and `secret verify`; add
   `secret verify --all`? Recommend: rename and add batch secret verification, then define separate
   payloads under the JSON ruling.
10. **Session create.** Collapse separate existing/new dependency names into one name slot plus
    boolean `--new-*` flags? Recommend: reshape during the breaking window; preserve the atomic
    rollback behavior.
11. **Confirmation matrix.** Which current operations are destructive enough to confirm, including
    membership removal, completion uninstall, rehome without removal, and console recreation?
    Recommend: confirm irreversible state loss, use `--yes` only as bypass, and do not prompt for
    reversible metadata edits.
12. **Node metadata.** Define live-instance origin, live-probe inclusion, template selection,
    relationship metadata, implicit console defaults, and public use of the word kind. Recommend:
    review each field as a separate decision set immediately before HLA.
13. **Config synchronization.** Combine the two sync commands? If yes, does one failure stop the
    other, how are partial writes reported, and can operators select one artifact? Recommend:
    combine only if both workflows move behind independent service operations and the aggregate
    reports both outcomes deterministically.
14. **VM rekey flag.** Replace `--ignore-env` in coordination with secret sources, or defer the
    spelling until that wave lands? Recommend: defer rather than mint the study's unproven
    `--prompt` spelling.
15. **Explain machine output.** Keep explain human-only, add JSON for capability documentation, or
    rely on schema only for the subset it covers? Recommend: human-only on day one absent a named
    machine consumer, while documenting that schema is not a complete twin.

## 7. HLA decision carried forward

**Inspection architecture boundary.** Build one request-scoped immutable snapshot, or compose
existing domain query services behind a constrained facade? This is not a verb-contract ruling. The
HLA must account for existing activation and PID-repair side effects, leave the finalized registry
unchanged, avoid the orchestration graph as inventory, and provide one approved consistency and
side-effect contract to renderers before planning begins.
