# Model declaration and rendering design

Status: implementation design for accepted groups A-D. Interpolation performs textual substitution;
each consumer retains its downstream interpretation.

## Marker and supported shapes

Declarations:

```python
value: Annotated[str, Interpolated()]
command: Annotated[str, Interpolated()] = ""
extra_args: Annotated[list[Annotated[str, Interpolated()]], MergeStrategy.REPLACE]
```

Support strings, nullable strings and string list elements needed by the agreed inventory. Preserve
existing validation constraints and list merge policy. Plain metadata must not coerce values, render
tokens or query invocation context at load.

Reject conflicting or unsupported placement, including mapping keys, non-string values and
combination with reference markers on the same value. Do not add generic expression policies,
per-field context catalogues or arbitrary nested-container support without a consumer. Nested models
remain traversable for declaration discovery through existing schema infrastructure.

## Declaration projections

- Extend field documentation with a presentation-free interpolation fact, carrying the distinction
  between a scalar and its elements.
- Emit a namespaced JSON Schema extension, `x-agw-interpolation: true` on the string node. Preserve
  nullable and list structure rather than marking unrelated branches as interpreted.
- Have scalar shorthand schema and documentation derive the fact from its target model field. Do not
  broaden shorthand's accepted scalar type to hide the metadata gap.
- Add the fact to resource explanation and generated sample annotations. Guide prose teaches use and
  signposts these derived surfaces rather than duplicating field lists.

## Runtime adapter

Provide a small typed adapter for scalar and list-field consumption around the existing renderer. It
needs the declaring model and field, authored value, known context values and diagnostic owner.
Eligibility comes from the declaration; there is no runtime `interpolate=True` switch to contradict
it. Explicit rendering without the matching marker raises a located StateError. Components read
unmarked fields normally; their contents remain literal.

The declaring model may differ from a resolved value carrier. For example, effective agent setup
currently uses `ResolvedAgentTemplate`, a dataclass. Read policy from `AgentTemplate` and render the
selected resolved value; do not duplicate annotations on the carrier or reconstruct an entire config
solely to recover metadata.

Consumers retain the type and meaning of each destination. Env checks LF/CR/NUL after substitution.
Argument values are quoted once during shell serialization. Shell command fields are authored
source, so interpolation does not imply automatic shell escaping. New fields must report their
actual owner/field; the existing native helper's hardcoded `.extra_args` location must not survive
in a generic adapter.

Keep legacy token selection and notice logic outside the marker. Pass the existing compatibility
behavior only at eligible consumption sites, preserving request deduplication and doctor behavior.
The marker has no grammar-selection or deprecation parameters.

## Verification

Use a compact synthetic model to compare declaration, emitted schema, documentation facts and
runtime behavior. An unmarked sibling containing namespace-looking text must stay literal. Invalid
placement should fail at the existing model/registration boundary.

Exercise env shorthand, nullable setup fields and list elements where adopted. Reuse existing
consumer tests for precedence, secret opacity, single-pass replacement, selected Shell commands,
pre-secret checks, quoting and preview parity. Add behavioral coverage only for genuinely new field
boundaries and destination handling, not copies of the lexer matrix or assertions on prose wording.

## Internal prior art

This is an internal refactor following existing declarations, not a new template language.
`schema/markers.py` already projects reference annotations into schema; `schema/merge.py` reads
code-owned merge policy; `schema/fields.py` supplies the common documentation stream;
`schema/shorthand.py` owns alternate scalar spelling. Reuse those extension points. The pure
substitution engine and compatibility adapter already exist in `interpolation.py` and
`interpolation_compat.py`. No new external templating dependency or separate external prior-art
research is proposed.

## Adapter interface and PATH consumption

Use `render_scalar_field(model, field_name, value, values, *, location, legacy_placeholders=None)`
for strings/nullable strings and `render_list_field(...)` for string lists. Scalar overloads
preserve the caller's nullable type. Both read interpolation eligibility from the declaring model
and reject an explicit call for an unmarked field or incompatible marker shape. This check also
applies to nullable values and empty lists. Lists retain order and render each element.

Installer `path` elements use the list adapter with the install resource's real kind/name and field
location. Both the VM/admin and agent runners render the selected resource's contributions before
its remote completion checks or command execution. Successful and already-installed resources
contribute those rendered values; failed installs contribute nothing. The source model is unchanged.
No contribution is injected between commands in the batch.

VM/admin setup supplies managed user and VM facts. Agent setup also supplies the agent name.
Workspace and session context remain unavailable. Non-namespace shell expressions pass through
unchanged; neither the renderer nor the adapters read ambient environment variables.

The existing profile writers consume the rendered strings after the batch. They retain their
collection order/deduplication conventions, convert a leading tilde to `$HOME`, and emit each entry
inside `export PATH="<entry>:$PATH"`. Later shells interpret that source when they load the profile.
Text inserted by interpolation has the same downstream meaning as text authored directly in that
position. The mechanism does not escape inserted values, restrict shell fragments, normalize paths
or change shell expansion. The renderer has no destination encoder hook.

Permanent documentation and the guide describe both stages, the reinitialization lifecycle and the
difference between Agentworks namespace escaping and shell quoting. A real-shell behavioral check
sources generated profile content under a controlled environment to demonstrate that namespace
substitution happens in the CLI while ordinary shell expansion happens later.
