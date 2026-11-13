# Model declaration and rendering design

Status: implementation design for accepted groups A-D. The public adapter surface and
profile destination encoding are specified below.

## Marker and supported shapes

Proposed declarations:

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
- Emit a namespaced JSON Schema extension, proposed as `x-agw-interpolation: true` on the string
  node. Preserve nullable and list structure rather than marking unrelated branches as interpreted.
- Have scalar shorthand schema and documentation derive the fact from its target model field. Do not
  broaden shorthand's accepted scalar type to hide the metadata gap.
- Add the fact to resource explanation and generated sample annotations. Guide prose teaches use and
  signposts these derived surfaces rather than duplicating field lists.

## Runtime adapter

Provide a small typed adapter for scalar and list-field consumption around the existing renderer. It
needs the declaring model and field, authored value, known context values and diagnostic owner.
Eligibility comes from the declaration; there is no runtime `interpolate=True` switch to contradict
it. Absence of the marker leaves the value literal.

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

## Adapter interface and profile source encoding

Use `render_scalar_field(model, field_name, value, values, *, location,
legacy_placeholders=None, encode_inserted=None)` for strings/nullable strings and
`render_list_field(...)` for string lists. Scalar overloads preserve the caller's
nullable type. Both read interpolation eligibility from the declaring model and
leave unmarked values unchanged. Lists retain order and are rendered elementwise.

For installer PATH additions, existing authored text is a double-quoted profile
fragment: leading `~` and ordinary authored shell variables continue to expand.
Only text inserted by Agentworks must be escaped for that double-quoted position.
A small destination encoder escapes backslash, double quote, dollar and backtick.
Apply it to context replacements and to the literal namespace prefix produced by
the grammar's escape operation. Source chunks remain authored shell source.

The pure renderer therefore accepts an optional `encode_inserted` callback with
identity behavior by default. The marker has no destination-policy parameter;
ordinary env values, command source and argv data keep their existing behavior.
The model-aware adapters forward the callback for the profile consumer. This
avoids a second lexer or a shell parser and prevents inserted dollar/backtick
text from becoming profile expressions. Both profile writers use the same
installer-produced fragments. Do not encode complete rendered paths, which
would also disable existing authored `$HOME` expansion.
