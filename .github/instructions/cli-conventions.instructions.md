---
description: CLI command shape and naming conventions
applyTo: '**/agentworks/cli/**/*.py,**/completions/**/*.py,**/agentworks/**/manager.py'
---
# CLI Conventions

The Agentworks CLI follows a small, opinionated set of shape rules. They are listed here so new
commands compose naturally with what already exists rather than inventing their own dialect.

## Verb pattern: `<resource> <verb>` or `<resource> <verb>-<object>`

Two shapes, depending on whether a second object is involved.

**Operations on the resource itself** use a single verb: `<resource> <verb>`. Examples:

- `agent create`
- `agent delete`
- `agent describe`
- `vm reinit`
- `workspace list`
- `console attach`

**Operations involving a second object** make the object explicit in the verb:
`<resource> <verb>-<object>`. Examples:

- `console add-sessions` (verb `add` operates on `sessions` inside the console)
- `console remove-sessions`
- `console reorder-sessions`
- `console add-shell`
- `console restore-session`
- `agent grant-workspaces`
- `agent revoke-workspaces`

Pluralize the object when the command takes a variadic list (`add-sessions`, `remove-sessions`,
`reorder-sessions`, `grant-workspaces`, `revoke-workspaces` all accept N items) and keep it singular
when the command operates on one (`add-shell`, `restore-session`). The singular/plural cue tells the
operator at a glance whether multi-arg use is expected.

Do not introduce a multi-word subcommand group (e.g. `agent workspace-grants`) just to host a small
family of related verbs. The flat `<resource> <verb>-<object>` form is more discoverable, has
shorter command depth, and matches the rest of the surface. If a future command pair needs the same
object (`agent suspend-workspace` / `agent resume-workspace`), the verb-object form scales
naturally.

## Positional vs option

- **Positional arguments** for required things: names, IDs, lists of things being operated on.
- **Options** (`--flag` / `--key value`) for modifiers, mode switches, and optional config.
- **Variadic positionals** for lists. `agent grant-workspaces my-agent ws1 ws2 ws3`, not a single
  comma-separated string. This gives operators shell-completion past the first item and avoids
  quoting hazards.

## Bulk flags

Bulk operations use `--all` as a single, consistent flag name, never the more verbose form. So
`agent grant-workspaces my-agent --all`, not `--all-workspaces` or `--every-workspace`. The
surrounding command provides the context for what "all" applies to.

## Filter options on list commands

List commands narrow their result set with filter options that follow two shapes depending on what
is being filtered.

**Name filters** (`--vm`, `--workspace`, `--agent`, etc.) accept either a single name or a
comma-separated list of names:

- `agw session list --vm vm1`
- `agw session list --vm vm1,vm2 --agent claude,gemini`

Values within a single flag are OR-ed; multiple flags AND together. This pairing of CSV-for-OR and
different-flags-for-AND keeps the operator's mental model unambiguous. Repeated-flag forms
(`--vm vm1 --vm vm2`) are intentionally not used here, because the same shape would have to mean OR
within one flag and AND across flags, which is the inconsistency this rule exists to avoid.

Note the carve-out from the variadic-positional rule above: variadic positionals are for the things
the command is operating on (the operands); filter options narrow what a list command considers.
Operands keep their variadic-positional form (`agent grant-workspace my-agent ws1 ws2`); filters
take CSV. Commas cannot appear in resource names (see `validate_name` in `agentworks.config`), so
CSV parsing is safe.

**Mode filters** are bare boolean flags rather than valued options. Use `--admin` on `session list`
to narrow to admin-mode sessions, not `--mode admin`. The bare-flag shape composes naturally with
the name filters and matches how `session create --admin` already shapes the admin/agent mode
selection elsewhere on the surface.

## `--names-only` on list commands

Every `<resource> list` command exposes a `--names-only` flag that emits one resource name per line
with no header and no formatting. Shell completion (`completions/{bash,zsh,powershell}.py`) shells
out to the CLI via this flag rather than parsing the human-readable table. The output order matches
the table's row order, and filters compose as usual -- `--names-only` is a presentation switch, not
a filter (`agw session list --vm my-vm --names-only` is the intersection of the VM filter and the
names-only render).

The convention exists because table layout is a UX concern that should be free to change without
silently breaking completion. New list commands should ship `--names-only` from day one; the
service-layer `list_*` function takes a `names_only: bool = False` kwarg and short-circuits the
table render in favor of one `output.info(row.name)` per row when set.

**Render-only work is skipped under `--names-only`.** Anything computed purely for display -- status
columns that probe live state, formatted timestamps, derived counts -- belongs after the
short-circuit, not before. The `session list` SSH status batch is the precedent: it computes the
STATUS column and is gated by the names-only check so completion doesn't pay SSH round-trips on
every TAB press. Filter logic, on the other hand, runs the same way in both modes so the result set
stays consistent. Completion fires often and must be fast and side-effect-free; the rule keeps it
that way.

## Service layer is the authority

CLI command bodies should be thin: argv to kwargs, then call the service-layer function on the
relevant manager. Validation, error shaping, and business logic live in the manager. If the CLI
finds itself re-implementing a check the manager already does (e.g. "refuse empty input"), let the
manager raise and propagate. The contract: service-layer functions raise typed `AgentworksError`
subclasses from `agentworks.errors`, organized by _kind_ of error (`NotFoundError`,
`AlreadyExistsError`, `ValidationError`, `StateError`, `AuthorizationError`, `ConnectivityError`,
`ExternalError`, `ConfigError`, `UserAbort`); the entity dimension (vm, workspace, agent, session,
console) is carried as the `entity_kind` / `entity_name` attributes on the exception, not as the
type. The CLI catches and renders them; no `typer.echo`, `print`, or `typer.Exit` from manager
modules. See the `agentworks-reviewer` rubric for the full check.

## When in doubt

Look at the closest existing command in the same resource group and match its shape. Consistency
across the surface is the strongest signal an operator gets that they're typing a real command.
