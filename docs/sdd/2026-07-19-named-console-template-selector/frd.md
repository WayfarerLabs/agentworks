# FRD: named-console-template instance selector

Start date: 2026-07-19. Tracks the console half of issue #165.

## Summary

Let an operator select which `named-console-template` a console uses, per console, at
`console create` time: `agw console create <name> [sessions...] --template <template>`. Today every
console implicitly uses the single reserved `named-console-template:default` (its `tmux_layout` is
the only setting a named console reads). This effort makes the kind selectable the same way
`vm create --admin-template` selects an `admin-template`. That admin half of issue 165 shipped as PR
200 and is the reference implementation this mirrors.

## Background and motivation

`named-console-template` is one of the two kinds issue #165 set out to make selectable. The admin
half shipped separately (PR #200). The issue assumed both kinds were already framework-plurified
(named-multi-instance with a `name` field on the dataclass). That is true for `admin-template` but
**not** for `named-console-template`: its `NamedConsoleConfig` dataclass has no `name` field and its
kind's `instances()` treats it as an effective singleton. So this effort carries prework the issue
did not budget for (plurifying `NamedConsoleConfig`), which is the main reason it is worth a short
SDD rather than a direct change.

The operator value: a console's tmux layout is currently fixed to the one default. Operators who
want different layouts for different consoles (for example a wide split for a monitoring console and
a stacked layout for an interactive one) cannot express that today. A per-console template selector,
with the layout declared as a normal manifest resource, closes that gap and removes the last
`_NO_SELECTOR_KINDS` entry (dead-config rejection in the manifest envelope), completing #165.

## Personas

- **Operator**: declares `named-console-template` resources (via YAML manifest) and picks one when
  creating a console. Wants the same selection ergonomics as the other template-bearing create
  commands (`vm create --template`, `workspace create --template`, `agent create --template`).

## Functional requirements

- **R1 Selector flag.** `agw console create` accepts `--template <name>` naming a declared
  `named-console-template`. Omitting it uses the reserved `default`, exactly as today.
- **R2 Named declarations.** A `named-console-template` may be declared with a non-`default`
  `metadata.name` via a YAML manifest. Before this effort the manifest envelope rejects such a
  declaration as dead config (`_NO_SELECTOR_KINDS`); after it, the declaration is accepted and
  selectable.
- **R3 Unknown name errors early.** Selecting a name that is not a declared `named-console-template`
  fails at command time with the framework's uniform unknown-template error naming the selector,
  before any DB write or on-VM work. Matches `vm create --admin-template` behavior.
- **R4 Persistence.** A console records its selected template so later reads (layout application on
  attach / restart / add-shell) resolve the same template the console was created with, independent
  of current config defaults. `NULL` persistence means the reserved `default`.
- **R5 Layout honored at use time.** Every place that applies a console's tmux layout resolves the
  console's own template, not the global default. A console created on a non-default template lays
  out its panes per that template's `tmux_layout`.
- **R6 Completion.** `--template` offers shell completion of declared `named-console-template`
  names, via the same dynamic-completer mechanism as the other template flags.
- **R7 TOML stays singleton.** The legacy `[named_console]` TOML block remains a singleton: it
  declares only the `default`. There is no `[named_console.<name>]` parsing. Named instances are
  manifest-only, consistent with the project's TOML-resource deprecation and with how the admin half
  kept `[admin.config]` singleton.

## Out of scope

- Any change to `admin-template` (shipped in PR #200) or to `vm create`.
- `[named_console.<name>]` TOML parsing (R7 keeps TOML singleton).
- New `named-console-template` settings beyond the existing `tmux_layout`. Plurification adds a
  `name`; it does not add layout features.
- Reflowing an existing console's layout when its template changes, or a `console set-template`
  command. The selector is a create-time choice, mirroring the other create commands.

## Acceptance criteria

- `agw console create c1 --template wide` on a declared `wide` template creates the console,
  persists the selection, and lays its panes out per `wide`'s `tmux_layout`; `agw console create c2`
  (no flag) uses `default`. (R1, R4, R5)
- A YAML manifest declaring `kind: named-console-template` with `metadata.name: wide` loads and is
  selectable; the same manifest was rejected before this effort. (R2)
- `agw console create c3 --template nope` (undeclared) errors naming `nope`, with no console row
  created and no SSH work done. (R3)
- `console describe` / `resource describe named-console-template/wide` "Used by" lists the consoles
  on that template, and `default` lists the rest, driven by the persisted column. (R4)
- Tab-completing `--template` lists declared `named-console-template` names. (R6)
- `[named_console]` in TOML still yields exactly one `named-console-template:default`; a
  `[named_console.wide]` block does not produce a second instance. (R7)
- Issue #165 is fully closed: `_NO_SELECTOR_KINDS` is empty (both kinds selectable), and the
  manifest envelope no longer special-cases either kind.

## Relationship to the admin half (PR #200)

This is a deliberate mirror. The admin half is the LLD-by-example: a nullable per-entity column
(`vms.admin_template`), a name-aware registry accessor, manifest name-threading, an
`unknown_template_error` at command time, an `instances()` filter by the column, and a dynamic
completer. The one structural difference is the prework: `AdminConfig` was already plurified;
`NamedConsoleConfig` is not, so this effort adds the `name` field and updates the kind's
`synthesize` / `instances` before wiring the selector. See the HLA for the mechanics.
