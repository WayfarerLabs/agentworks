# Low-Level Design: Declarative-Schema Guide Adapter

- Status: Proposed for Phase 1 release-gate implementation
- Authoritative dependency: declarative-schema Phase 2 on `main` at merge commit `5c0b6e18`
- Parent design: `hla.md` and `guide-contract-lld.md`

## Scope

This adapter closes the narrow Phase 1 release gate created when the automated resource migrator was
deleted. It binds existing `FieldReference` and `Sample` guide blocks to the declarative-schema
services on `main`, adds the exceptional `concept-migration` teaching topic, and keeps the topic
usable while operator configuration is invalid.

The later Phase 4 registry-inventory work remains separate. This adapter does not add graph
templates, a migration engine, a frozen old-schema oracle, or a general upgrade workflow.

## Authoritative sources

The adapter consumes facts, never another command's rendered output:

- `agentworks.manifests.reference.describable_targets() -> tuple[str, ...]` enumerates declarable
  kinds, capability kinds, and installed capability implementations without loading config, building
  a runtime registry, or constructing a capability.
- `agentworks.manifests.reference.reference_for(target) -> SchemaReference` supplies the title,
  summary, overview, metadata tree, spec tree, alternatives, and optional root value for a kind or
  capability implementation.
- `agentworks.manifests.samples.sample_text(kind) -> str` supplies a live declarable-kind sample
  from the same model stream the loader validates.
- `agentworks.manifests.reference.plain_text(text) -> str` is the shared normalization for model
  prose whose reStructuredText code spans become Markdown code spans.

`SchemaReference`, `FieldEntry`, and their underlying `FieldDoc` values are the field-rendering
contract. Guide code does not call `manifests.describe.reference_lines`, invoke the resource CLI,
walk Pydantic models, or retain a second list of fields.

## Topic and block target resolution

Schema blocks resolve their targets from the contribution anchor. Contributors do not supply a
callable, renderer name, or arbitrary lookup expression.

| Anchor                             | `FieldReference` target | `Sample` target                                |
| ---------------------------------- | ----------------------- | ---------------------------------------------- |
| `KindAnchor(kind)`                 | `kind`                  | `kind` when declarable                         |
| `ResourceAnchor(kind, name)`       | invalid in this gate    | invalid in this gate                           |
| `ImplementationAnchor(kind, name)` | `kind/name`             | invalid because capabilities are not manifests |
| `ConceptAnchor(name)`              | invalid                 | invalid                                        |

Trusted and plugin contribution validation rejects an incompatible anchor/block combination as a
scoped content issue. `FieldReference.section` is a tuple of field-path segments. An empty tuple
renders the complete reference. A non-empty tuple selects one exact metadata or spec subtree and
fails closed when the path is absent or ambiguous.

Dynamic guide topics use the following block sets:

- a declarable bare kind renders current inventory, field reference, and live sample;
- a capability bare kind renders its alternatives as a field reference and current inventory;
- a capability implementation renders state, relationships, current instances, and its field
  reference;
- a declared resource renders state, relationships, current instances, and links to its bare kind
  for the shared field reference and sample.

`describable_targets()` participates in the guide name set independently of the runtime registry.
Disabled and not-enabled implementations therefore remain discoverable and documentable. Runtime
resource names are added only when a registry is available. The existing broken-config exception
remains: an explicitly requested, syntactically valid `declarable-kind/name` is accepted as an
unavailable resource topic even though it cannot appear in completion names without a registry.

## Rendering

For kind and implementation topics, `SchemaReference.title` becomes `TopicContribution.title`,
`summary` becomes `TopicContribution.summary`, and `overview` becomes one `Overview` block. The
field block does not repeat those authored facts. Authored overview Markdown crosses the ordinary
contribution validator and remains Markdown; it is not escaped into plain text.

The landed `session-template` overview documents `{{session_name}}` and `{{workspace_name}}` inside
same-line, unescaped single-backtick spans. The ordinary validator preserves only that exact form.
Multi-backtick runs, fenced blocks, multiline spans, escaped backticks, unmatched openers, and
prose-position delimiters remain invalid. No trusted bypass exists, and the overview is not passed
through `plain_text`.

The field renderer then reads only the remaining `SchemaReference` records. Its Markdown names the
target and emits stable rows for path, required or optional status, type, default or owner-templated
default, description, choices, constraints, examples, and reference marker when those facts exist.
Nested paths come from the existing `FieldEntry` tree. Capability-kind references render their live
alternatives and exact `kind/name` targets. Root-valued implementation config renders its one root
entry without inventing a `spec` wrapper. Only projected plain-text field facts and alternative
summaries are escaped for Markdown.

Literal defaults, examples, choices, and constraints never pass through `plain_text`. Their document
form comes from `agentworks.manifests.yaml_value.render_value`, the same wire-value serializer used
by live samples. For the inline field row, backslash, carriage return, line feed, and tab in that
wire text become the visible lossless sequences `\\`, `\r`, `\n`, and `\t`. The single-line result
is wrapped in a safe Markdown code span whose backtick delimiter is one character longer than its
longest backtick run. Values that begin or end with a backtick or space are space-padded inside that
delimiter. `plain_text` remains limited to authored model prose such as descriptions and alternative
summaries.

The sample renderer calls `sample_text` only for a declarable kind and embeds the returned text in a
fenced YAML block. It never calls `write_sample`, touches the filesystem, or treats a capability
reference as a manifest.

Both renderers still pass through the guide's final terminal-control sanitizer. Renderer-owned
headings retain the `⟦AGW framework⟧` label. Human and agent modes receive identical schema block
payloads.

Schema blocks are independent of `GuideView`. `render_topic` resolves them before applying a live
registry unavailability notice. Thus a broken `config.toml` may make state, relationships, and
inventory unavailable while field references and samples still render successfully from the code
registry. A schema-service error is framed for that block and does not erase static teaching.

## Migration teaching and inert actions

`concept-migration` is a core, package-data topic with static overview, agent contract, teaching, an
`ActionList`, and related-topic blocks. It teaches the 0.14 resource-model migration as an
exceptional operation, not as the normal upgrade experience. `concept-onboarding` and
`concept-management` link to it and do not copy its procedure.

Static prose explains sequencing and interpretation, but every suggested read, workstation probe, or
mutation that crosses a consent boundary is also a validated `GuideAction`. The records name the
exact file, directory, kind, or manifest input; use `READ_CONFIGURED_STATE`, `EXAMINE_WORKSTATION`,
or `MUTATE_AGENTWORKS` as appropriate; state the expected result and verification step; and give a
refusal alternative that preserves the last known-safe state. They are rendered instructions and are
never executed by `agw guide`.

The core topic contributes these records in order. Command actions use only the current portable
Agentworks CLI. Manual actions name their exact inputs and outcomes but let the operator choose
platform-native copy, inspection, and editing tools.

| Action ID                     | Consent                 | Operation                                                                                                                                                                                                                                                                                                                                                                              | Verification or refusal boundary                                                                                                                                                          |
| ----------------------------- | ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `inventory-retired-resources` | `READ_CONFIGURED_STATE` | Manual: read `CONFIG_PATH` plus existing manifests under optional `RESOURCES_PATH`. Before backup or editing, record the complete union: every pre-existing manifest and every manifest-producing retired section as canonical `kind/name`, `operator-declared`, and the existing or operator-chosen intended manifest file. Exclude `[secret_backends.*]` and collapse nested tables. | The caller owns one immutable expected identity-and-origin set; refusal stops before backup or editing.                                                                                   |
| `backup-config`               | `MUTATE_AGENTWORKS`     | Manual: copy `CONFIG_PATH` to a fresh operator-selected `CONFIG_BACKUP_PATH` outside and distinct from the active config and resources trees.                                                                                                                                                                                                                                          | Refusal leaves config untouched and stops migration.                                                                                                                                      |
| `backup-resources`            | `MUTATE_AGENTWORKS`     | Manual: when `RESOURCES_PATH` exists, copy it to a fresh `RESOURCES_BACKUP_PATH` outside and distinct from both active trees. Otherwise record an absent resources baseline and create no active path.                                                                                                                                                                                 | Refusal leaves resources untouched and stops migration.                                                                                                                                   |
| `verify-migration-inputs`     | `READ_CONFIGURED_STATE` | Manual: compare config backup with source, verify a matching resources copy or recorded absent baseline, and review the already-complete expected identity-and-origin set before any edit.                                                                                                                                                                                             | Every present backup must match; destinations must be outside active trees; verification must not add or rewrite expected entries. Otherwise stop.                                        |
| `edit-one-manifest`           | `MUTATE_AGENTWORKS`     | Manual: edit only the pre-recorded `MANIFEST_PATH` for `MANIFEST_KIND`, using its sample and, when tagged config is present, the separate field reference for optional `CAPABILITY_TARGET`.                                                                                                                                                                                            | Consume but never change the expected entry; leave the last validated manifests unchanged on refusal and do not remove TOML.                                                              |
| `validate-manifest-set`       | `EXAMINE_WORKSTATION`   | `agw doctor`                                                                                                                                                                                                                                                                                                                                                                           | The new manifest receives precise feedback while the retired-section config failure remains; refusal leaves the edit unverified and blocks cutover.                                       |
| `review-null-secret-fields`   | `READ_CONFIGURED_STATE` | Manual: inspect only the named site manifests for `token_secret`, `service_principal.secret`, and `credentials.access_key_secret`.                                                                                                                                                                                                                                                     | Record default, custom, or ambient-auth intent per hit; refusal leaves the site unchanged and blocks cutover.                                                                             |
| `remove-retired-sections`     | `MUTATE_AGENTWORKS`     | Manual: remove every retired resource section from `CONFIG_PATH` in one edit and update `[secret_config].backends` when needed.                                                                                                                                                                                                                                                        | Refusal restores or retains the untouched config and its hard error.                                                                                                                      |
| `compare-operator-inventory`  | `READ_CONFIGURED_STATE` | `agw resource list --origin operator`                                                                                                                                                                                                                                                                                                                                                  | Compare identity, `operator-declared`, and manifest file with the expected set while ignoring source line; any missing or extra row stops completion and uses the backups to investigate. |
| `finish-doctor`               | `EXAMINE_WORKSTATION`   | `agw doctor`                                                                                                                                                                                                                                                                                                                                                                           | Zero failures completes the migration; refusal leaves host readiness unverified.                                                                                                          |

The teaching covers:

1. before backup or editing, record the complete expected final operator set as the union of
   pre-existing manifest identities and manifest-producing retired sections, including canonical
   `kind/name`, the `operator-declared` variant, and each existing or operator-chosen intended
   manifest file; exclude source line from equality, exclude `[secret_backends.*]`, and collapse
   nested tables into their parent; later steps verify or consume this immutable set but never add
   to it;
2. preserve config and, when present, resources-directory backups at fresh destinations outside the
   active trees; record an absent resources baseline when appropriate, then verify the copies or
   absence plus the expected identity set before editing;
3. use `agw resource sample KIND` or `agw guide KIND` for the live manifest shape and use
   `agw resource describe-kind KIND/NAME` or `agw guide KIND/NAME` for tagged capability config;
4. write one manifest at a time while leaving all retired TOML sections in place, then run
   `agw doctor` after each edit so its degraded config path validates the growing manifest set;
5. treat `[secret_backends.*]` as the one section family with no manifest: delete those empty
   declarations during the final TOML cutover and activate desired backends through
   `[secret_config].backends`;
6. fix closed-world fields, strict types, non-nullable nulls, and the retired sibling capability
   shape from the emitted error and live field reference;
7. scan explicitly for the three changed null-secret fields, then choose a custom secret name or
   accept the default name; Azure and AWS may instead remove the enclosing credentials block for
   ambient authentication, while Proxmox has no no-secret mode;
8. remove every retired TOML resource section in one pass, compare the normal
   `agw resource list --origin operator` inventory with the pre-migration names, and finish only
   when `agw doctor` reports zero failures.

The section-to-kind mapping is release-history teaching and may be authored because the current
schema cannot derive a retired TOML name. Target manifest fields and samples are never authored in
the topic.

## Known upstream documentation inconsistency

The permanent 0.14 upgrade guide first states correctly that omission and explicit null both select
the default secret, then later says deleting the null line means no secret. Landed models and tests
show that the later statement is false. This effort does not edit the locked declarative-schema SDD
or silently take ownership of its permanent guide. The operator is notified, and `concept-migration`
follows the implemented behavior described above.

## Verification

The adapter is complete when tests prove:

- `concept-migration`, one declarable kind, and one disabled capability implementation render in a
  single atomic request while `config.toml` is refused;
- dynamic `SchemaReference` prose passes through contribution validation once, preserves authored
  overview Markdown, and does not repeat it in the field block;
- exact same-line single-backtick spans preserve literal expression delimiters, while prose,
  multi-backtick, fenced, multiline, escaped, and unmatched placements are rejected for trusted and
  plugin contributions alike;
- field and sample payloads change when fixture model declarations change, with no guide switchboard
  or copied field list;
- capability references never reach the sample renderer;
- resource and concept anchors reject schema blocks, while an explicit resource request still
  degrades to unavailable when config cannot build the registry;
- section selection is exact and invalid selectors fail as scoped guide content;
- onboarding and management link to migration without duplicating its teaching;
- migration output renders validated consent-bearing action records, preserves fresh out-of-tree
  backups or an absent baseline, carries the union of existing and migrated operator identities,
  names the normal load, inventory, and per-manifest doctor loop, handles backend sections, and
  contains the exact null-secret choices;
- final inventory comparison accepts a preserved operator resource whose manifest source line moved
  while requiring its identity, operator-declared variant, and manifest file to match;
- schema scalar rendering uses `render_value` for booleans, enums, collections, empty and whitespace
  strings, and embedded backticks; backslash, CR, LF, and tab become distinct visible sequences
  before safe single-line code-span wrapping;
- guide power-boundary, mode-parity, completion, package-data, full Phase 1, and repository gates
  remain green.
