# CLI Grammar Cutover: Low-Level Design

<!-- cspell:ignore sdds -->

- Status: Draft for final artifact checkpoint review
- Date: 2026-08-16
- Implements: `frd.md` FR1-FR5 and FR15-FR26
- Refines: `hla.md` A2 and A7-A9
- Code basis: `origin/main` at `bcde4983`

## Purpose and boundary

This design pins the command-boundary work of the CLI grammar correction. The graph-query LLD owns
graph records, traversal, lazy live projection, rendering, and graph JSON facts. This document owns
the command tree, the shared resource-identity access seam, retirement of the generic resource card,
completion wiring, permanent collateral, and the atomic cutover commit.

By explicit operator direction, the existing draft artifact PR is the only implementation vehicle;
its public final-artifact handoff supplies the active-saga coordination point without an early
artifact merge. There is no alias, warning, fallback dispatcher, compatibility ID, new graph
operation, facet label, or deprecation runway. A new spelling replaces an unreleased spelling
silently. The 0.14 upgrade guide maps the shipped `resource describe` break only.

## Ownership and file plan

| Path                                                                                                                                                              | Change | Responsibility                                                                                                                                                        |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cli/agentworks/resources/access.py`                                                                                                                              | Extend | In the earlier additive phase, add the shared first-slash parser and fact-minimal validated registry resolver.                                                        |
| `cli/agentworks/resources/inspect.py`                                                                                                                             | Reduce | Repoint edit in the additive phase; at cutover, delete the generic card while retaining list, kind, readiness, `used_by_for`, and edit-location behavior.             |
| `cli/agentworks/cli/commands/resource.py`                                                                                                                         | Modify | Register `resource explain`, retain sample writer semantics, rename the schema installer option, and remove `resource describe`.                                      |
| `cli/agentworks/cli/commands/graph.py`                                                                                                                            | Add    | Top-level graph Typer group and thin `show` orchestration only.                                                                                                       |
| `cli/agentworks/cli/commands/__init__.py`                                                                                                                         | Modify | Import the graph command module with the command-group registration imports.                                                                                          |
| `cli/agentworks/bootstrap.py`                                                                                                                                     | Reuse  | Build the graph request registry through `load_request_registry(..., probe_host_readiness=False)`. No graph-specific builder or registry mutation.                    |
| `cli/agentworks/machine_output.py`                                                                                                                                | Modify | Remove `RESOURCE_DESCRIBE`, add `GRAPH_SHOW = "graph.show"`, and retain all unrelated IDs, especially `SECRET_DESCRIBE`.                                              |
| `cli/agentworks/completions/spec.py`                                                                                                                              | Modify | Change dynamic command keys, add graph focus mapping, and carry finite static suggestions that do not constrain a parameter's grammar.                                |
| `cli/agentworks/completions/{bash,zsh,powershell}.py`                                                                                                             | Modify | Render Click choices or a spec-provided static suggestion set. Reuse existing `resource_kinds` and `resource_refs` snippets. Do not add shell-specific graph grammar. |
| `cli/tests/`                                                                                                                                                      | Modify | Delete resource-card tests and migrate their fact assertions to their surviving owners. Add focused command, completion, resolver, and removal coverage.              |
| `cli/README.md`, `cli/command-reference.md`, `cli/agentworks/sample-config.toml`, surviving README and guide files, hints, and `docs/guides/upgrading-to-0.14.md` | Modify | Make the active teaching and upgrade map match the final command surface.                                                                                             |

The graph command module imports graph-query services lazily inside `show`, in the same pattern as
other command modules. Importing the command group must not open a database, load config, build a
registry, or probe a host.

## Shared resource identity and resolution

`resources/access.py` gains the reusable, presentation-free types and functions below. They are the
only common `KIND/NAME` parser and registry resolver introduced by this effort.

```text
ResourceIdentity
  kind: str
  name: str

ResolvedResource
  identity: ResourceIdentity
  resource: object
  origin: Origin | None

parse_resource_identity(value: str) -> ResourceIdentity
resolve_resource(registry: Registry, identity: ResourceIdentity) -> ResolvedResource
```

Both records are frozen, slotted dataclasses. `ResolvedResource` deliberately exposes only identity,
the exact registry row, and its origin. It does not grow description, readiness, enablement,
references, instance usage, display text, or JSON fields. Graph needs the row for its kind hook and
edit needs the origin; neither needs a replacement resource card.

`parse_resource_identity` partitions on the first slash. It rejects a missing slash, an empty kind,
and an empty name with `ValidationError`, before any config, registry, or database work. The error
identifies the required `KIND/NAME` shape and carries `entity_kind="resource"`. The complete suffix
is the name, unchanged. Thus `session/legacy--name`, names containing dots, and names containing
colons remain valid identity values. The parser does not validate a kind or consult `KIND_REGISTRY`.

`resolve_resource` validates the kind before looking up the row. For an unknown kind, it raises the
present typed `NotFoundError` shape:

```text
unknown kind '<kind>'
entity_kind: resource-kind
entity_name: <kind>
hint: known kinds: <sorted kind names>
```

For a known kind with an absent name, it preserves the current resource-card error shape and its two
branches: a populated kind suggests `agw resource list --kind KIND`; an empty kind says no resources
of that kind are published. The exception carries `entity_kind=kind` and `entity_name=name`. The
resolver never enables, probes, resolves, or otherwise acts on a row.

The graph command parses its focus before loading config and passes that identity to the graph-query
service. After finalization, that service performs the one authoritative lookup through this
resolver and retains the exact row for any eligible live-instance hook. `resource edit` parses its
argument through the same parser before its existing editor/config work, then passes the identity
fields to its retained `edit_location` service. There is no shared resolver with config-free
`resource explain`: its `KIND/NAME` form is a capability-schema target and continues to use
`reference_for`.

### Edit behavior after the extraction

`resources.inspect.edit_location(registry, kind, name)` remains the owner of the origin-specific
edit decision, but calls `resolve_resource` rather than `describe_resource`. It returns an
operator-declared origin's file and line as today. Built-in, auto-declared, and capability rows
retain their current typed validation errors and authoring guidance. The generic card's disabled
sentence, readiness state, description, references, and live usage do not enter this path.

`resource edit` retains its current invalid-manifest recovery exactly:

1. Load config, build the ordinary finalized registry, and ask `edit_location`.
2. Catch only `ConfigError` from that registry path. Do not convert parser, unknown-kind, or
   unknown-name errors into a fallback scan.
3. Call `locate_document(resources_dir, kind, name)` without validation. If it finds an operator
   document, warn that validation is failing and open its recorded file and line.
4. If the tolerant scan did not find a document, re-raise the original `ConfigError`. When
   unreadable candidate files exist, append the current direct-edit hint to that exception before
   re-raising it.

This preserves the recovery path for the situation in which the operator most needs an editor,
without treating an invalid manifest as a known finalized row.

## Final command tree and parsing

The final relevant tree is:

```text
agw graph show KIND/NAME
    [--direction dependencies|dependents|both]
    [--depth POSITIVE_INTEGER|all]
    [--output human|json]

agw resource explain TARGET
agw resource schema [KIND] [--install]
```

`cli/agentworks/cli/commands/graph.py` creates `graph_app = typer.Typer` with `name="graph"`,
`no_args_is_help=True`, and a relationship-query help summary, then registers it on the root app.
The command module exports only the Typer group, the argument grammar parser, and `show`; graph
collection and renderers stay outside the CLI package.

`show` takes one required positional `focus`. Typer therefore makes no operand a missing-argument
usage error and more than one an unexpected-extra-argument usage error. It defines these options:

| Option        | Type and accepted values                                      | Default | Failure                                                                                              |
| ------------- | ------------------------------------------------------------- | ------- | ---------------------------------------------------------------------------------------------------- |
| `--direction` | Closed choice: `dependencies`, `dependents`, `both`           | `both`  | Typer choice usage error, before config or registry work.                                            |
| `--depth`     | String parsed as `all` or a base-10 integer greater than zero | `1`     | `BadParameter` on `--depth`: it must be a positive integer or `all`, before config or registry work. |
| `--output`    | Existing `OutputFormat`: `human`, `json`                      | `human` | Existing closed output-choice usage error.                                                           |

The depth parser returns `None` for `all` and the parsed positive `int` otherwise. It does not
accept zero, negative values, non-numeric strings, or any additional symbolic spelling. JSON
projects this value as `depth_limit`, with `null` for `all`, as specified by the HLA and graph-query
LLD.

The command's execution order is fixed:

1. Parse focus with `parse_resource_identity` and parse option grammar.
2. `load_config(warn_issues=output_format is OutputFormat.HUMAN)`.
3. `load_request_registry(config, warn=output_format is OutputFormat.HUMAN, probe_host_readiness=False)`.
   This runs normal finalization and reference validation but does not run host-readiness probes.
4. Construct the unopened request-scoped live source from the process's canonical
   `agentworks.db.DB_PATH` and call the graph query service once with its explicit registry, parsed
   focus, direction, depth limit, and source dependencies. The service calls `resolve_resource` once
   before any database operation. This effort adds no database-path config field or secondary
   path-resolution rule.
5. Select the graph-query LLD's human renderer or explicit JSON projector. For JSON, call
   `write_json_envelope(MachineOutputCommand.GRAPH_SHOW, data, ...)`.

The graph command never calls `get_db`. It does not resolve secrets, prompt, activate resources, run
provider or remote APIs, add rows to `Registry`, or mutate `Registry.graph`. The live source remains
lazy until the graph-query service's demand predicate says it is needed.

`resource describe-kind` is renamed in place to `@resource_app.command("explain")` and
`resource_explain`. Its `target` argument, `reference_for(target)`, and `render_reference` call are
otherwise unchanged. It loads no config, creates no registry, and opens no database. In particular,
it must continue to work with absent or invalid config and an installed but disabled plugin.

`resource schema` replaces its boolean `write` option with a boolean `install` option named only
`--install`. When false, the current stdout forms for no kind and one kind are unchanged. When true,
a supplied kind raises the same validation rule with the option name updated to `--install`; a
missing kind calls the existing `write_schema_set` with the existing canonical `resources/.schema/`
destination, overwrite behavior, and report. No path is accepted for `--install`.
`resource sample --write PATH` is unchanged. After the cutover, every remaining option literally
named `--write` takes a path.

Resource-group help changes from cross-kind inspection to the resulting inventory, explanation,
authoring, and editing responsibilities. It must not claim a generic per-resource inspector exists.

## Generic resource-card deletion

The following are deleted in the same cutover commit:

- `resource_describe` registration and its CLI parsing, database open, and `--output` path in
  `cli/agentworks/cli/commands/resource.py`.
- `ResourceDescription`, `describe_resource`, `resource_description_data`,
  `render_resource_description`, and the describe-only disabled-line helper in
  `resources/inspect.py`.
- `MachineOutputCommand.RESOURCE_DESCRIBE`, its command-reference schema, machine-output fixtures,
  and CLI cases.
- `cli/tests/test_resource_describe.py`, after its useful fact checks have moved to the owners
  below.
- The `resource.describe` completion mapping and every dynamic-completion expectation specific to
  that command.

`used_by_for`, its guarded kind-hook behavior, `not_ready_reason_for`, list projection, kind
listing, and `edit_location` remain. `used_by_for` remains because `secret describe` uses it. There
is no renamed DTO, hidden service, or generic card-shaped JSON record.

The migration map for assertions is intentional:

| Former card assertion                                                | New owner                                                                                                               |
| -------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Unknown kind/name and parser behavior                                | `resources/access.py` parser/resolver tests, plus focused graph/edit CLI tests.                                         |
| Inbound declared reference, relationship verb, usage, and provenance | Graph-query service and graph CLI tests.                                                                                |
| Live instance usage                                                  | Graph-query service and graph CLI tests.                                                                                |
| Registry row identity, description, and origin                       | Direct `Registry.lookup` and resource-list tests where a user-visible inventory projection is intended.                 |
| Readiness and enablement                                             | `registry.graph` / resource-list / doctor tests, according to the fact under test.                                      |
| Whether a row is editable and its source location                    | `test_resource_edit.py` and `edit_location` tests.                                                                      |
| Plugin and domain parity tests that used the card as a shortcut      | The concrete registry row, explicit graph fact, list projection, or kind-specific service that the test actually needs. |

This includes the current direct uses in plugin, VM, session-template, apt, resource-instance, and
machine-output tests. The implementation must migrate each assertion before deleting the service. It
must not retain `describe_resource` as a test helper or replace all deleted assertions with weaker
absence checks.

`secret describe` is excluded from the deletion. It retains its own service, reduced `dependents_of`
view, live grouping, human sections, and exact `secret.describe` JSON record. Adding full incoming
graph edges is additive and does not migrate secret behavior.

## Completions

`DYNAMIC_COMPLETIONS` in `completions/spec.py` is the sole dynamic mapping change:

```text
("resource.explain", "target") -> resource_kinds
("resource.edit", "ref")       -> resource_refs
("graph.show", "focus")         -> resource_refs
```

The retired `("resource.describe", "ref")` and `("resource.describe-kind", "target")` entries
disappear. `resource_kinds` continues to invoke the config-free `agw resource kinds --names-only`
path in all three shells. `resource_refs` continues to invoke the config-backed
`agw --completion-probe resource list --names-only` path in all three shells. That names-only path
must finalize the registry but bypass `get_db` and call `list_resources(..., db=None)`, because its
candidate set is declared resource identities and does not need live `used_by_count` facts. Thus
explain completion remains available with absent or broken config; graph and edit completion remain
available with a valid registry even when the database is absent, stale, newer, malformed, busy,
unreadable, or otherwise unusable; and all config or registry failures still yield no candidates.
The ordinary human and JSON `resource list` paths retain their current database-backed behavior.

Direction and output are Click choices emitted from the Typer tree. Depth offers static useful
candidates `1`, `2`, `3`, and `all`; it does not pretend to enumerate all positive integers or use
`click.Choice`, because the command must still accept every positive integer.

`completions/spec.py` extends `ParamSpec` with `suggestions: list[str] | None`, populated from a new
`STATIC_COMPLETION_SUGGESTIONS` mapping. Its only cutover entry is:

```text
("graph.show", "depth") -> ("1", "2", "3", "all")
```

`_build_param_spec` retains `choices` from Click and independently looks up this mapping. The three
generic renderers use `choices` when present, otherwise `suggestions`, for both option values and
positional arguments. The completion-version serializer includes `suggestions`. This provides one
common generated path for finite suggestions without weakening the arbitrary-positive-integer parser
or hand-writing a graph branch in any shell renderer. It does not add a kind filter or other graph
option.

After the mapping changes, regenerate the three completion outputs through the existing generation
path and test all of the following structurally:

- the introspected command tree has `graph show`, `resource explain`, and
  `resource schema --install`, and lacks both retired resource subcommands;
- every dynamic mapping still points to a real command parameter and every completer ID exists in
  all three shell renderer maps;
- each shell invokes config-free kinds completion for explain without the completion probe, and
  invokes resource-reference completion for graph with the completion probe and its existing stderr
  suppression;
- explain completes during missing and invalid config; graph focus preserves the config/registry
  failure behavior of config-backed resource refs but still completes during absent, stale, newer,
  malformed, busy, and unreadable database states;
- static graph choices and schema `--install` are emitted by all three shells.

## Documentation, hints, and historical records

The active-reference sweep runs from the cutover tree, not from an assumed earlier guide surface.
The guide deletion that landed in PR 556 is treated as already absent: do not edit, restore, or
regenerate its resource topics, schema adapters, or generic guide projection.

Update active command-owned teaching in `cli/command-reference.md`, `cli/README.md`,
`cli/agentworks/sample-config.toml`, surviving capability and domain README files, current guides,
skeleton/sample/error/reference hints, and resource-group help. Add `graph show` and its human/JSON
contract to the command reference, replace active `describe-kind` examples with `explain`, replace
schema installer references with `--install`, and remove generic resource-card examples and JSON
documentation.

`docs/guides/upgrading-to-0.14.md` keeps a concise map for the shipped `resource describe` removal:
relationship inspection becomes `graph show`, while inventory, doctor, edit, and kind-specific
commands own the remaining questions. It does not advertise `describe-kind` or schema `--write` as
user migrations because those spellings did not ship as stable contracts.

Before committing, perform a one-time reviewed search of tracked active source, tests, docs,
completion artifacts, examples, hints, and fixtures for:

```text
resource describe
describe-kind
resource.describe
resource schema --write
```

The only allowed matches are the 0.14 resource-describe upgrade explanation, clearly historical ADR
or completed-SDD records, and focused negative tests that prove a retired spelling now fails. Review
every other match rather than maintaining a broad textual allowlist. This search is cutover
evidence, not a committed test that polices repository-authored prose. Persistent tests cover
command registration, exit behavior, command IDs, hints emitted by owned code paths, and completion
structure. The sweep must also confirm that no current operator-facing phrase points to the removed
generic inspector.

## One collateral-complete cutover commit

The graph storage, query service, records, renderers, lazy database source, shared identity access,
edit repointing, and their tests land in the earlier additive commits named by the plan. The
remaining command-surface changes owned by this document land together in exactly one final cutover
commit, with no pushed partial registration or stale active teaching:

1. Add graph command registration and thin orchestration against the completed graph-query service.
2. Consume the already-extracted identity parser/resolver from graph and edit, preserving the
   invalid-manifest fallback.
3. Rename explain and the schema installer, then remove the generic resource card and its machine
   command.
4. Update completion mappings and generated bash, zsh, and PowerShell output.
5. Update permanent collateral, active hints, upgrade map, resource help, and the migrated test
   suite.

The commit is complete only when every observable surface describes the final grammar. It must never
publish both old and new command identities, leave a removed machine ID in the enum, or teach a
command that is not registered.

## Focused verification and gates

Run focused tests for resource access and edit fallback, graph CLI option and registry construction
behavior, explain with absent/invalid config, schema installation parity and old-flag rejection,
resource-card removal, secret describe parity, JSON command IDs, and bash/zsh/PowerShell completion
generation and broken-config behavior. Resource-reference completion tests prove its registry-only
names path never opens absent, stale, newer, malformed, busy, or unreadable database state. Graph
query service, renderer, source demand, secret-safety, and database transaction coverage are owned
by the graph-query LLD and run with this slice.

Then run from `cli/`:

```text
uv run ruff check .
uv run mypy agentworks tests
uv run pytest tests/ -m 'not integration'
```

Run repository gates:

```text
./scripts/lint-files.sh
git diff --check
./scripts/check-locked-sdds.sh
./scripts/rulesync-upgen.sh --check
```

Documentation is reviewed directly for accuracy. Tests assert command existence, exit behavior,
records, machine schemas, generated completion structure, and ownership facts. They do not pin prose
authored by this change.
