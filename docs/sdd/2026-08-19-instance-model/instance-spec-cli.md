# Instance Spec CLI Contract

- Status: Implemented in R4
- Date: 2026-08-24
- Requirements: R4 and R5 in `frd.md`

## Principle and surface

An instance spec is the final partial layer of the effective declaration selected at a
template-setting lifecycle boundary. It is not independently mutable desired state. The option
appears exactly on the existing commands where an instance template can be selected or changed:

```text
agw vm create NAME [--template TEMPLATE] [--spec JSON]
agw workspace create NAME [--template TEMPLATE] [--spec JSON]
agw agent create NAME [--template TEMPLATE] [--spec JSON]
agw session create NAME [--template TEMPLATE] [--spec JSON]
agw agent reinit NAME [--update-template TEMPLATE] [--spec JSON]
```

`agent reinit` is the sole existing-instance command that can repoint its owner to another template.
Its `--spec` may be supplied with or without `--update-template`: the command either evaluates both
new inputs together or evaluates the new instance spec against the stored template.

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

The input is one inline JSON object. Its fields are the declarable spec fields for that instance
kind. The top level must be an object; arrays, scalars, and `null` are rejected. The exact empty CLI
value is accepted only by `agent reinit` as a convenience spelling for `{}`; whitespace-only input
remains invalid. There is no wrapper, and the object cannot declare `kind`, `name`, `inherits`,
description, source metadata, or framework provenance.

Parsing is strict JSON at the service boundary. Duplicate object member names, non-finite numbers
such as `NaN` or `Infinity`, trailing content, and JSON `null` at any depth are rejected with the
normal typed validation error. Omission represents no contribution from a field; `null` is not a
second spelling for inherit, clear, or unset. No parser-specific extension or last-member-wins
behavior reaches the typed model.

For example, a new VM can select a template and append its final partial layer in one operation:

```console
agw vm create build-01 --template dev \
  --spec '{"cpus":8,"memory":16,"apt_packages":["ripgrep"]}'
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

An empty object on a creation command is equivalent to omitting `--spec`. Agentworks canonicalizes
both cases as absence rather than retaining a meaningless empty record.

The instance spec is the final input to the shared layer runner:

- a scalar declared by the instance spec replaces the resolved template scalar;
- an ordinary map declared by the instance spec merges by key, with its value winning;
- a list declared by the instance spec appends with the kind's existing stable deduplication; and
- an empty list or map within the object does not clear inherited content because the template model
  has no removal tombstone.

Session harness selection keeps its domain reducer rather than using ordinary map merge. Naming a
different harness integration resets the prior integration config. Naming the same integration
combines config through that integration's typed merge function, including integration-owned
behavior such as the shell integration's stable union of required commands.

There is deliberately no `--set PATH=VALUE`, JSON-path patch language, or generic key-value surface.
Those shapes would create a second type system for nested values, map keys, list semantics, and
field removal alongside the domain models that already own them.

## Validation, persistence, and effects

The service validates the partial spec shape, folds it after the selected template chain, and runs
the effective-instance reference and capability validators before starting remote work. A template
and instance spec supplied together are one candidate effective declaration; any validation failure
leaves the prior database record unchanged.

Creation uses the candidate effective declaration for the lifecycle operation. A nonempty desired
layer and its owner row are inserted together in one transaction at the owner's existing database
boundary; `{}` or omission inserts only the owner. Owner unwind or deletion removes the desired
layer in the same owner-delete transaction. If a lifecycle failure retains an owner for retry, it
also retains the declaration that the retry must use. Applied state records only the slices that the
lifecycle can prove; partial or failed work remains explicitly unknown where proof is absent.

On `agent reinit`, validated template and spec changes are persisted together before the remote
lifecycle boundary, matching the existing `--update-template` retry behavior. A failed reinit may
therefore leave the newly selected declaration pending for the next retry, but no successful command
can change a stored instance spec without running the lifecycle operation that consumes it.

Each command reports every material instance-spec disposition with a human declaration-result line
once success or unwind determines the final retained desired state. A nonempty layer reports `set`
when no prior layer exists and `replaced` otherwise. Omission on `agent reinit` reports `retained`
when a layer exists; omission with no stored layer emits no new line, preserving the simple case. An
empty object reports `cleared` when it removes a prior layer and `explicitly absent` otherwise. Set,
replacement, and retention name the sorted top-level fields; clear names the prior fields. Values
are never echoed because a spec may contain plaintext environment values.

A creation path that unwinds its owner and desired layer never emits a `set` line. A failed
`agent reinit` still reports the retained, replaced, or cleared declaration because its
pre-lifecycle persistence is the retry contract. The line reports final desired declaration, not
remote application or applied state.

## Inspection and machine output

The existing `vm describe`, `workspace describe`, `agent describe`, and `session describe` surfaces
show the stored instance spec and either the current fully resolved spec or an explicit unresolved
state. Per-value provenance and comparison state appear only where resolution exists; applied slices
remain separately visible. Their JSON v1 forms add optional tagged fields without changing existing
fields. Configured secret references may appear; resolved secret values never do.

Creation and reinit retain their ordinary human lifecycle output and add the declaration-result line
above when applicable. Automation reads the structural JSON v1 description after mutation.
