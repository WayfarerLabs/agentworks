# Resource Show: Functional Requirements

- Status: Approved by the operator for implementation
- Date: 2026-08-17
- Parent saga: `docs/sdd/2026-08-04-next-steps/`
- Successor to: `docs/sdd/2026-08-10-cli-grammar/`

## Purpose

Add the missing focused inspection question to the resource grammar: given one concrete registry
identity, what resource did Agentworks actually load? The answer is a factual projection, so the
command is `agw resource show KIND/NAME`.

This is an explicit successor ruling to the CLI grammar correction. It does not restore the old
generic `resource describe` card. That card mixed identity, relationships, live usage, and
diagnostics. The new command has one narrower job: project the selected registry row and, for a
declarable resource, its canonical normalized declaration.

## Settled product decisions

1. `show` means a factual projection of one concrete loaded resource. `explain` continues to mean
   accepted authoring shape, `graph show` continues to mean relationships, and `doctor` continues to
   mean diagnosis and remediation.
2. The selector is `KIND/NAME`, parsed and resolved through the existing shared resource identity
   service.
3. Declarable resources expose a normalized manifest envelope reconstructed from the loaded row. It
   is not source-exact YAML and it is not an effective inheritance expansion.
4. Capability resources expose only their uniform concrete registry facts. They have no resource
   declaration, and this command does not copy capability configuration or future facet models from
   `resource explain`.
5. `resource describe` and `resource.describe` remain removed. There is no alias, compatibility
   shim, or deprecation runway.
6. `secret describe` remains unchanged because its backend mapping and resolution preview are
   domain-specific synthesis.

## Functional requirements

### Command and identity

- **FR1.** The CLI shall add `agw resource show REF`, where `REF` is exactly one `KIND/NAME`
  argument.
- **FR2.** The command shall reuse the shared first-slash parser and validated registry resolver.
  Names containing dots, colons, or legacy double hyphens remain ordinary names.
- **FR3.** The command shall load the operator's config and finalized request registry, then show
  disabled rows as resolvable concrete entries rather than hiding them. Loader warnings shall be
  suppressed so lookup and fact assembly complete before command output begins.
- **FR4.** Unknown kinds, unknown names, and malformed identities shall retain the shared typed
  resource errors and structural metadata.

### Fact projection

- **FR5.** The service result shall contain exactly these resource facts: kind, name, category,
  description, origin, enablement, optional readiness, and declaration.
- **FR6.** Category shall be the registered kind category, `declarable` or `capability`.
- **FR7.** Enablement and readiness shall remain separate axes. The projection shall expose the
  stored enablement value. For an enabled row it shall expose the stored readiness verdict,
  including its optional reason and whether the verdict was available. For a disabled row it shall
  expose readiness as null because finalization deliberately does not evaluate readiness for a
  disabled node; its internal ready placeholder is not an operator verdict.
- **FR8.** Origin shall use the existing safe four-variant origin projection. Missing origin stays
  null in JSON and renders as unknown for humans.
- **FR9.** Description shall come from the concrete row when that row carries one, otherwise it
  shall be the empty string. The service shall not reach into a capability implementation merely to
  manufacture description text.

### Normalized declarations

- **FR10.** A declarable resource shall project a JSON-native manifest envelope with the ordered
  keys `apiVersion`, `kind`, `metadata`, and `spec`.
- **FR11.** `metadata` shall always contain `name`, and shall contain non-null `description` and
  `expires` values from the loaded row. `spec` shall contain every non-null kind-specific loaded
  field, including model defaults and an authored `inherits` selector when present.
- **FR12.** Framework fields such as `declared_at` and `origin` shall never enter the declaration.
  The projector shall derive the metadata/spec split from the shared declared-resource contract, not
  a per-kind field list.
- **FR13.** Dates, timestamps, enums, nested models, mappings, and collections shall be converted
  through Pydantic's JSON-mode projection. The result shall satisfy the closed JSON v1 value
  contract without converting arbitrary objects to strings.
- **FR14.** A capability resource shall carry `declaration: null`. The command shall not project
  implementation classes, configuration models, configuration placement, or capability facets.
- **FR15.** The declaration represents the normalized row published in the registry. It shall not
  preserve source comments, key spelling, document order, omitted-versus-defaulted distinctions, or
  the exact source document, and it shall not resolve or merge inheritance.

### Human and machine output

- **FR16.** `--output` shall accept the existing closed `human` and `json` formats and default to
  human.
- **FR17.** Machine output shall add the stable command identifier `resource.show` to JSON v1. Its
  `data` object shall contain one `resource` object with the FR5 facts; `declaration` shall be the
  FR10 envelope or null.
- **FR18.** Human output shall render the same complete service result. For a declarable resource,
  the declaration shall be recognizable as a normalized manifest; capability output shall state
  structurally that no declaration exists without inventing capability configuration.
- **FR19.** Both output paths shall complete config loading, registry loading, lookup, and fact
  assembly before emitting output. JSON shall retain atomic encoding, deterministic key order, and
  terminal-control safety. Human output shall remove line-breaking characters from interpolated
  scalar fact lines and sanitize the structured declaration block after YAML encoding, making
  untrusted row text inert without destroying intentional document newlines.

### Ownership boundaries and collateral

- **FR20.** The command shall read no state database, query no live instances, traverse no graph
  edges, resolve no secret values, call no remote providers, mutate no registry or config, and
  prompt for nothing.
- **FR21.** Relationships and live usage remain exclusively with `graph show`; accepted fields and
  capability facet models remain with `resource explain`; diagnosis and remediation remain with
  `doctor`; source location and mutation remain with `resource edit`.
- **FR22.** `resource list`, `resource kinds`, `resource explain`, `resource edit`,
  `resource sample`, `resource schema`, `graph show`, and every kind-specific `describe` command
  retain their current contracts.
- **FR23.** Bash, zsh, and PowerShell completion shall offer registry identities for the `REF`
  argument through the existing `resource_refs` source.
- **FR24.** Command help, machine-output documentation, command reference, CLI overview, resource
  guide, and the 0.14 upgrade map shall teach the new ownership in the same change. Historical and
  locked SDD artifacts remain unchanged.

## Quality requirements

- **QR1.** The projection service and declaration projector shall have structural tests independent
  of the CLI.
- **QR2.** Tests shall assert types, fields, values, ordering contracts, side-effect boundaries, and
  command identities, not authored explanatory prose.
- **QR3.** The command module shall stay thin and preserve lazy imports and fast help generation.
- **QR4.** Projection shall be deterministic and secret-safe for every registered declarable kind.
- **QR5.** The implementation shall add no compatibility layer and no speculative capability-facet
  model.

## Acceptance criteria

1. `agw resource show KIND/NAME` returns the selected loaded resource in human and JSON forms.
2. An operator-declared declarable row produces a normalized manifest envelope that decodes to the
   same row facts after source-only framework fields are restamped.
3. Auto-declared and built-in declarable rows project safely; capability rows return a null
   declaration and only uniform facts.
4. Disabled rows carry null readiness, enabled not-ready rows carry their verdict, and unknown or
   malformed selectors fail through the shared typed resource boundary.
5. Instrumented tests prove the command does not open the database, traverse graph relationships,
   resolve secrets, or invoke provider operations.
6. `resource describe` remains unavailable, `secret describe` is unchanged, and completion and
   active documentation consistently teach the new division of responsibilities.
7. Focused tests, the full non-integration suite, Ruff, format, strict mypy, repository guards, and
   live local CLI acceptance pass before merge intent.

## Out of scope

- Restoring any field, relationship, live-usage section, or diagnostic prose from the deleted
  generic resource card.
- Source-exact YAML, source comments, document-order preservation, diffs, edit previews, or an
  effective merged inheritance view.
- Capability configuration, facet models, consumer placement, or relationship facet labels.
- YAML machine output, `resource show --field`, multi-resource show, or new graph queries.
- Compatibility aliases or changes to the permanent CLI verb glossary.
