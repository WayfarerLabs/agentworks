# Focused CLI Grammar Study

- Status: Study input for FRD review
- Date: 2026-08-15
- Code basis: `origin/main` at `4ff5f1427c8970387b44e4cce0731b6fd14caa02`
- Scope authority: the next-steps saga ruling plus the operator's 2026-08-15 scope and ownership
  decisions

## Outcome

This effort is no longer a whole-CLI grammar redesign. It makes four related corrections:

1. Rename `agw resource describe-kind TARGET` to `agw resource explain TARGET` without expanding the
   command's behavior.
2. Add a top-level `agw graph` namespace whose first subcommand owns relational inspection.
3. Remove `agw resource describe KIND/NAME`. Its relationship data moves to `graph`; its remaining
   facts already have other owners.
4. Make `--write` consistently path-valued by replacing the fixed-destination
   `agw resource schema --write` mode with `--install`.

The implementation also fixes directly touched help, completion, machine-output, and documentation
contracts. It does not rename unrelated commands or establish a universal grammar for the other 69
current endpoints.

## Current surface

The rebased CLI has 73 leaf command endpoints. The only current commands in implementation scope
are:

| Current command                     | Current responsibility                                                   | Disposition                                     |
| ----------------------------------- | ------------------------------------------------------------------------ | ----------------------------------------------- |
| `resource describe-kind TARGET`     | Config-independent field and capability documentation                    | Rename to `resource explain`; preserve behavior |
| `resource describe KIND/NAME`       | Resource header, inbound declaration references, and live-instance usage | Remove; relationships move to `graph`           |
| `resource sample KIND --write PATH` | Write or append an inert sample at an operator-selected path             | Keep                                            |
| `resource schema [KIND] --write`    | Install the complete schema set at a fixed path                          | Rename the mode to `--install`                  |

No top-level `graph` group exists today.

### Why `resource describe` should disappear

The command currently renders two categories of facts:

- Identity, description, origin, readiness, and enablement facts. `resource list`, `resource kinds`,
  `resource edit`, `doctor`, and the kind-specific list and describe commands already own the
  actionable views of these facts.
- Inbound declaration references and live database instances that use the resource. These are
  relationships, so `graph` is their natural owner.

Keeping a reduced `resource describe` would create a card with no distinct operator question.
Rebuilding it as a generic cross-kind card would restore the broad node-model project that was
explicitly removed from scope. The clean migration is deletion, with no alias and no replacement
card.

## Recommended launch grammar

The HLA should validate this concrete starting point:

```text
agw graph show KIND/NAME
    [--kind KIND[,KIND...]]
    [--direction dependencies|dependents|both]
    [--depth N]
    [--output human|json]
```

`show` is preferable to making the graph group itself executable. It names the neighborhood query
and leaves room for a later two-node `path` query without changing the launch grammar. It is also
preferable to noun-specific subcommands such as `graph resource`: graph ownership should follow the
relationship question, not reproduce the source-system partitions that the graph connects.

The first implementation must cover the relationships that `resource describe` exposes today:

- inbound declared-resource references, including their existing `source`, `usage`, and optional
  `declared_by` metadata;
- live-instance usage returned by the resource kind's existing `instances` hook.

The saga's focal node, kind filter, direction, depth, and output axes belong in the initial
contract. Launch requires exactly one focal `KIND/NAME`; omitting it is a usage error. A whole-graph
query is future work. The HLA must determine which flag combinations can be implemented truthfully
from current sources. It must not invent a unified living graph or register database rows into the
frozen resource registry to satisfy the syntax.

### Read and safety boundary

`graph` is an inspector. It must not create or migrate a database, repair stored runtime state,
activate a resource, resolve a secret value, or prompt for credentials. A declaration-only query
must not demand an unrelated live-state source. When a selected relationship requires the database,
the service must open it through an explicitly read-only boundary and give a source-specific error
if that source cannot be read. The HLA owns the exact source-demand and partial-result policy.

Human output should be a deterministic adjacency or tree view. JSON should use the repository's
versioned envelope and typed node and edge records. DOT, Mermaid, watch mode, and arbitrary field
selection have no named launch consumer and are not initial requirements.

## Explain rename boundary

`resource explain TARGET` keeps the current `KIND` and capability `KIND/NAME` target forms. It keeps
the defining operational property of `describe-kind`: it reads no config and builds no registry, so
it can explain recovery steps while configuration is broken or a plugin is disabled.

The launch does not add:

- a bare invocation that replaces `resource kinds`;
- dotted field-path selection;
- JSON output;
- aliases or a compatibility wrapper for `describe-kind`.

Implementation names may contain dots today. A future field selector therefore must use a distinct
option such as `--field PATH`, not append a dotted path to `KIND/NAME` and create an ambiguous
operand.

## Writer semantics

Only two CLI options are currently named `--write`:

- `resource sample --write PATH` takes an operator-selected relative YAML path.
- `resource schema --write` is a boolean that writes the complete set to the fixed
  `resources/.schema/` directory.

After this effort, `--write` always takes a path and `resource schema --install` names the fixed,
idempotent installation action. `schema [KIND]` remains the stdout form. `schema --install` takes no
kind and retains the existing whole-set and fixed-path behavior.

## Directly related hygiene

The touched surface needs one internally consistent cutover:

- Typer command registration, help, examples, errors, and generated shell completions;
- dynamic completion command IDs;
- machine-output command IDs and JSON fixtures for the removed and added inspectors;
- command reference, resources guide, active platform guides, upgrade guide, and embedded guide
  content;
- schema, sample, and validation hints that direct operators to the field reference;
- tests of behavior and structured contracts, without assertions that police author-written prose.

Historical ADR and completed SDD text should remain historical unless it is an active instruction
that operators still follow.

## Explicit non-goals

- A generic `agw describe` command or a shared card model for every declaration and live instance.
- Renaming grouped lifecycle, inspection, verification, environment, or synchronization commands.
- A universal `KIND/NAME` model for every database-backed object.
- A persistent or mutable living graph service.
- New graph relation discovery beyond the launch relationships and axes.
- New explanation features beyond the rename.
- Compatibility aliases during the pre-0.14 cutover.

## Review questions handed to HLA

1. What are the exact depth-zero, depth-one, unbounded, cycle, and repeated-node rules?
2. How do current inbound references and live-instance usage map to typed edge kinds and direction?
3. Which query combinations require the database, and what is the behavior when it is absent, stale,
   or unreadable?
4. What stable node and edge fields form the initial `graph.show` JSON v1 contract?
