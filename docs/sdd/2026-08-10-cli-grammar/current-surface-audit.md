# CLI Grammar Rework, Current-Surface Audit

- Status: Code-verified study input
- Date: 2026-08-10
- Basis: `docs/cli-grammar-seed` at `284b447b`; merge-base `4d010d42`; mainline comparison
  `origin/main` at `e2bf898e`
- Scope: The registered command tree, operand and flag grammar, output contracts, confirmations,
  exit behavior, completions, and documentation consumers

## Inventory method and boundary

The audit followed the sole command-registration path in `cli/agentworks/cli/commands/__init__.py`,
inspected every registered callback and its service seam, and compared the result with
command-reference, completion, guide, sample-configuration, and test consumers. It found 71 command
endpoints. The root also owns `--non-interactive`, `--debug`, and `--no-deprecations`; these global
controls are part of the grammar even though they are not command endpoints.

This is an inventory of behavior at the study basis, not a proposed target surface.

## Registered command tree

| Group        | Endpoints                                                                                                                                                                |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| root         | `guide`, `doctor`, `version`                                                                                                                                             |
| `vm`         | `create`, `list`, `backup`, `describe`, `verify-connection`, `start`, `stop`, `delete`, `rekey`, `reinit`, `exec`, `shell`, `port-forward`, `add-git-credential`, `logs` |
| `workspace`  | `create`, `list`, `describe`, `rehome`, `repair`, `delete`, `copy`                                                                                                       |
| `agent`      | `create`, `list`, `describe`, `reinit`, `grant-workspaces`, `revoke-workspaces`, `exec`, `shell`, `delete`                                                               |
| `session`    | `create`, `describe`, `list`, `stop`, `resume`, `attach`, `delete`, `logs`                                                                                               |
| `console`    | `create`, `list`, `describe`, `attach`, `delete`, `add-sessions`, `remove-sessions`, `reorder-sessions`, `add-shell`, `restore-session`                                  |
| `config`     | `init`, `edit`, `sample`, `sync-vscode-workspaces`, `sync-ssh-config`                                                                                                    |
| `env`        | `show`                                                                                                                                                                   |
| `secret`     | `list`, `describe`, `verify`                                                                                                                                             |
| `resource`   | `list`, `kinds`, `describe`, `describe-kind`, `edit`, `sample`, `schema`                                                                                                 |
| `completion` | `show`, `install`, `uninstall`                                                                                                                                           |

## Existing conventions that are already coherent

- Every current `list` command, plus `resource kinds`, implements `--names-only` and
  `--output human|json` as mutually exclusive choices.
- CSV list filters mean OR within one flag and AND across flags. Unknown explicit names are errors.
- `session list --names-only` skips remote status work. `resource kinds --names-only` skips config
  and registry loading entirely. These are intentional low-cost fast paths worth preserving.
- All seven grouped `describe` commands and `doctor` already support JSON.
- Console create/add use the one established per-operand mini-grammar, `name+N`.
- `session resume` is distinct from start because it preserves logical identity and runs harness
  resume hooks.
- `secret verify` refuses interaction unless the operator supplies `--allow-interaction`, evaluates
  all requested secrets, and returns 1 if any cannot be resolved.

## Material inconsistencies and safety facts

### `reinit` and `repair`

`vm reinit` and `agent reinit` perform full idempotent re-initialization. `workspace repair`
reconciles only infrastructure that can be converged without re-initializing the live repository.
The verbs are intentionally different. The workspace command and manager prose currently call repair
an analog of reinit; that prose is wrong and caused the seed study's withdrawn rename.

### `--force`, `--yes`, and confirmation

- VM, workspace, and agent deletion use `--force` as a dependent-resource override.
- Session stop, resume, and deletion use `--force` to kill a broken tmux process. One spelling has
  two unrelated meanings.
- VM force-delete also suppresses confirmation even without `--yes`; agent and workspace
  force-delete do not.
- All instance delete commands expose `--yes`, but `completion uninstall` also removes installed
  files and has no confirmation decision recorded.
- `console attach --recreate` kills and rebuilds tmux state without confirmation or `--yes`.
- `console remove-sessions --yes` does not confirm membership removal. The flag controls only a
  possible follow-on offer to delete the now-empty console.
- `workspace rehome` confirms even when `--remove-old` is false, so its current confirmation is
  broader than a destructive-only rule.

The contract therefore needs an operation-by-operation confirmation matrix, not only a global
slogan.

### Output and status

- `env show`, `secret verify`, and `vm verify-connection` are read-only inspectors or proofs with no
  JSON output today.
- `env show --resolve` prints actual secret-backed values. Adding JSON without an explicit redaction
  and non-interaction rule would create a machine-readable secret-exposure surface.
- CLI-domain failures normalize to status 1, Click usage failures use status 2, and Ctrl-C
  returns 130.
- Child-status passthrough includes the expected exec, shell, attach, and port-forward commands. It
  also includes `config edit` and `resource edit`, whose contract is to run an editor. Any
  passthrough rule must cover all run-a-child commands rather than listing only remote execution.

### Command-specific corrections

- `console reorder-sessions` already documents and implements a bump-to-front operation while
  preserving the relative order of other members. The seed claim that its help understates the
  behavior is false.
- `vm logs` reads host-side Agentworks SSH-operation transcripts; `session logs` reads workload tmux
  scrollback. A single `logs` verb currently names different sources.
- `vm verify-connection` proves only canonical-admin connectivity. `secret verify` proves secret
  resolution and has different cardinality and interaction rules.
- `resource schema --write` is a boolean that writes one fixed schema path.
  `resource sample --write PATH` accepts a destination. The seed's writer inconsistency is factual.
- `session create` exposes 11 flags, including separate existing-resource and new-resource name
  spellings. This is a reshape decision, not a missing feature.

## Downstream contracts

### JSON

The command reference defines a closed v1 command-ID set and exact payload shapes for existing
machine commands. Incompatibly changing an existing payload's fields, meanings, ordering, or
enumerations requires a new schema version and an explicit compatibility period. A genuinely new
command receives its own documented schema. Removing an old CLI spelling and retiring its command ID
is a separate lifecycle decision that the current machine-output contract does not settle. Unifying
`*.describe` renderers cannot be called byte-identical while retaining different command IDs unless
equality is defined at the fact-record or payload-body boundary. The SDD must decide one of these
before HLA:

1. Preserve each existing v1 command ID and payload shape while sharing internal fact records.
2. Introduce a versioned replacement with a documented compatibility period.
3. Explicitly remove machine behavior under the breaking-window policy, coordinated with #462.

The generic `describe`, `graph`, `env`, and verify additions also need stable command IDs, payload
schemas, ordering, enum extensibility rules, and secret-safety tests.

### Completions

Completions introspect the static Click tree but maintain hand-written dynamic mappings for Bash,
Zsh, and PowerShell. Every renamed command or option must update those mappings. A cross-plane
`KIND/NAME` operand requires a new completion source that can combine registry declarations with
live instance names and preserve the config-free kind-list fast path. Enumeration must remain
prompt-free, secret-resolution-free, remote-probe-free, and read-only; it should skip render and
live-readiness work and degrade safely when configuration or the database is unavailable.

### Documentation and samples

The command reference, CLI README, resource and Proxmox guides, plugin docs, upgrade guide, and
authored guide topics contain dense command references. The guide's exact JSON actions for `doctor`
and `resource.list` are separately governed and must not drift. Sample configuration contains
relevant authoring guidance even though most grammar changes do not change configuration keys. Each
final decision needs an explicit docs and sample-config disposition.

## Study conclusions

1. The grammar rework is not a rename sweep. Generic describe and graph introduce a new cross-plane
   read model and a new machine-output contract.
2. The current registry graph cannot simply absorb database rows. A request-scoped immutable
   snapshot and a constrained facade over existing domain query services are the credible
   architecture boundaries for the HLA to compare. The facade is not read-only unless its current
   activation and PID-repair side effects are removed or explicitly approved.
3. JSON compatibility, secret-safe environment output, confirmation semantics, and completion
   identity are design inputs, not implementation cleanup.
4. Console-wide reconciliation is deep lifecycle work. It must be priced and designed separately
   from renaming the existing one-window restore operation.
5. No implementation should begin until the operator has ruled on the discrete choices in the verb
   contract and those rulings have been reflected in the FRD and HLA.
