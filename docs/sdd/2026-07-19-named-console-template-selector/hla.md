# HLA: named-console-template instance selector

Implements the [FRD](./frd.md). This is a deliberate mirror of the shipped admin-template selector
(PR #200); the sections below note where the console case matches admin and where it diverges.

## Shape of the change

Nothing new architecturally. The resource framework already treats template kinds as
named-multi-instance and already has the selector pattern (a per-entity DB column that names the
selected template, a name-aware registry accessor, `instances()` filtering by the column, and a
command-time unknown-name error). The work is to (a) finish plurifying `named-console-template` so
it has the same shape as the other template kinds, and (b) wire the console selector onto that.

## Components

### 1. `NamedConsoleConfig` plurification (the prework)

`NamedConsoleConfig` (`sessions/template.py`) currently has `tmux_layout`, `declared_at`, `origin`,
`references` and no `name`. Add a `name: str = "default"` field, mirroring `AdminConfig`.

- `_NamedConsoleTemplateKind.synthesize` builds `NamedConsoleConfig(name="default", origin=...)`
  (today it builds it with no name).
- `_NamedConsoleTemplateKind.instances` today yields every console unconditionally (an effective
  singleton). It becomes a filter by the new console column: yield a console only when
  `(console.template or "default") == resource.name`, exactly like `_AdminTemplateKind.instances`
  now filters VMs by `vms.admin_template`.
- **Open item (verify in Phase 1):** whether the resource protocol requires a `referenced_resources`
  method on `NamedConsoleConfig`. `AdminConfig` has one that sources requirements from `self.name`;
  `NamedConsoleConfig` has no env or secret references, so if the method is required it returns an
  empty list (its only "reference" would be a future `inherits`, which is not in scope). The kind
  works today without one, so the likely answer is "not required"; confirm and, if needed, add a
  trivial `referenced_resources` returning `[]`.

### 2. Name-aware accessor

`named_console_template(registry)` (`resources/access.py`) resolves the singleton `default`. Make it
`named_console_template(registry, name: str = "default")`, resolving
`registry.lookup("named-console-template", name)`, backward compatible via the default argument.
Mirrors the `admin_template(registry, name="default")` change.

### 3. DB column and migration

Add a nullable `template` column to the `consoles` table: `NULL` means the reserved `default`.
Mirrors `vms.admin_template` and the existing `workspaces.template` / `agents.template` columns.

- `ConsoleRow` gains `template: str | None`.
- `insert_console` gains a `template: str | None = None` parameter and writes it.
- `_to_console` reads the column.
- A new forward-only `ALTER TABLE consoles ADD COLUMN template TEXT` migration. It needs no
  backfill: `NULL` is semantically `default`, and every read normalizes with `or "default"`.

**Migration-number coordination (cross-PR).** Migrations are a shared sequential resource.
`LATEST_VERSION = max(MIGRATIONS)` on this branch is **28**; the admin half (PR #200, not yet
merged) takes **29** for `vms.admin_template`. This console migration must therefore be **30**,
finalized after #200 merges. If #200 has not merged when this implements, rebase onto it first so
the numbers do not collide; the plan calls this out as a gating step.

### 4. `console create` selector

`console create` (`cli/commands/console.py`) gains `--template <name>`, threaded into the console
manager's create path. The manager resolves the selected name against the registry; an unknown
non-`default` name raises
`unknown_template_error(kind="named-console-template", label="named console template", ...)` before
any DB write or SSH work, matching create's admin-template error. It persists the canonical `NULL`
for the reserved default (whether the flag was omitted or passed explicitly as `default`), the
normalization the admin half adopted on review.

The console manager is the service-layer authority; the CLI body stays thin (argv to kwargs). The
error is typed and raised from the manager, rendered by the CLI.

### 5. Layout consumers read the console's template

Four sites in `sessions/multi_console.py` read `named_console_template(registry).tmux_layout`
(around lines 445, 711, 764, 2030). Each runs in the context of a specific console, so each resolves
that console's own template:
`named_console_template(registry, console.template or "default").tmux_layout`. This is the console
analogue of threading `vm.admin_template or "default"` through the admin-env consumers. Phase 2
confirms each site has the `ConsoleRow` in scope (the expectation is yes, since they are
layout-application paths keyed on a console) and threads it; any site that turns out not to have the
row is flagged rather than guessed.

### 6. Manifest decode and envelope

- `_decode_named_console_template` (`manifests/decode.py`) threads `name=doc.name` into
  `_load_named_console`, so a manifest with a non-`default` `metadata.name` produces a named
  `NamedConsoleConfig`. `_load_named_console` (`config.py`) gains a `name: str = "default"`
  parameter it passes to the dataclass; the TOML path never passes it (stays singleton, satisfying
  R7).
- `manifests/envelope.py`: remove `"named-console-template"` from `_NO_SELECTOR_KINDS`. That empties
  the set (the admin half already removed `"admin-template"`), so the set and its now-dead
  special-casing branch are deleted, and the envelope no longer rejects any kind for lacking a
  selector. This is the step that finally closes #165.

Note: the issue referenced a `SYNTHESIZED_SINGLETON_KINDS` constant for the TOML-singleton
guarantee. That constant no longer exists in the tree. The real R7 invariant is simply that
`_load_named_console` only ever emits `default` and there is no `[named_console.<name>]` parsing; no
constant needs touching.

### 7. Completions

Add a `console_templates` dynamic completer
(`agw resource list --kind named-console-template --names-only`) wired in `completions/spec.py` (the
identifier, the `("console.create", "template")` mapping, and a doc comment) and in the three shell
backends (`bash.py`, `zsh.py`, `powershell.py`), mirroring the `admin_templates` completer the admin
half added.

### 8. Docs

`cli/README.md` documents `--template` on `console create`. If the resource guide notes the kind, it
updates to reflect that named instances are now selectable. No permanent doc anchors to this SDD
path (SDD-not-permanent rule).

## Data flow

Create: `console create --template wide` -> manager resolves `named-console-template:wide` (error if
absent) -> `insert_console(..., template="wide")` -> console row carries `template="wide"`.

Use (attach / restart / add-shell): layout site loads the `ConsoleRow`, resolves
`named_console_template(registry, row.template or "default")`, applies that `tmux_layout`.

Projection: `_NamedConsoleTemplateKind.instances(resource=wide)` lists consoles whose
`(template or "default") == "wide"`; `resource describe` renders that as "Used by".

## Risks and mitigations

- **Migration collision with #200** (Section 3): mitigated by rebasing onto merged #200 and using
  migration 30. Gating step in the plan.
- **A layout consumer without the `ConsoleRow` in scope** (Section 5): would make threading awkward.
  Mitigation: Phase 2 audits all four sites first and reports before editing; the admin half's audit
  found every consumer had its entity in scope, so this is expected to be clean.
- **`referenced_resources` protocol requirement** (Section 1): low-risk unknown resolved by reading
  the protocol in Phase 1; the fix if needed is a one-line `return []`.

## What does not change

- No orchestration, gating, or secret-projection changes: unlike the admin template (whose `env`
  feeds the secret-reachability projection), `named-console-template` carries only `tmux_layout`, so
  there is no `_secrets_reachable_from_session` analogue to touch.
- No change to `console` subcommands other than `create` (the selection is a create-time choice).
- No TOML surface change (R7).
