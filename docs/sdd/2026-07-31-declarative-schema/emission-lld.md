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

`tests/manifests/test_emit.py::test_emitted_schemas_accept_every_bundled_sample_document` pins the
direction that matters: every document the real loader accepts also validates against the emitted
schema. Nothing pins the other direction, because the other direction is false by design.

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

`test_emit.py::test_document_schema_top_level_keys_match_the_envelope` asserts the third row against
`envelope._ENVELOPE_KEYS` directly, so a fifth envelope key cannot be accepted by the loader and
missing from the schema.

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

### 3.5 The one-arm union, which is the shipped case for two kinds of three

`Union[(X,)]` collapses to `X`, so a capability kind with a single registered implementation has no
union left in its annotation. That is not an edge case here: `harness-integration` (shell) and
`git-credential-provider` (github) both have exactly one in-tree implementation, and a host with no
plugins enabled sees them that way.

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
`test_emit.py::test_a_one_arm_union_still_carries_its_discriminator`, which builds the emission over
a registry seated with a single fixture capability rather than relying on the two in-tree kinds that
happen to be one-arm today.

## 4. The envelope schema

`manifest.schema.json` is the schema for a manifest document of ANY kind: a `oneOf` over the
per-kind document schemas with a `discriminator` on `kind`. It is assembled the same way everything
else is, as a real pydantic discriminated union over the per-kind document models (each of which
pins `kind: Literal[...]`), so pydantic emits the `oneOf` and the `mapping` and this module writes
neither.

It is SELF-CONTAINED rather than a set of `$ref`s to the sibling files. Cross-file references would
make each file useless on its own and make the modeline's correctness depend on relative-path
resolution in whatever editor is reading it. The cost is duplication across files (about 43 KB for
the whole-set file against roughly 2 to 10 KB per kind), which is the right trade for generated
artifacts nobody hand-edits.

`discriminator` is an OpenAPI keyword, not a JSON Schema one, so an editor may ignore it. That
degrades error QUALITY (a "matched none of the branches" report rather than a located one), never
correctness, because every arm pins `kind` with a `const` and so at most one arm can match. The
per-kind schemas exist partly for this reason: a file holding one kind gets pointed at its own
schema and never meets the `oneOf` (section 6).

## 5. What emission deliberately does not express

Recorded rather than left for a reader to notice, per section 2's rule.

- **The secret-backend map-keyed splice.** The HLA (Component 3) says `backend_mappings` "expresses
  it as per-key properties". It does not, yet. `SecretDecl` declares
  `dict[str, str | dict | Literal[False]]`, and that is what emits: sound, under-constrained, no
  false diagnostics. Two things block the splice, and the first is the real one:
  - The descriptor table has no record of WHERE a map-keyed capability is hosted. `manifest_section`
    is `HostSurface | None` and secret-backend's is `None`, so emission would have to hard-code
    `secret` / `backend_mappings`, which is precisely the switchboard the descriptor exists to have
    killed. Doing it properly means a new descriptor record, which is a contract change and belongs
    with an owner's decision, not smuggled into an emission step.
  - Both shipped backends make the splice worth little today: `env-var`'s mapping is a bare string
    (already fully expressed by the current schema) and `prompt`'s is a validator-only refusal that
    JSON Schema cannot state at all.
  - **Trigger:** the first secret backend whose mapping is a real table (1Password's
    `{account, reference}` is the named candidate). At that point the per-key properties carry
    something an editor can complete, and the descriptor record pays for itself.
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

Both writers ensure the schema SET exists before stamping (`write_schema_set`), because a modeline
pointing at a file that does not exist is a red error banner in the operator's editor: strictly
worse than no modeline. So `sample --write` and `migrate` both write `.schema/` as a visible,
reported side effect.

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
  metaschema, which is what catches a malformed `$defs` graph or a dangling `$ref`;
- `iter_errors` over real sample documents is the automated half of box 2's end-to-end check.

Cost: six wheels in the dev environment (`jsonschema` plus `attrs`, `jsonschema-specifications`,
`referencing`, `rpds-py`, `typing-extensions`), no network at test time (the metaschemas are
vendored in `jsonschema-specifications`), and nothing in the wheel we ship.

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
- The map-keyed splice (section 5) is the one piece of Component 6 not built. Its trigger is a
  backend, not a step.
