# Resource Show: Functional Requirements

- Status: Reopened for final review cleanup
- Date: 2026-08-17
- Parent saga: `docs/sdd/2026-08-04-next-steps/`
- Successor to: `docs/sdd/2026-08-10-cli-grammar/`

## Purpose

Add the missing focused inspection question to the resource grammar: given one concrete registry
identity, what does Agentworks know about this resource right now? The answer is a factual,
diagnostic projection, so the command is `agw resource show KIND/NAME`.

This is an explicit successor ruling to the CLI grammar correction. On 2026-08-18, after review of
the first narrow successor design, the operator ruled that focused inspection should be a superset
of the bulk resource views. The predecessor was right to remove the old generic `resource describe`
spelling and its loosely bounded card service. It was too narrow, however, to treat overlap with
bulk commands as a reason to omit useful facts. `resource list` and `doctor` are fleet-wide scans. A
named `show` is expected to contain every fact those surfaces would expose about the selected row
and to expand compact counts and statuses into the detail useful for investigating that one
resource.

## Settled product decisions

1. `show` is the complete focused view of one loaded resource. Overlap with `resource list`,
   `doctor`, and the direct neighborhood visible through `graph show` is intentional.
2. Bulk and focused surfaces shall share fact producers so overlapping fields cannot acquire
   different meanings. `show` shall not scrape rendered tables, filter authored doctor prose, or
   recompute registry verdicts.
3. `graph show` remains the general relational query: it owns direction, depth, traversal, and
   arbitrary neighborhoods. `resource show` includes only the selected row's direct declared
   relationships and current live usage.
4. `doctor` remains the complete system sweep. `resource show` includes every health check whose
   subject is the selected resource; unrelated install, config, database, shell, and completion
   checks remain global.
5. Declarable resources expose a normalized manifest envelope reconstructed from the loaded row. It
   is not source-exact YAML and it is not an effective inheritance expansion.
6. `resource describe` and `resource.describe` remain removed. There is no alias, compatibility
   shim, or deprecation runway. `secret describe` remains because its secret-specific synthesis is
   still a useful domain view, even where focused facts overlap.
7. The operator retained both state spellings on 2026-08-18: `disabled` and `not_ready_reason`
   preserve exact `resource list` parity, while `enablement` and `readiness` expose the richer state
   axes expected from the focused view. This additive successor does not migrate the existing
   `resource.list` machine contract.

## Functional requirements

### Command and identity

- **FR1.** The CLI shall add `agw resource show REF`, where `REF` is exactly one `KIND/NAME`
  argument.
- **FR2.** The command shall reuse the shared first-slash parser and validated registry resolver.
  Names containing dots, colons, or legacy double hyphens remain ordinary names.
- **FR3.** The command shall load the operator's config and finalized request registry, then show a
  named disabled row rather than hiding it. Human mode shall retain the same config and manifest
  advisories as sibling inspection commands; JSON mode shall suppress ambient warnings and remain a
  single clean envelope.
- **FR4.** Unknown kinds, unknown names, and malformed identities shall retain the shared typed
  resource errors and structural metadata.

### List-compatible row facts

- **FR5.** The `resource` object shall carry the complete `resource list` row contract: kind, name,
  origin, reference count, used-by count, description, not-ready reason, and disabled state.
- **FR6.** Those fields shall be produced from the same summary builder or shared lower-level
  projectors used by `resource list`; they shall not be separately reinterpreted for `show`.
- **FR7.** The focused record shall additionally carry category, the structural enablement value,
  the full stored readiness verdict when one exists, normalized declaration, direct relationships,
  current live usage, and focused diagnostic checks.
- **FR8.** Enablement and readiness remain separate axes. A disabled row shall expose
  `disabled: true`, `enablement: disabled`, and `readiness: null`; its folded ready placeholder is
  not an observed verdict. An enabled row shall expose the stored `is_ready`, `is_available`, and
  optional reason without recomputation.
- **FR9.** Origin shall use the existing safe four-variant projection. Missing origin stays null in
  JSON and renders as unknown for humans. Description comes from the concrete row when present and
  otherwise is the empty string.

### Direct relationships and live usage

- **FR10.** `relationships.dependencies` shall contain every direct declared edge from the selected
  resource, and `relationships.dependents` shall contain every direct declared edge into it. Each
  edge shall retain source, target, relationship, usage, and optional declarer provenance.
- **FR11.** Relationship order and projection shall reuse the graph service's canonical identity and
  edge ordering. The focused slice shall not walk beyond one edge or include induced edges between
  neighboring resources.
- **FR12.** `reference_count` shall equal the complete incoming relationship count under the list
  contract. Renderers may deduplicate repeated display lines for readability, but machine facts and
  counts shall retain every edge.
- **FR13.** `used_by` shall expand the same current-config live-instance hook that supplies
  `resource list`'s `used_by_count`. Kinds without an instance concept shall expose both fields as
  null; supported kinds shall expose a list, including an empty list when no live instance uses the
  resource, and its length as the count.
- **FR14.** The state database shall be opened only for the supported live-usage projection and
  shall follow the existing read-only inspection lifecycle. No provider, remote backend, or secret
  value is contacted to build relationship or live-usage facts.

### Focused diagnostics

- **FR15.** `diagnostics` shall contain every current doctor check whose subject is the selected
  resource, using the same status, message, hint, and underlying fact producer as the bulk report.
- **FR16.** The initial attribution set includes the readiness row doctor emits for an enabled VM
  platform or secret backend, VM-site readiness and per-site preflight, secret-source
  participation/readiness, secret resolution preview, and the existing dotfiles check only for
  `admin-template/default` when its dotfiles source is non-empty. Disabled VM platforms and secret
  backends receive no focused diagnostic because bulk doctor skips them; their show condition still
  exposes disabled enablement with null readiness. A kind with no resource-specific doctor check
  exposes an empty list.
- **FR17.** Checks about the host installation, config as a whole, database schema or contents,
  shell completions, plugins as origins, or a different resource/live instance shall not be
  force-fit into the selected record. Direct relationships and live usage still expose the context
  behind dependent-resource warnings.
- **FR18.** Focused diagnostics shall preserve doctor's safety boundary: read-only local checks and
  value-free predictions are allowed; prompting, secret resolution, authenticated runup, remote
  provider mutation, and unrelated system-wide sweeps are not.

### Normalized declarations

- **FR19.** A declarable resource shall project a JSON-native manifest envelope with the ordered
  keys `apiVersion`, `kind`, `metadata`, and `spec`.
- **FR20.** `metadata` shall always contain `name` and shall contain non-null `description` and
  `expires` values from the loaded row. `spec` shall contain every non-null kind-specific loaded
  field, including model defaults and an authored `inherits` selector when present.
- **FR21.** Framework fields such as `declared_at` and `origin` shall never enter the declaration.
  The projector shall derive the metadata/spec split from the shared declared-resource contract, not
  a per-kind field list.
- **FR22.** Dates, timestamps, enums, nested models, mappings, and collections shall be converted
  through Pydantic JSON mode and satisfy the closed JSON v1 carrier without arbitrary text
  conversion.
- **FR23.** A capability resource shall carry `declaration: null`. The command shall not project
  implementation classes, configuration models, configuration placement, or capability facets.
- **FR24.** The declaration is the normalized row published in the registry. It does not preserve
  comments, source order, omitted-versus-defaulted distinctions, or resolve/merge inheritance.

### Human and machine output

- **FR25.** `--output` shall accept the existing closed `human` and `json` formats and default to
  human.
- **FR26.** Machine output shall use stable command identifier `resource.show` and emit one closed
  JSON v1 `resource` object containing every FR5-FR24 field.
- **FR27.** Human output shall render the same complete service result in concise identity,
  condition, diagnostics, relationship, live-use, and declaration sections. A capability shall state
  structurally that no declaration exists.
- **FR28.** JSON encoding and fact assembly shall complete before the first JSON byte is written.
  Human fact rendering shall begin only after the selected row, its focused checks, relationships,
  live usage, and declaration have been assembled; normal human loader advisories may precede it.
- **FR29.** Both renderers shall preserve terminal-control safety. Interpolated human scalar facts
  cannot inject sibling lines, and the structured declaration block shall remain parseable after
  safe encoding.

### Ownership and collateral

- **FR30.** `resource explain` continues to own accepted fields and capability facet models;
  `resource edit` owns source location and mutation; kind-specific commands may retain richer domain
  synthesis. Overlap does not move those authorities.
- **FR31.** `resource list`, `doctor`, and `graph show` shall retain their existing public contracts
  while consuming shared focused fact producers where needed for consistency.
- **FR32.** Bash, zsh, and PowerShell completion shall offer registry identities for `REF` through
  the existing `resource_refs` source.
- **FR33.** Help, machine-output docs, command reference, CLI overview, installed management guide,
  resource guide, and the 0.14 upgrade map shall teach the focused-superset model in the same
  change. Historical and locked predecessor artifacts remain unchanged.

## Quality requirements

- **QR1.** Summary, direct-graph, live-use, diagnostic, declaration, and complete-show services
  shall have structural tests independent of CLI rendering.
- **QR2.** Tests assert types, fields, values, ordering, shared-producer parity, side-effect
  boundaries, and command identities—not authored explanatory prose.
- **QR3.** The command module stays thin and preserves lazy imports and fast help generation.
- **QR4.** Projection is deterministic and secret-safe for every registered kind.
- **QR5.** The implementation adds no compatibility layer, speculative capability-facet model, or
  generic provider probing framework.

## Acceptance criteria

1. `agw resource show KIND/NAME` returns the selected resource in human and JSON forms.
2. Every list-row field for the selected resource is identical to `resource list` under the same
   registry and database snapshot.
3. Direct dependencies/dependents and current live usages are complete, canonical, and their counts
   reconcile with the compact fields.
4. Every attributable doctor check for representative VM-platform, VM-site, secret-backend,
   secret-source, secret, and admin-template rows matches the bulk doctor's structured check.
5. Declarable rows carry a normalized declaration; capabilities carry null; disabled/readiness
   semantics and typed lookup failures remain truthful.
6. Structural and instrumented tests prove the command performs no traversal beyond direct edges, no
   secret value resolution, no authenticated runup, no remote provider mutation, and no prompt.
7. `resource describe` remains unavailable, `secret describe` remains unchanged, and completions and
   active documentation consistently teach the focused-superset model.
8. Focused tests, full non-integration pytest, Ruff, format, strict mypy, repository guards, and
   local CLI acceptance pass before merge intent.

## Out of scope

- Source-exact YAML, comments, document-order preservation, diffs, edit previews, or effective
  merged inheritance.
- Capability configuration/facets, relationship facet labels, transitive graph traversal, path
  finding, or multi-resource show.
- Global doctor checks unrelated to the selected resource, authenticated runup, provider calls,
  secret values, or mutation.
- YAML machine output, `resource show --field`, compatibility aliases, or permanent glossary work.
