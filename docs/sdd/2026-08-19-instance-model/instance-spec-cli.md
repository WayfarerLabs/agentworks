# Instance Spec CLI Contract

- Status: Design for R4
- Date: 2026-08-23
- Requirements: R4 and R5 in `frd.md`

## Surface

Each instance command group owns the same two spec verbs:

```text
agw vm set-spec NAME SPEC
agw vm clear-spec NAME

agw workspace set-spec NAME SPEC
agw workspace clear-spec NAME

agw agent set-spec NAME SPEC
agw agent clear-spec NAME

agw session set-spec NAME SPEC
agw session clear-spec NAME
```

`SPEC` is a required positional inline JSON object. The four direct creation commands also accept
`--spec JSON`. This lets initial realization use the final partial spec layer without creating an
ownerless pending database record.

`session create` can create three owners in one request. Its `--spec JSON` applies to the session;
`--workspace-spec JSON` applies to the workspace created by `--new-workspace`, and
`--agent-spec JSON` applies to the agent created by `--new-agent`. A child spec without its matching
`--new-*` flag is a usage error, following the existing `--workspace-template` and
`--agent-template` ownership cues.

`workspace copy` does not accept `--spec`. A copied workspace currently stores the synthetic,
unresolvable template marker `copied` and does not run ordinary workspace-template realization.
Neither a copy-time spec nor a later `workspace set-spec` can be validated against a real base
chain, so the latter fails cleanly for that owner. Supporting it requires a separately designed way
to select and realize a destination template; this contract does not silently invent one.

The verb-object names follow the CLI convention for operations involving a second object. There is
no new generic `instance` command group and no `resource` mutation command for live instances.

## Input value

The input is one inline JSON object. Its fields are the declarable spec fields for that instance
kind. The top level must be an object; arrays, scalars, and `null` are rejected. There is no
wrapper, and the object cannot declare `kind`, `name`, `inherits`, description, source metadata, or
framework provenance.

Parsing is strict JSON at the service boundary. Duplicate object member names, non-finite numbers
such as `NaN` or `Infinity`, and trailing content are rejected with the normal typed validation
error. No parser-specific extension or last-member-wins behavior reaches the typed model.

For example, an existing VM's final partial spec layer can be replaced directly:

```console
agw vm set-spec build-01 '{"cpus":8,"memory":16,"apt_packages":["ripgrep"]}'
```

The same layer can participate in initial creation:

```console
agw vm create build-01 --template dev \
  --spec '{"cpus":8,"memory":16,"apt_packages":["ripgrep"]}'
```

This input is command data, not an instance manifest or a file reference. Agentworks does not
discover it, watch it, retain a source path, or make it part of the Resource Registry. The command
parses it into the kind's strict typed partial-spec model and persists canonical versioned data in
the database. The database record is the sole desired instance-spec authority after the command
returns.

## Replacement and merge semantics

`set-spec` and `--spec` validate and store the complete supplied partial layer atomically. They are
not field-patch operations. Replacing an instance spec with an object that omits an old field
removes that field's contribution and reveals the value from the template chain or domain default.
`clear-spec` removes the complete desired layer. Clearing an absent layer succeeds as a no-op.

The stored object is the final input to the shared layer runner:

- a scalar declared by the instance spec replaces the resolved template scalar;
- an ordinary map declared by the instance spec merges by key, with its value winning;
- a list declared by the instance spec appends with the kind's existing stable deduplication; and
- an empty list or map does not clear inherited content because the template model has no removal
  tombstone.

Session harness selection keeps its domain reducer rather than using ordinary map merge. Naming a
different harness integration resets the prior integration config. Naming the same integration
combines config through that integration's typed merge function, including integration-owned
behavior such as the shell integration's stable union of required commands.

There is deliberately no `--set PATH=VALUE`, JSON-path patch language, or generic key-value surface.
Those shapes would create a second type system for nested values, map keys, list semantics, and
field removal alongside the domain models that already own them.

## Validation and effects

The service first validates the partial spec shape, folds it after the selected template chain, and
runs the effective-instance reference and capability validators. Any failure leaves the prior
database record unchanged. A create command validates the complete effective spec before writing
local owner state or starting remote work.

For an existing instance, `set-spec` and `clear-spec` change desired database state only. They do
not invoke VM reinit, workspace repair, agent reinit, session resume, or any remote mutation. The
success result says that explicitly. A later lifecycle operation records only the applied slices it
can prove; unsupported or unapplied desired fields remain visible as not recorded or drift.

For a creation command, the supplied instance spec is part of the effective spec used by that
creation. The owner row and desired layer are inserted or removed atomically at each existing
database boundary. When lifecycle unwind removes an owner, typed owner deletion removes its overlay
in the same transaction. This does not change existing failure retention: if a VM row survives an
initialization failure, or another lifecycle retains an already-created owner, its desired overlay
survives with it.

## Inspection and machine output

The existing `vm describe`, `workspace describe`, `agent describe`, and `session describe` surfaces
show the stored instance spec and either the current fully resolved spec or an explicit unresolved
state. Per-value provenance and comparison state appear only where resolution exists; applied slices
remain separately visible. Their JSON v1 forms add optional tagged fields without changing existing
fields. Configured secret references may appear; resolved secret values never do.

The spec commands produce ordinary human success output. Automation reads the structural JSON v1
description after mutation.
