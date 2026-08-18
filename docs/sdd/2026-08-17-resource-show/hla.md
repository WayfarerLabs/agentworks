# Resource Show: High-Level Architecture

- Status: Approved by the operator for implementation
- Date: 2026-08-17
- Implements: `frd.md`
- Code basis: `origin/main` at `217930fd`

## Summary

`resource show` is one registry-backed read path with two projections. The CLI parses the shared
resource identity, loads one finalized request registry, asks a presentation-free service for a
closed `ResourceShow` fact record, and selects the human or JSON renderer. Neither renderer reaches
back into config, registry, graph, a capability implementation, or the database.

The architectural distinction from the deleted card is structural. The service has no relationship
or live-instance inputs, and its record has no place to carry those facts. Its only richer field is
the normalized declaration available when the selected row implements the common `DeclaredResource`
contract.

## Current anchors

- `resources.access.parse_resource_identity` and `resolve_resource` already own selector syntax and
  typed registry lookup.
- `DeclaredResource` and `METADATA_FIELDS` already define the declarable row's envelope, spec, and
  framework field split.
- `Registry.graph` already stores enablement and readiness verdicts produced during registry
  finalization.
- `machine_output.py` already owns the closed JSON v1 command enumeration, origin projection, and
  atomic JSON writer.
- `resources.render.format_origin_line` already owns the human origin spelling.
- The completion specification already exposes the `resource_refs` source used by resource edit and
  graph show.

## Architectural decisions

### A1. Add a focused closed service record

A small module beside resource inspection owns frozen records conceptually equivalent to:

```text
ResourceReadiness(is_ready, is_available, reason)
ResourceShow(
    identity,
    category,
    description,
    origin,
    enablement,
    readiness | null,
    declaration,
)
```

`show_resource(registry, identity)` performs one shared validated lookup. It reads category from the
registered kind handler, uniform row facts from the resolved row, and the already-finalized state
axes from `registry.graph`. The declaration is a closed JSON object or null. The result carries no
registry, graph, handler, row, implementation class, config, or database object.

The module also owns the JSON projector and human renderer because all three are one small cohesive
feature. If the implementation would push the existing broad `resources.inspect` module past a clear
reading boundary, the focused module is preferred over regrowing the former card service there.

### A2. Reconstruct a normalized declaration only from `DeclaredResource`

For a `declarable` kind, the row must be a `DeclaredResource`. The projector calls
`model_dump(mode="json", include=declared_field_names, exclude_none=True)` once and partitions the
result using the shared base models:

- metadata keys are emitted in `EnvelopeMetadata.model_fields` declaration order;
- spec keys retain the concrete Pydantic model field order; and
- the outer envelope is `apiVersion`, `kind`, `metadata`, then `spec`.

`declared_field_names` is the concrete model's fields minus the framework-only fields derived from
`DeclaredResource.model_fields` and `METADATA_FIELDS`. The include set prevents `declared_at` and
`origin` from entering recursive serialization at all rather than dumping and deleting them later.

The API version comes from `manifests.envelope.API_VERSION`. The projector includes defaults because
the command answers what the normalized loaded row contains, not which keys were authored. Nulls are
omitted because the canonical manifest does not need them to reproduce the normalized row. An empty
spec remains an empty object.

The projector does not use the permissive teaching-oriented YAML value fallback: an unexpected
non-JSON value is a contract failure, not text to stringify. Pydantic JSON mode is the only
recursive conversion authority. Framework fields `declared_at` and `origin` are excluded through the
derived include set and also covered by a completeness test against the shared framework contract.

For a `capability` kind, declaration is null without reflecting over the dataclass. A category/row
contract mismatch raises loudly as an internal invariant failure rather than leaking a partial view.

### A3. Keep readiness and enablement factual and separate

The CLI uses the ordinary request-registry build, matching `resource list`, so the record reports
the same finalized host-readiness and plugin-enablement facts operators already see in inventory.
`show_resource` reads only `enablement_of` and `readiness_of`; it never recomputes either and never
dispatches a kind hook.

The record preserves enablement as the existing `enabled` or `disabled` enum value. For an enabled
row, it preserves readiness's three structural facts: `is_ready`, `is_available`, and optional
`reason`. For a disabled row, readiness is null. Finalization deliberately skips readiness
evaluation for disabled nodes and stores a ready placeholder for fold mechanics; projecting that
placeholder as an observed verdict would be false. Renderers may choose concise labels but may not
collapse the axes or turn a reason into remediation. `doctor` remains the diagnostic owner.

### A4. Human and JSON output are projections of the completed record

JSON v1 adds `MachineOutputCommand.RESOURCE_SHOW`. The data shape is:

```json
{
  "resource": {
    "kind": "secret",
    "name": "npm-token",
    "category": "declarable",
    "description": "npm registry token",
    "origin": {
      "variant": "operator-declared",
      "file": "/home/operator/.config/agentworks/resources/secrets.yaml",
      "line": 7,
      "source": null,
      "source_resource": null,
      "plugin": null
    },
    "enablement": "enabled",
    "readiness": {
      "is_ready": true,
      "is_available": true,
      "reason": null
    },
    "declaration": {
      "apiVersion": "agentworks/v1",
      "kind": "secret",
      "metadata": { "name": "npm-token", "description": "npm registry token" },
      "spec": {}
    }
  }
}
```

The concrete origin object retains the existing fixed six-field shape. A capability uses the same
resource object with `declaration: null`.

The human renderer emits the uniform facts, then a deterministic block-style YAML declaration for a
declarable row. Every value interpolated into a fact line passes through a line-safe scalar helper
that applies `sanitize_terminal_output` and removes line feeds and tabs, matching graph's existing
inert-scalar boundary. The declaration mapping is encoded as YAML first and the complete YAML text
is then passed through `sanitize_terminal_output`: intentional document line feeds remain, while a
scalar's embedded line break remains governed by YAML quoting or block-scalar structure instead of
becoming a new fact line. Capability output has no YAML block and makes the null declaration
legible. Rendering starts only after `show_resource` returns; JSON encoding completes before the
first stdout write.

### A5. Keep the CLI and completion wiring conventional

The command function is added to the existing resource Typer group. It parses the identity before
config work, loads config and a finalized request registry, calls `show_resource`, and dispatches
one renderer. Both loader calls suppress advisory output so a later typed lookup or projection
failure cannot leave a warning-prefixed partial result. It does not call `get_db`.

The argument is named `ref`, matching `resource edit`, and maps to `resource_refs` in the completion
spec. The existing generic bash, zsh, and PowerShell generators require no algorithm change.

### A6. Prove the ownership boundary observationally

Service tests use a finalized registry with representative operator, auto, built-in, capability,
disabled, and not-ready rows. Projection tests assert the complete record and JSON structure, plus
declaration round trips and exact exclusion of framework fields. Renderer tests feed inert records
and assert complete fact projection without pinning labels or sentences. Structural injection tests
place line feeds, tabs, DEL, C1, and ANSI controls in identity-adjacent text, descriptions,
readiness reasons, origin details, and declaration scalars; they prove facts cannot inject sibling
lines and that the emitted declaration contains no literal C0 controls other than its structural
line feeds, no literal DEL or C1 controls, and no terminal escape sequence. Parsing the declaration
shall recover the original scalar values because the YAML encoder represents embedded controls as
inert printable escapes before terminal sanitization.

CLI tests replace config and registry loading with controlled boundaries, assert the machine command
identity and output schema, assert both loader warning flags are false, and make database access
fail if attempted. Existing graph, explain, secret-describe, edit, and removed-spelling tests remain
the regression proof that ownership did not move. A registered-kind sweep projects every declarable
row fixture available to the framework and proves the declaration is JSON encodable without secret
resolution.

## Rejected alternatives

- **Rename or restore the deleted card:** rejected because it would mix graph, database, and
  diagnostic ownership again.
- **Put concrete values in `resource explain`:** rejected because explain is config-free accepted
  shape, not a loaded row.
- **Put declarations in `graph show`:** rejected because declarations are node content, not
  relationships.
- **Reflect capability implementation configuration:** rejected because capability explain owns the
  schema and future facets, while consuming resources own placement.
- **Preserve source-exact YAML:** rejected because registry rows intentionally do not retain that
  syntax or comments, and pretending otherwise would create a second manifest storage model.
- **Add a compatibility alias:** rejected by explicit operator direction and the 0.14 break posture.
