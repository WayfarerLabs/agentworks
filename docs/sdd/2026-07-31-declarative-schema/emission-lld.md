# LLD: schema emission and editor association (step 2.7)

Status: written 2026-08-06, at implementation time. Covers plan step 2.7's two boxes: JSON Schema
(2020-12) emitted per kind plus the envelope schema, the `agw resource schema` surface, and the
yaml-language-server modeline on written manifests.

Step 2.8's renderer may share this file if its seams overlap (the plan sanctions that); nothing here
constrains the renderer beyond section 8's hand-off list.

## 1. What emission derives from, and what it is NOT

Emission is a SIBLING of the field-reference stream, not a consumer of it (the HLA's 2026-08-06
correction to Component 1). Both derive from the models. Emission's authority is `model_json_schema`
over the same classes `iter_field_docs` walks, so a fact authored once on a field reaches both
surfaces with nothing to keep in sync. The marker's own `__get_pydantic_json_schema__` hook
(`schema/markers.py:142`) is what carries `x-agw-ref` across, and `tests/schema/test_markers.py`'s
round-trip test is what keeps the two derivations honest.

The consequence for this step: **emission writes no schema of its own.** Every property, type,
constraint, default, and description in an emitted document came out of a pydantic model. Where a
fact is not on a model, this LLD either derives it from the one place that owns it (section 3) or
records that it is deliberately not expressed (section 5).

## 2. The soundness contract

**An emitted schema is a sound UNDER-approximation of what the loader accepts:** everything the
schema rejects, the loader also rejects; the loader rejects more.

This is the load-bearing invariant, and the direction matters. A schema that is too permissive costs
an operator a missed completion or a diagnostic they get one second later from `agw doctor`. A
schema that is too STRICT red-underlines valid config in the operator's editor, confidently and
wrongly, which is worse than shipping no schema at all. So where a rule cannot be expressed
faithfully in JSON Schema, emission leaves it out rather than approximating it.

What the loader checks and the schema does not, today:

- field and model VALIDATORS (`Expiry`'s spelling check, `PromptMapping`'s blanket refusal, every
  cross-field rule a kind declares);
- the operator-name rules `decode._check_declared_name` applies (the character rule; the length cap
  IS expressed, see section 3.3);
- `envelope._NO_SELECTOR_KINDS` (`named-console-template` accepts only `name: default`);
- capability config for an implementation this host has not registered.

`tests/manifests/test_emit.py::test_emitted_schemas_accept_every_document_the_full_load_path_accepts`
pins the direction that matters: every document the real loader accepts also validates against the
emitted schema. Nothing pins the other direction, because the other direction is false by design.

**It runs `build_registry`, not `load_manifests`, and the difference is the point.** Capability
config is checked at FINALIZE, not at decode, so a document with an unknown key inside a capability
block passes `load_manifests` and is refused by `build_registry` (verified:
`harness_integration: {name: shell, nonsense: 1}`). A pairing test that stopped at decode would be
claiming "the loader accepts these" about a weaker loader than the one an operator runs. Both halves
are one test over one set of documents for the same reason: two tests can drift onto different
inputs, and then neither is pairing anything.

### 2.1 Three places the schema WOULD have been stricter than the loader

All three were found by that test rather than by reading, which is the argument for section 7's
dependency in one sentence. Each is fixed at the layer that owns the fact, never by the emitter
patching its own output.

- **A field the model FILLS emitted as required.** Pydantic computes `required` from the declared
  field, which knows nothing about `AgwModel._fill_owner_templated_defaults`, so
  `GitHubConfig.token` (marker default `git-token-{owner_name}`) emitted as required and an editor
  would have red-underlined `provider: {name: github}`, which is what every unscoped credential in
  the shipped sample writes. Fixed on `AgwModel.__get_pydantic_json_schema__`: the class that does
  the filling is the class that says so, and a consumer correcting for it downstream would be a
  second place to keep in sync. The field's `x-agw-ref` still carries the template, so a hover shows
  what the omission resolves to.
- **`spec:` with nothing after it.** The envelope reads a null `spec` as an empty mapping, so the
  emitted `spec` is required but NULLABLE.
- **`expires` emitted as `format: date-time` alone.** `Expiry` is a before-validator over a
  `datetime`, and pydantic emits the type it PRODUCES rather than the ones it accepts, so
  `expires: 2026-01-01` (which the validator takes) would have failed in an editor that asserts
  formats. Fixed by naming the accepted input on the validator itself
  (`json_schema_input_type=str | date | datetime`), which is pydantic's own hook for the
  distinction.

### 2.2 Two more the review found, both about NULL

Both slipped past section 2.1's test because no bundled sample and no migrator output writes an
explicit `null`, and both are things a hand-written manifest does as a matter of course.

- **The capability splice dropped the row's null arm.** `_spec_model` re-annotated the naming field
  with the bare union while keeping the row's `FieldInfo`, so `session-template`'s
  `harness_integration: CapabilityBlock | None = None` lost its `| None` and kept `default: null`.
  Two failures in one: `harness_integration: null` loads and was refused, and the property
  advertised a default its own subschema rejects, so an editor's insert-default produced config the
  same schema flagged. The splice now replaces the field's MODEL and nothing else about it.
- **The owner-template correction implemented half its own rule.** `_fill_owner_templated_defaults`
  treats an omission and an explicit `null` alike, deliberately and in its own docstring, but the
  schema hook only stripped the field from `required`. All five templated fields emitted
  non-nullable, so `token: null` and azure's `secret: null` were refused while loading. The hook now
  widens them, with the describing keywords hoisted outside the `anyOf` so hover text survives.

The second one exposed something older than this step: a marker rides the branch its `Annotated`
sits on, so `Annotated[str, SecretRef(...)] | None` has ALWAYS nested `x-agw-ref` one level down
(pydantic's doing, with no hook of ours involved). The round-trip guards read the property's top
level only, so they would have reported "no reference here" for a field that declares one.
`tests/_emitted_schema.py` is now the single reader and searches the subtree, which makes the guards
cover both shapes for a reason unrelated to the widening.

**Corrected 2026-08-07, from the roadmap-lead review.** The paragraph above accepted the burial and
taught the readers to work around it, which left the promise on `AgwModel` ("the field keeps its
`x-agw-ref`, so a hover still shows what the omission resolves to") false in every emitted schema
that mattered: all five templated secrets, plus every natively optional marked field. A widened
property is not indistinguishable from a declared one if neither can be hovered. `AgwModel`'s hook
now lifts the marker onto the property, for every marked field rather than the templated ones alone,
because pydantic's native burial and our own widening produce the same shape and only one of them
was ever this step's doing. The subtree search stays and is still needed, for the case where the
marker genuinely belongs one level down: a COLLECTION's element marker rides `items`, where it
describes what it sits on, and lifting that onto the field would claim the list names a Resource.

The lift is pinned on both sides of the fixture line. `tests/schema/test_markers.py` states three
facts (the property is branchy, the marker is on it, no branch kept a copy) against fixture models
covering both burial shapes;
`tests/manifests/test_emit.py::test_the_shipped_token_field_states_its_reference_on_the_property`
states the same three against the real emitted `git-credential` schema, so the shipped artifact is
its own tripwire rather than a fixture standing in for it. The second pin is needed because the
round-trip guard beside it cannot catch this regression:
`test_reference_markers_reach_emitted_schema` reads through the subtree search, so a marker that
fell back into its `anyOf` branch still satisfies it. Verified by disabling the lift, which reddens
the new pin and leaves that one green.

### 2.3 The third round is not an emission bug: the two parsers read different YAML

**Added 2026-08-06, from the greenfield closeout verification.** Sections 2.1 and 2.2 are both bugs
in what emission WROTE. This one is not: every schema involved describes its model correctly. The
divergence is that a JSON Schema never sees YAML, it sees whatever a parser already turned the YAML
into, and the loader's parser is not the editor's parser.

Measured, both sides, rather than read off the specs (pyyaml 6.0.3, `yaml` npm 2.9.0):

| source text           | loader (pyyaml `SafeLoader`, YAML 1.1) | editor (`yaml` npm, YAML 1.2 core) |
| --------------------- | -------------------------------------- | ---------------------------------- |
| `verify_ssl: no`      | `False`                                | the string `"no"`                  |
| `expires: 2027-01-01` | `datetime.date(2027, 1, 1)`            | the string `"2027-01-01"`          |
| `memory: 8_192`       | `8192`                                 | the string `"8_192"`               |
| `memory: 1:30`        | `90`                                   | the string `"1:30"`                |
| `memory: 010`         | `8`                                    | `10`                               |
| `memory: 0o17`        | the string `"0o17"`                    | `15`                               |

yaml-language-server parses with the `yaml` package at its defaults, verified to be `version: 1.2`,
`schema: core`. So there is no single YAML version at which the emitted schema agrees with the
loader on every loader-valid input, and the three halves are decided differently because they are
not the same problem.

**What this table is and is not.** It is not a list of every scalar type; it is every type where the
two parsers disagree ON THE MANIFEST SURFACE, which is booleans, timestamps and integers. That set
is derived, not surveyed: the surface's scalar types are `str`, `bool`, `int` and the `date` behind
`expires`, and strings cannot disagree because a plain scalar that resolves to nothing else is a
string under both versions. Floats would be a fourth and are not reachable, because no emitted field
is one; `test_the_float_gap_is_still_unreachable` fails the day that changes. The first two rounds
of this section each found one member of this set after declaring the previous list complete, so the
claim here is stated with its derivation attached rather than as a count.

**Booleans: fixed, by widening.** `verify_ssl: no` is ordinary YAML the loader reads as `False`, and
a `"type": "boolean"` made the real yaml-language-server answer `Incorrect type. Expected "boolean"`
on it. That is over-reporting, the one direction section 2 forbids. Every emitted boolean is now
`{"type": ["boolean", "string"], "pattern": ...}` over the twelve spellings YAML 1.1 resolves to a
boolean and 1.2 core does not (`yes`/`no`/`on`/`off`, three casings each). Three choices inside
that:

- **In `bool_schema` on a `GenerateJsonSchema` subclass**, not a post-walk of the emitted dict, so a
  boolean added later anywhere (new kind, plugin capability config, nested block) is covered without
  anyone remembering. Consistent with section 2.1's rule that a fix belongs at the layer owning the
  fact; here the fact is "what a YAML boolean can look like", which is emission's alone.
- **`pattern`, not `enum`.** JSON Schema applies `pattern` only to string instances, so one flat
  schema covers both types with no nested `anyOf`; and an editor draws completions from `enum` but
  not from `pattern`, so a boolean field still completes to `true` / `false` rather than to twelve
  odd ways of saying them.
- **The spelling list is derived**, from pyyaml's own `SafeConstructor.bool_values` through a real
  load, so a pyyaml change moves it instead of leaving it quietly short.

The residual, and it is deliberate: the widened schema also accepts the QUOTED `"no"`, which the
loader refuses. A schema sees the parsed instance and cannot tell `no` from `"no"` under 1.2, so
this is not fixable, only assignable to a direction. It is under-reporting, which section 2 permits.
The nine field docstrings that warn about quoted booleans carry the weight instead, and they were
corrected in the same pass (they claimed bare `no` was a string, which is true of TOML and false of
this loader).

**Integers: fixed the same way, one round later.** `memory: 8_192` is how an operator writes eight
thousand of something in exactly this field, and the loader reads `8192` where a 1.2 editor holds
the string `"8_192"`. Same over-reporting, same direction, so the same correction: an `int_schema`
override on the same `_ManifestJsonSchema` subclass, emitting
`{"type": ["integer", "string"], "pattern": ...}`.

Three things differ from the boolean case, and each is a decision rather than a detail:

- **A pattern, not a spelling list.** The boolean disagreement is twelve words; this one is
  infinite, because underscores and sexagesimal groups repeat. So `YAML_11_ONLY_INTEGERS` is a
  regex, derived rather than written: pyyaml's own `tag:yaml.org,2002:int` implicit resolver,
  stripped of its `re.VERBOSE` layout, minus the language a 1.2 core parser resolves without help.
  `test_the_widened_integer_pattern_is_pyyamls_own_language_minus_yaml_12` rebuilds it from those
  two live halves, and checks the strip by behavior rather than by comparing text.
- **The generator's own answer is widened, not replaced.** A boolean carries no constraints, so
  `bool_schema` can return a literal. Integers carry `exclusiveMinimum` (`cpus`, `memory`) and
  `examples` (`template_vmid`), which have to survive. Those numeric keywords then apply to the
  integer arm alone, since JSON Schema's numeric keywords ignore string instances, so `cpus: 0_0`
  passes the schema and the loader refuses it. That is this module's under-approximation working as
  designed; restating every bound as a regex would be a second place to be wrong about `cpus`.
- **The subtraction earns its keep here in a way it did not for booleans.** Under 1.2 a bare `no`
  and a quoted `"no"` are the same instance, so excluding `true`/`false` from the boolean list
  changes nothing an editor could act on. Integers are different: bare `5` reaches the editor as a
  NUMBER and only the quoted `"5"` arrives as a string, so admitting plain decimals into the pattern
  would have thrown away a real diagnostic against the strict loader. It is excluded, and `"5"` is
  still flagged.

The residual is the boolean one exactly, and no larger: a quoted `"8_192"` is the same parsed
instance as a bare one, so no schema separates them. Under-reporting, which section 2 permits.

Measured over a corpus run through both parsers, the disagreement sorts into three classes and only
the first is a type error the schema can answer:

| class                     | members                                                                              | schema                |
| ------------------------- | ------------------------------------------------------------------------------------ | --------------------- |
| loader int, editor string | underscores (`8_192`), sexagesimal (`1:30`), binary (`0b1010`), signed hex (`+0x1F`) | fixed by the pattern  |
| both int, different value | leading-zero octal (`010` is 8 and 10, `0777` is 511 and 777)                        | unreachable           |
| loader string, editor int | `0o17`, `1e3`                                                                        | silent, and permitted |

**`010` is not fixable, and that is the honest answer rather than a deferral.** Both parsers hand
the validator a conforming integer; they differ only in which one. A JSON Schema constrains the
instance it is given and has no access to the source text, so there is no keyword that could see the
difference, and widening the type does not help because the type was never wrong. It is pinned in
`test_a_leading_zero_integer_is_a_value_disagreement_no_schema_can_reach` so it stays a known shape,
and it is the reason `_EditorLoader` had to override pyyaml's int CONSTRUCTOR as well as its
resolver: left alone the harness reports 8, which is the loader's answer, and the case disappears.

The third class inverts: the editor resolves a number that the loader leaves a string and the strict
models refuse, so the operator gets a load error the editor did not warn about. Under-reporting,
which section 2 permits, and it is recorded here rather than left for a fourth round to rediscover.

**Dates: not fixable in the schema, and not a defect against the documented editor.** `expires`
emits string-typed arms, so a validator handed a `datetime.date` would reject it. No arm can fix
that: JSON Schema's type system is JSON's, and a date is not a JSON type. Verified by execution
against the reference implementation, feeding a `datetime.date` to each candidate:

```text
{"type": "string"}                         -> REJECTED (not of type 'string')
{"type": "string", "format": "date"}       -> REJECTED (not of type 'string')
{"anyOf": [all six JSON types]}            -> REJECTED (not valid under any of the given schemas)
{}                                         -> ACCEPTED (an empty schema, which constrains nothing)
```

Only the empty schema takes one, and an empty schema is not a description. So "widen the arms" was
never on the table for dates, and a future round should not re-open it. It costs nothing today
because the editors these schemas are written for are 1.2, where a bare date is a string and
validates clean: checked against the real yaml-language-server, `expires: 2027-01-01` produces no
diagnostic. The exposure is confined to a hypothetical YAML 1.1 validator, and is recorded in the
guide under the declared target rather than worked around.

**`tests/manifests/test_emit.py`'s `_EditorLoader` is where this class of bug hides.** Twice now. It
modelled the timestamp difference while leaving booleans resolving as pyyaml does, so no document
the harness held exercised that disagreement; the boolean round fixed that and left integers
resolving as pyyaml does, so the same thing happened again one type over. Every rendered sample
spells its booleans `true` / `false` and its integers as plain decimals, which is why following the
samples never lands an operator here and why the harness's own blind spots decide what the tests can
see.

The harness now models all three, and its integer fidelity is checked against the real `yaml` npm
package over the corpus above rather than against a reading of the spec: it agrees on every member
except `1e3`, which is the float it does not model and is inert while no emitted field is one. The
lesson worth carrying past this SDD is that this file's coverage is bounded by `_EditorLoader`, so a
claim about what an editor sees is only as good as what the harness is willing to produce. Widening
the schema without widening the harness leaves a green suite that proves nothing about the case in
hand.

## 3. The per-kind document schema

### 3.1 It is a DOCUMENT schema, not a spec schema

The unit is the whole manifest document (`apiVersion` / `kind` / `metadata` / `spec`), not the
kind's `spec` mapping alone. That is forced by the consumer: a yaml-language-server `$schema`
modeline associates a schema with a FILE, and the file holds documents. A spec-only schema could not
be pointed at from anything.

### 3.2 The document envelope IS modeled, and 2.5's deferral is discharged here

The kind-spec-models LLD (section 3.2) left the document envelope unmodeled and named this step as
the one that would decide. The call: **modeled, for emission only; `manifests/envelope.py` keeps its
hand-rolled runtime validation unchanged.**

Both halves matter.

- Modeled, because a document schema has to say what `apiVersion` and `kind` and `metadata` are, and
  the alternative is emission hand-writing a JSON Schema fragment, which is a second authority for
  the envelope's shape and exactly the drift FR13 forbids.
- Emission-only, because 2.5's reasons for keeping the runtime validator hand-rolled all still hold:
  its errors are the best in the codebase, and it must be able to NAME the kind before any kind
  model is in hand. Replacing it would trade good errors for uniformity nobody asked for.

Two authorities would still be two authorities if the emission model restated the envelope's facts.
It does not. Every fact comes from the place that already owns it:

| Fact                        | Owner it is read from                                            |
| --------------------------- | ---------------------------------------------------------------- |
| the `apiVersion` value      | `envelope.API_VERSION`                                           |
| which kinds may be declared | `KIND_REGISTRY`, filtered on `category == "declarable"`          |
| the top-level key set       | the emission model's own fields, pinned against `_ENVELOPE_KEYS` |
| the metadata key set        | `declared_resource.METADATA_FIELDS`                              |
| each metadata field's type  | THE KIND'S OWN ROW MODEL (section 3.3)                           |
| every `spec` field          | the kind's row model, which is its spec model                    |

`test_emit.py::test_the_document_schema_states_exactly_the_envelope_keys` asserts the third row
against `envelope._ENVELOPE_KEYS` directly, so a fifth envelope key cannot be accepted by the loader
and missing from the schema.

### 3.3 Metadata comes from the KIND's row, not from the shared base

`EnvelopeMetadata`'s three fields all carry `SkipJsonSchema`, which is what keeps them out of the
SPEC surface (step 2.5's ruling, and the reason `EnvelopeMetadata` exists at all rather than a bare
marker). They are exactly the fields the `metadata` block wants, so emission builds a metadata model
per kind from `row.model_fields[name] for name in METADATA_FIELDS`, with the `SkipJsonSchema` entry
dropped from each field's metadata list.

Reading them off the KIND's row rather than off `EnvelopeMetadata` is not incidental: a kind may
re-declare one. `secret` makes `description` required and `admin-template` defaults `name` to
`"default"`, and both differences show up in the emitted metadata block for free. Verified by
execution:

```text
SecretDeclMetadata      -> required: ["description", "name"]
AdminConfigMetadata     -> name: {"default": "default"}
```

`metadata.name` additionally carries `maxLength` when the row declares `NAME_MAX_LENGTH` (`secret`,
`vm-site`). That cap is applied by `decode._check_declared_name` to exactly the names a manifest
carries (operator-written ones), so it is faithful, and it is one derived integer.

The character rule is deliberately NOT emitted as a `pattern`. `validate_name` is a Python regex
plus a separate consecutive-hyphen check; reproducing that pair as one ECMA-262 pattern would be a
hand-written second authority for a rule whose violation is already a clean load error, and getting
it slightly wrong red-underlines valid names. Section 2's rule decides it.

### 3.4 The capability union is spliced by SUBCLASSING, not by dict surgery

A hosting kind's row carries the capability as a `CapabilityBlock`: `name` plus `extra="allow"`,
because the extra keys belong to another owner and are checked at finalize against that owner's
model. That is right for the row and useless as a schema, which would say only "some table with a
name".

So emission replaces it. The mechanism is a per-kind pydantic subclass of the row whose naming field
is re-annotated to the assembled union:

```python
create_model(f"{...}Spec", __base__=row, **{naming_field: (capability_config_union(kind), field)})
```

reusing the row's own `FieldInfo` so the authored description survives onto the spliced property.

Chosen over merging two `model_json_schema` outputs and rewriting `$ref` strings. The dict-surgery
version has to own `$defs` collision handling itself (two calls can mint the same key for different
classes, and pydantic only disambiguates WITHIN one call), and a mis-merge there is a dangling
`$ref` or, worse, a `$ref` resolving to the wrong model: a silent wrong answer. Subclassing means
the entire document is ONE `model_json_schema` call, so pydantic owns `$defs` naming, collisions,
and reference integrity, and there is no merge to get wrong.

Which kinds splice comes from the descriptor table (`manifest_section.host_kind` selects the kind,
`manifest_section.naming_field` selects the field), so nothing here enumerates hosts.

### 3.5 The one-arm union, which no host sees today

**Corrected 2026-08-06, after review.** This section originally called the one-arm union "the
shipped case for two kinds of three", and that was wrong. Measured live registries are vm-platform
5, harness-integration 3, git-credential-provider 2, and `plugins/__init__.py` registers every
shipped plugin's implementations unconditionally at import, so enablement never removes an arm. **No
host sees a one-arm union.** The claim came from counting in-tree implementations and forgetting
that the bundled plugins are in-tree too.

The mechanism below is unchanged and stays, because what it guards against is real and cheap:
`Union[(X,)]` collapses to `X`, so a capability kind that ever has a single registered
implementation has no union left in its annotation, and a classifier keyed on "is this still a
union" would silently drop the discriminator for it. A plugin author's out-of-tree kind, or the
removal of a shipped implementation, gets there.

Emission therefore classifies on **discriminator presence**
(`descriptor.config_schema.discriminator is not None`), never on whether the annotation is still a
union. Verified by execution: pydantic keeps the tagged-union core schema through the collapse and
still emits `oneOf` plus `discriminator`, so a one-arm kind emits

```json
{
  "discriminator": { "mapping": { "shell": "#/$defs/ShellConfig" }, "propertyName": "name" },
  "oneOf": [{ "$ref": "#/$defs/ShellConfig" }]
}
```

which is the shape a second implementation grows into with no change of form. Pinned by
`test_emit.py::test_a_one_arm_union_still_carries_its_discriminator`, which seats a single fixture
capability and sets the shipped ones aside for the duration. That construction is now the only way
the case is reachable, which is exactly why the pin is written that way rather than leaning on a
kind that happens to be one-arm.

## 4. The envelope schema

`manifest.schema.json` is the schema for a manifest document of ANY kind: a `oneOf` over the
per-kind document schemas with a `discriminator` on `kind`. It is assembled the same way everything
else is, as a real pydantic discriminated union over the per-kind document models (each of which
pins `kind: Literal[...]`), so pydantic emits the `oneOf` and the `mapping` and this module writes
neither.

It is SELF-CONTAINED rather than a set of `$ref`s to the sibling files. Cross-file references would
make each file useless on its own and make the modeline's correctness depend on relative-path
resolution in whatever editor is reading it. The cost is duplication across files: as written to
disk, 99 KB for the any-kind file against 3 to 17 KB per kind (measured 2026-08-06; an earlier
"about 43 KB" here was compact JSON from a prototype, not the indented text that lands). That is the
right trade for generated artifacts nobody hand-edits.

`discriminator` is an OpenAPI keyword, not a JSON Schema one, so an editor may ignore it. That
degrades error QUALITY (a "matched none of the branches" report rather than a located one), never
correctness, because every arm pins `kind` with a `const` and so at most one arm can match. The
per-kind schemas exist partly for this reason: a file holding one kind gets pointed at its own
schema and never meets the `oneOf` (section 6).

## 5. What emission deliberately does not express

Recorded rather than left for a reader to notice, per section 2's rule.

- **The secret-backend map-keyed splice.** The HLA (Component 3) says `backend_mappings` "expresses
  it as per-key properties". It does not. `SecretDecl` declares
  `dict[str, str | dict | Literal[False]]`, and that is what emits: sound, under-constrained, no
  false diagnostics.

  **The blocker, and it is a real one.** The descriptor table has no record of WHERE a map-keyed
  capability is hosted. `manifest_section` is `HostSurface | None` and secret-backend's is `None`,
  so emission would have to hard-code `secret` / `backend_mappings`, which is precisely the
  switchboard the descriptor exists to have killed. Nor does `propertyNames` over the backend
  registry escape it: that constrains the KEYS without reaching each key's own model, which is the
  whole value. Doing it properly means a new descriptor record, which is a contract change and an
  owner's decision rather than something to smuggle into an emission step.

  **Corrected 2026-08-06, after review: the cost is queued, not hypothetical.** This entry said
  "both shipped backends" and named 1Password as the future trigger. Three backends are registered
  (`env-var`, `prompt`, `onepassword`), and `onepassword` already ships in-tree with a fully modeled
  mapping (`OnePasswordMapping = OpUri | OnePasswordAccountRef`, with `op://` validated). The
  trigger fired before the deferral was written. So an operator writing
  `backend_mappings.onepassword` today gets no completion on `account` or `reference`, no `op://`
  check, and no key checking, all off a model the descriptor could already reach. The remaining two
  are genuinely cheap to skip: `env-var`'s mapping is a bare string the current schema already
  expresses exactly, and `prompt`'s is a validator-only refusal JSON Schema cannot state at all.

  This is now a known missing feature with a live cost, not a deferral waiting on a trigger. The
  roadmap lead owns when it lands.

- **A required capability field an inheriting kind's PARENT supplies.** `session-template` composes
  along an `inherits` chain and `SessionTemplate.validate_config` validates the MERGED harness blob,
  because a child's declaration is legitimately partial until the chain completes it (FR12). JSON
  Schema has no view of that chain, so it checks the child's fragment against the arm model
  directly. A child that inherits a required field therefore loads and does not validate, which is
  the FORBIDDEN direction rather than the safe one.

  Exposure today is nil: no registered harness arm requires a field beyond its own tag.
  `tests/manifests/test_inherited_capability_config.py` forces the divergence with a fixture arm (so
  it rests on observed behavior, not on reasoning) and carries the tripwire that fires the day an
  arm gains one.

  Not fixed here, deliberately. The only structural fix is to relax `required` on the arms of an
  inheriting kind's capability block, which buys soundness for inheriting templates by removing a
  real missing-field diagnostic from standalone ones, and there is no evidence yet about which
  matters more. Raised for a decision rather than taken.

- **`_NO_SELECTOR_KINDS`.** `named-console-template` accepts only `name: default` at the envelope.
  Expressible as a `const`, and left out: the rule is transitional (it leaves when the kind's
  selector ships, issue #165), and wiring emission into an envelope-private constant to express a
  rule that is going away buys an editor diagnostic the loader already gives clearly.
- **Everything in section 2's validator list**, for the reason section 2 gives.

## 6. The surfaces

### 6.1 `agw resource schema`

```text
agw resource schema             # the any-kind document schema, to stdout
agw resource schema KIND        # that kind's document schema, to stdout
agw resource schema --write     # the whole set, into <resources>/.schema/
```

A bare invocation prints the envelope schema rather than erroring the way `resource sample` does.
The shapes differ because the answers do: a bare `sample` would be a wall of thirteen documents with
no single right one, while a bare `schema` has exactly one obvious answer, and it is the file the
modeline points at most often.

`--write` is a bare flag, not `sample --write`'s filename, because the destination is not the
operator's to choose: the modeline hard-codes the relative path, so a schema written somewhere else
would be a schema nothing references. It writes the SET (envelope plus every declarable kind), never
a subset, for the same reason: a partial set leaves some manifest's modeline dangling. `KIND` with
`--write` is refused rather than silently writing everything.

Files land in `<resources>/.schema/`: beside the manifests so the modeline's relative path is short
and the whole directory stays portable and committable, and dot-prefixed because
`loader._iter_manifest_files` prunes dot-directories, so generated artifacts cannot become
manifests.

### 6.2 The modeline, and who stamps it

```yaml
# yaml-language-server: $schema=.schema/vm-template.schema.json
```

Stamped as the FIRST line of a manifest file, by both writers the plan names:

- `agw resource sample --write`
- `agw resource migrate`, on the files it CREATES

**Only on creation, in both cases.** A modeline has to be at the top of the file, so stamping an
existing file means inserting at line 1 and shifting every line number an operator (and every stored
`declared_at`) already knows. That is a bigger change to a file than either command was asked to
make. An operator who wants the header on an existing manifest adds one line; `agw resource schema`
prints the path it should point at.

Both writers ensure the schema SET exists (`write_schema_set`), because a modeline pointing at a
file that does not exist is a red error banner in the operator's editor: strictly worse than no
modeline. So `sample --write` and `migrate` both write `.schema/` as a visible, reported side
effect. The migrator writes it AFTER verification passes, outside its transactional block: it is an
idempotent derived artifact, so making rollback responsible for it would add a failure mode to the
recovery path in exchange for nothing.

The migrator's header is a `FileWrite` PROPERTY rather than stored text, derived from the kinds the
file ends up holding, and `created_yaml_text` is the one spelling of "the whole text of a file this
run creates", shared by the dry run and the write. That pairing is what makes `--dry-run --full`
show the bytes that land, header included, which is the same discipline `appended_yaml_text` already
established for the append path.

The referenced schema is the PER-KIND one when the file holds exactly one kind, and the envelope
otherwise. That is the migrator's default (`--layout per-kind`) and its per-resource layout, so the
common case gets the schema with the better diagnostics (section 4).

**Known interaction, for 2.8.** `manifests/samples.py`'s fully-commented contract says an operator
uncomments by stripping one leading `#` per line, and the test suite does that mechanically. The
modeline is a REAL comment (one `#`), so a blanket strip would leave `yaml-language-server: ...` as
a broken top-level key. It is stamped by `write_sample` as a file header and is not part of
`sample_text`, so the mechanical test (which runs over `sample_text`) is unaffected and the shipped
guidance ("uncomment the document lines") stays true. 2.8 owns the sample body and the uncomment
contract; if it makes the body genuinely uncommentable-in-bulk, it should settle the header's
spelling then.

## 7. The dev-only validator dependency: taken

`jsonschema` (4.26.0, the latest stable at implementation time) is added to the `dev` dependency
group. It is imported by tests only; nothing under `agentworks/` imports it and no shipped
dependency changes.

Taken, rather than hand-rolling structural assertions, because of what the alternative actually
proves. Assertions written by the author of the emitter encode the author's beliefs about JSON
Schema, so they pass in exactly the cases where those beliefs are wrong. The failure this step must
not ship is a schema that is subtly wrong, since an editor will confidently red-underline valid
config, and a reference implementation is the only independent oracle available for that. Two checks
depend on it and could not be written honestly without it:

- `Draft202012Validator.check_schema` meta-validates every emitted document against the 2020-12
  metaschema, which catches a keyword misused or misspelled anywhere in the tree;
- `iter_errors` over real documents is the automated half of box 2's end-to-end check, and it is
  what found every soundness bug this step had.

**Corrected 2026-08-06, after review: `check_schema` does NOT catch a dangling `$ref`.** This
section claimed it did, and the test that asserted it said so too. Proven false in one line:
`check_schema({"$ref": "#/$defs/nope"})` raises nothing, because reference resolution is a
validation concern, not a meta-validation one. Only `iter_errors` catches a dangling pointer, and
only on a branch some document happens to exercise, which for a `$defs` graph this size means most
of it is never visited.

So the walk is now written rather than assumed: `test_every_ref_is_local_and_resolves` collects
every `$ref` in every emitted file and asserts each is local and present. Twelve lines, and a real
guard for a graph nobody writes by hand. The emitted set was clean throughout; the gap was in what
the tests proved, not in what they were proving it about.

Cost: six wheels in the dev environment (`jsonschema` plus `attrs`, `jsonschema-specifications`,
`referencing`, `rpds-py`, `typing-extensions`), plus `types-jsonschema` for strict mypy; no network
at test time (the metaschemas are vendored in `jsonschema-specifications`), and nothing in the wheel
we ship.

It paid for itself twice over: the three soundness bugs in section 2.1 during implementation, and
two more (section 2.2) that review found with the same tool.

## 8. Hand-off to 2.8

- `manifests/emit.py` is emission's whole surface. The renderer is a separate derivation over
  `iter_field_docs`; neither imports the other, and that is the HLA's sibling-derivation rule, not
  an accident of ordering.
- `write_sample` gains the modeline header only. The sample BODY (`sample_text`) is untouched, so
  2.8's rewrite over the live renderer inherits a two-line seam: the header, and the
  header-versus-uncomment note in section 6.2.
- `agw resource schema`'s NAME is settled here only as far as this step needed it; the plan puts the
  `schema` / `describe` surface naming in 2.8, coordinated with the onboarding child SDD. If that
  coordination renames the command, this step's completions entry and the two doc pointers move with
  it.
- The map-keyed splice (section 5) is the one piece of Component 6 not built, and it is a known
  missing feature with a live cost rather than a deferral waiting on a trigger. The roadmap lead
  owns when it lands.
- Two rules the renderer inherits, because they are properties of the models rather than of
  emission. A field with an owner-templated default is NOT the operator's to write and accepts an
  explicit `null` meaning the same thing (sections 2.1 and 2.2), so a rendered sample should show it
  commented with its resolved-name template rather than as a required line; and `expires` accepts
  three spellings, not just the RFC 3339 one. Both facts are on the models and both reach
  `iter_field_docs` already (`FieldDoc.default_template`, and the annotation), so this is a
  presentation choice for 2.8, not new plumbing.
- `tests/_emitted_schema.py` is the one reader of an emitted property. Any surface that grows a
  consumer of `x-agw-ref` should go through it (or promote it beside the marker that writes it)
  rather than indexing the property's top level, which is right only for the fields that happen not
  to be nullable.
