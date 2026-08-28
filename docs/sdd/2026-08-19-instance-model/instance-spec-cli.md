# Instance Spec CLI Contract

- Status: Implemented in R4; merge-strategy correction in design review
- Date: 2026-08-24
- Requirements: R4 and R5 in `frd.md`

## Principle and surface

An instance spec is the final partial layer of the effective declaration selected at a
template-setting lifecycle boundary. It is not independently mutable desired state. The option
appears exactly on the existing commands where an instance template can be selected or changed:

```text
agw vm create NAME [--template TEMPLATE] [--spec JSON]
                   [--admin-template TEMPLATE] [--admin-spec JSON]
agw workspace create NAME [--template TEMPLATE] [--spec JSON]
agw agent create NAME [--template TEMPLATE] [--spec JSON]
agw session create NAME [--template TEMPLATE] [--spec JSON]
agw agent reinit NAME [--update-template TEMPLATE] [--spec JSON]
```

`agent reinit` is the sole existing-instance command that can repoint its owner to another template.
Its `--spec` may be supplied with or without `--update-template`: the command either evaluates both
new inputs together or evaluates the new instance spec against the stored template.

`vm create` has two declaration slots. Unprefixed `--template` and `--spec` select and refine the VM
declaration. `--admin-template` and `--admin-spec` select and refine the admin declaration.
`--admin-spec` may be supplied without `--admin-template`, in which case it follows the reserved
`default` admin template. The `vm` command name already supplies the primary-resource context, so
there are no `--vm-template` or `--vm-spec` aliases.

`session create` can create three owners in one request. Its `--spec JSON` applies to the session;
`--workspace-spec JSON` applies to the workspace created by `--new-workspace`, and
`--agent-spec JSON` applies to the agent created by `--new-agent`. A child spec without its matching
`--new-*` flag is a usage error, following the existing `--workspace-template` and
`--agent-template` ownership cues.

There are no `set-spec` or `clear-spec` verbs. `vm reinit`, `workspace repair`, and `session resume`
do not accept `--spec`: VM reinit cannot select a different VM template, workspace repair is not
full idempotent convergence, and session resume retains unresolved sharp edges. `workspace copy`
also does not accept `--spec` because it does not select and realize an ordinary workspace template.
A VM, workspace, or session spec therefore cannot change after creation until that kind gains a
lifecycle operation that can also change its template. This is deliberate.

## Input value

Each spec input is one inline JSON object. Its fields are the declarable spec fields for that
declaration slot: VM-template fields for `vm create --spec`, and admin-template fields for
`vm create --admin-spec`. The top level must be an object; arrays, scalars, and `null` are rejected.
The exact empty CLI value is accepted only by `agent reinit` as a convenience spelling for `{}`;
whitespace-only input remains invalid. There is no wrapper, and the object cannot declare `kind`,
`name`, `inherits`, description, source metadata, or framework provenance.

Parsing is strict JSON at the service boundary. Duplicate object member names, non-finite numbers
such as `NaN` or `Infinity`, trailing content, and JSON `null` at any depth are rejected with the
normal typed validation error. Omission represents no contribution from a field; `null` is not a
second spelling for inherit, clear, or unset. No parser-specific extension or last-member-wins
behavior reaches the typed model.

For example, a new VM can select both templates and append both final partial layers in one
operation:

```console
agw vm create build-01 --template dev \
  --spec '{"cpus":8,"memory":16,"apt_packages":["ripgrep"]}' \
  --admin-template operator \
  --admin-spec '{"shell":"zsh","env":{"TOKEN":{"secret":"build-token"}}}'
```

An agent can replace its layer while retaining its current template:

```console
agw agent reinit coder-01 --spec '{"shell":"/bin/bash","mise_activate":true}'
```

It can instead change both inputs to the effective declaration:

```console
agw agent reinit coder-01 --update-template typescript \
  --spec '{"shell":"/bin/zsh","mise_activate":true}'
```

This input is command data, not an instance manifest or a file reference. Agentworks does not
discover it, watch it, retain a source path, or make it part of the Resource Registry. The command
parses it into the kind's strict typed partial-spec model and persists canonical versioned data in
the database.

## Replacement and merge semantics

Supplying `--spec` declares the complete instance layer, not a field patch. On `agent reinit`, it
atomically replaces the prior stored layer. Omitting `--spec` retains that layer. Supplying the
empty JSON object or the exact empty CLI value clears it, so this command returns the agent to its
template-derived declaration:

```console
agw agent reinit coder-01 --spec '{}'
agw agent reinit coder-01 --spec ''
```

An empty object for either layer on a creation command is equivalent to omitting that layer.
Agentworks canonicalizes both cases as absence rather than retaining a meaningless empty component
or record.

The instance spec is the final input to the shared layer runner:

- a scalar declared by the instance spec replaces the resolved template scalar;
- an object or map merges by key and recursively applies each conflicting child's model-declared
  strategy;
- a list appends with stable deduplication by default; and
- an object or list whose model node declares `replace` discards the complete prior value, so an
  empty replaced object or list clears that value without introducing a patch language.

The policy is identical for template inheritance and the final instance layer. It is derived from
typed model annotations at every nesting depth for core and capability-owned config models. A
replaced object is a subtree boundary: no child strategy inside the discarded prior object
participates in that layer. An unmarked empty object or list remains additive and therefore changes
nothing.

For discriminated and structural unions, a containing `replace` wins before arm selection. Otherwise
different arms, or arms that cannot be selected, replace the complete object even when the field
says `merge`, so a field cannot retain children from the old arm. Equal arms use an explicit
containing `merge` when present, then the selected arm model's policy. Raw invalid values remain raw
until effective-spec validation; merging does not filter unknown keys, drop malformed items, or
coerce a bad shape into a valid declaration. When the same unknown key appears twice, the later raw
value wins rather than being recursively combined by runtime shape.

The admin spec follows the same field rules after its selected admin template. Although an admin
template does not inherit, the layer fold still distinguishes omitted fields from its concrete
defaults, so an omitted admin-spec field leaves the selected template value intact.

Session harness selection keeps one structural transition around the generic merge. Naming a
different harness integration resets the prior integration config. Naming the same registered
integration combines config through that integration's model and the same annotations core models
use. Repeating the same unknown selector replaces its complete prior raw config because there is no
model through which to merge; the Registry reports the selector miss. A capability does not supply
an imperative merge callback.

There is deliberately no `--set PATH=VALUE`, JSON-path patch language, or generic key-value surface.
Those shapes would create a second type system for nested values, map keys, list semantics, and
field removal alongside the domain models that already own them.

## Validation, persistence, and effects

The service validates each partial spec shape, folds it after the corresponding selected template,
and runs the effective-instance reference and capability validators before starting remote work. On
VM creation, both effective declarations are validated as one candidate lifecycle decision. A
failure in either leaves the database unchanged.

Creation uses the candidate effective declaration for the lifecycle operation. A nonempty desired
layer and its owner row are inserted together in one transaction at the owner's existing database
boundary; `{}` or omission inserts only the owner when every layer is empty. A VM's VM and admin
layers occupy one typed, versioned desired payload so persistence cannot retain only one. Owner
unwind or deletion removes the desired payload in the same owner-delete transaction. If a lifecycle
failure retains an owner for retry, it also retains the complete declaration that the retry must
use. `vm reinit` accepts no spec input but reapplies both stored VM declaration slots. Applied state
records only the slices that the lifecycle can prove; partial or failed work remains explicitly
unknown where proof is absent.

On `agent reinit`, validated template and spec changes are persisted together before the remote
lifecycle boundary, matching the existing `--update-template` retry behavior. A failed reinit may
therefore leave the newly selected declaration pending for the next retry, but no successful command
can change a stored instance spec without running the lifecycle operation that consumes it.

Each command reports every material instance-spec disposition with a human declaration-result line
once success or unwind determines the final retained desired state. A nonempty layer reports `set`
when no prior layer exists and `replaced` otherwise. Omission on `agent reinit` reports `retained`
when a layer exists; omission with no stored layer emits no new line, preserving the simple case. An
empty object reports `cleared` when it removes a prior layer and `explicitly absent` otherwise. Set,
replacement, and retention name the sorted top-level fields; clear names the prior fields. When a VM
has both layers, the lines identify the VM or admin declaration slot. Values are never echoed
because a spec may contain plaintext environment values.

A creation path that unwinds its owner and desired layer never emits a `set` line. A failed
`agent reinit` still reports the retained, replaced, or cleared declaration because its
pre-lifecycle persistence is the retry contract. The line reports final desired declaration, not
remote application or applied state.

## Inspection and machine output

The existing `vm describe`, `workspace describe`, `agent describe`, and `session describe` surfaces
show the stored instance spec and either the current fully resolved spec or an explicit unresolved
state. VM inspection distinguishes its VM and admin declaration slots. Per-value provenance and
comparison state appear only where resolution exists; applied slices remain separately visible.
Their JSON v1 forms add optional tagged fields without changing existing fields. Configured secret
references may appear; resolved secret values never do.

When provenance is available for a list value, it identifies that value by its position in the
displayed resolved list after merging, not by the item's spelling or representation.

Creation and reinit retain their ordinary human lifecycle output and add the declaration-result line
above when applicable. Automation reads the structural JSON v1 description after mutation.
