# Direct artifact declarations: requirements

## Authority and scope

On 2026-11-15 the operator approved the lead's response to issue #907 and directed implementation.
This SDD records that approved contract. It extends artifact authoring across all supported owning
scopes. The locked artifact and harness-scope SDDs remain historical; the open MCP SDD retains its
own scope and later OAuth work. No prior effort is reopened by this change.

## Outcome

An operator can define a single-purpose artifact alongside the owning template's environment, or
supply a one-off session artifact through the ordinary instance spec, without first registering a
named artifact bundle. Named bundles remain the reusable form. Both forms reach the same normalized
artifact pipeline.

## Requirements

1. **Authoring.** Every existing owning `artifacts` block accepts `declare`, containing the same
   five type maps as a bundle: `hints`, `rules`, `skills`, `agents`, and `mcp_servers`. Entry validation
   and supported content sources are identical. Direct declarations accept no resource metadata,
   inheritance list, nested bundle references, unknown fields, or per-entry null tombstones.
2. **Selection.** Resolve selected named bundles in their existing order, then apply the effective
   direct declarations. Within one owner, a later same-type/key definition replaces the complete
   earlier artifact, retaining ordinary replacement evidence and warnings for changed content.
   Different types and different original owners retain their separate namespaces.
3. **Inheritance.** Direct type maps merge by key through template and instance layers. An artifact
   entry replaces as a unit. Omission and an empty `declare: {}` preserve inherited definitions;
   `declare: null` clears inherited direct definitions. A later layer can declare new content after
   that clear. `bundles: []` clears inherited bundle references only. Clearing direct declarations
   does not clear bundle references.
4. **Capture and identity.** Acquire and normalize both forms with the same validation, LF text
   normalization, source semantics, revision resolution and operation-wide limits. Changing a
   declaration's packaging from a named bundle to direct content does not change its owning
   scope/type/key identity or its input identity when normalized content is unchanged. Routing,
   placement, native registration, adoption, cleanup and retention keep their existing contracts.
5. **Freshness and compatibility.** Direct changes participate in declaration freshness checks.
   Existing bundle-only declarations keep their current fingerprints and capture interpretation;
   upgrading alone must not make them stale. Existing persisted input and ownership state remains
   readable. No migration of operator declarations is required.
6. **Inspection and diagnostics.** `agw artifact show` includes direct definitions with missing,
   current or stale captures, explaining selection and replacement without inventing a bundle name.
   Human output identifies content declared on its owner. Existing machine fields stay compatible;
   direct selection may add explicit metadata. Inspection stays offline and omits artifact bodies
   and MCP connection values. Acquisition and replacement diagnostics retain safe addresses.
7. **Delivery.** Permanent artifact guidance, relevant schema teaching and examples ship with the
   implementation. Ordinary resource schema/reference extraction, owner resolution, instance specs
   and packaged guides must accept and explain the same shape.

## Boundaries

Direct authoring does not imply inline bytes: a `source` can still acquire workstation or Git
content. Co-locating MCP and env declarations reduces authoring mistakes; it does not add runtime
environment probes, validate final login-shell bindings, install servers, authenticate clients, or
expand native session support. Features, new source formats and OAuth remain separate work.
