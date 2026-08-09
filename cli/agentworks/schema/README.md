# The model layer, and everything derived from it

One authored source: the pydantic model graph. `AgwModel` and `AgwRootModel` subclasses, their
`RefMarker` annotations, their `scalar_shorthand` declarations, their docstrings, and their `Field`
metadata, including an explicit `StructuralUnion` where closed arms are selected by required and
allowed keys. Every other statement about a resource's shape in this codebase is DERIVED from that
graph, and none of them is allowed to be authored a second time.
[ADR 0023](../../../docs/adrs/0023-declared-schemas-and-the-kind-descriptor.md) records the
decision; [`../capabilities/README.md`](../capabilities/README.md) tells a capability author how to
write a model. This file is for the other reader: whoever is about to add, change, or debug a
DERIVATION.

**Read this before adding one.** The recurring defect in this layer, four times over, has been two
derivations of one fact disagreeing while nothing compared them. Adding a derivation is not adding a
consumer; it is adding N new pairs that have to agree.

## The derivations

Each of these walks the model graph itself, so each can be wrong on its own.

|     | Derivation                                              | Where                                                                                                                                | What it walks with                                                                        |
| --- | ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------- |
| D1  | Loader validation                                       | `manifests/decode.py`, `manifests/envelope.py`, `capabilities/config.py`                                                             | pydantic, plus scalar shorthand folding, over the D5-filled blob                          |
| D2  | Emitted JSON Schema                                     | `manifests/emit.py`, model and marker schema hooks, `_ManifestJsonSchema`                                                            | `model_json_schema` plus marker, shorthand, structural-union, and YAML corrections        |
| D3  | Field-documentation stream                              | `fields.py::iter_field_docs`                                                                                                         | the classifier (`_shape.py::shape_of`, `accepted_annotation`)                             |
| D4  | Reference extraction, which builds the dependency graph | `extract.py::extract_references`                                                                                                     | the same classifier, over RAW blobs, before validation                                    |
| D5  | Owner-template default fill                             | `fill.py::filled_defaults`, run at the boundaries (decode, the capability config core)                                               | the same classifier, rewriting the raw blob BEFORE D1 and D4 read it                      |
| D6  | The error bridge                                        | `errors.py::_resolve_path`                                                                                                           | re-walks the model beside pydantic's `loc`                                                |
| D7  | Registration conformance                                | `base.py::reference_marker_error`, `_shape.py::model_is_complete` and `structural_union_error`, run by `capabilities/conformance.py` | the guard derivation: its job is making the other walkers' blind spots loud               |
| D8  | Readiness probes over raw config                        | `capabilities/vm_platform/lima.py::not_ready` and kin                                                                                | a hand-rolled read, deliberately non-constructing                                         |
| D9  | Retired-shape advice                                    | `capabilities/retired_shapes.py`, decode's sibling-shape refusal                                                                     | hand-authored knowledge of BOTH the old shape and the live model                          |
| D10 | Guides and READMEs                                      | `docs/guides/`, capability `prose` ClassVars                                                                                         | prose restating model facts, including pasted console output                              |
| D11 | The envelope's own document validation                  | `manifests/envelope.py`                                                                                                              | kept deliberately beside D2's `_document_model`; the shared fact is the top-level key set |
| D12 | The YAML 1.1 spelling tables                            | `emit.py::YAML_11_ONLY_BOOLEANS` and `_INTEGERS`                                                                                     | pyyaml's own resolver, restating what D1's parser accepts                                 |

**Dependent derivations** consume D3 rather than the models, so they cannot drift from it
separately: the field tree (`manifests/field_tree.py`) and, through it, `describe-kind`, the
generated sample, and the service records `agw guide` consumes. That layering is a soundness
feature. Prefer adding a consumer of D3 over adding a thirteenth independent walker.

## What compares which

A pair that should agree and has no comparator is a hole whether or not it currently harbours a bug.

- **D4 against D1** is the highest-cost pair: a secret that validates but produces no edge is never
  gated, resolved, or reported. `tests/schema/test_extract_completeness.py` compares them with the
  validated object as oracle. That is fixture-scoped, so the real closure is D7 refusing the shapes
  D4 cannot reach.
- **D7 against D1 and D4** is that closure, made concrete by
  `tests/capabilities/test_conformance.py::test_a_marker_validation_accepts_and_no_walker_reads_is_refused`:
  it validates a blob, asserts D4 finds no edge in it, and asserts D7 refuses the model. The middle
  assertion is a premise check, so the test fails loudly rather than vacuously if D4 ever grows to
  reach the shape.
- **D7 against D1 and D2** also guards structural unions. `StructuralUnion` emits `oneOf`, so an
  open or overlapping arm declaration, or an arm with validation aliases, would let ordinary union
  validation accept a value whose raw keys the selector or schema reads differently.
  `structural_union_error` refuses that declaration at registration even when its arms carry no
  reference markers. Scalar shorthands remain valid for marker-free arms, while marker conformance
  refuses a shorthand-bearing arm with references because raw graph traversal selects structural
  arms only from table keys. Structural unions are selector-free, so the same guard refuses any
  coexisting discriminator rather than allowing tagged dispatch to override shape selection. The
  shipped-surface sweep and marker-reachability tests cover both halves.
- **D2 against D1** is guarded by
  `tests/manifests/test_emit.py::test_emitted_schemas_accept_every_document_the_full_load_path_accepts`,
  which runs every uncommented sample through the FULL load path (registry build included, since
  capability config is checked at finalize) and against the emitted schemas. Its sibling
  `test_a_capability_key_the_schema_rejects_is_rejected_on_every_host` is the rejects direction, and
  the rejects direction is the one the contract actually states.
- **D2 against D3** is guarded by `tests/manifests/test_accepted_type_parity.py`, whose expectation
  comes from pydantic rather than from our classifier, which is why it cannot agree with a wrong
  answer. Its limit is granularity: it compares JSON type sets, so two `object`-typed derivations
  can still disagree structurally under it.
- **D4 against D2** is guarded one-directionally by `tests/schema/test_extract_totality.py`: the
  kinds it expects an edge to carry are read off `x-agw-ref` in emitted schema, deliberately the
  OTHER derivation mechanism, so the oracle cannot inherit D4's own blind spots.
- **D3 and the sample against D1** is guarded by uncommenting the real sample and loading it, for
  the FIRST arm of every union only, because a document holds one arm.
- **D5 against D1 and D4** is structural: D5 is the only renderer of an owner-templated default, and
  D1 and D4 both read the blob D5 already rewrote, so there is no second rendering to drift. The
  pipeline (fill, then validate or extract) is what `tests/schema/test_owner_templates.py` and the
  completeness suite exercise.
- **D8 against D1** is guarded per-blob by `_readiness` in `tests/vms/test_platform_support.py`:
  every config handed to a real `not_ready` is validated through the platform's own model first. The
  exposure it closes is a RENAME, not an unguarded read. Change a tag and the production manifests
  move with the model because validation forces them to, while the hand-rolled read and a test's
  hand-spelled literal can both keep the old spelling and agree with each other. Validating the
  literal is what makes that combination impossible. The MALFORMED blobs are deliberately exempt:
  answering a config no model accepts is the totality `not_ready` exists for, so those call sites
  pass the blob raw and say so.
- **D9 against the live models** is pinned structurally, because pinning both sides as literals
  would let a rename leave the advice and its test stale together.
- **D11 against D2** is guarded by
  `tests/manifests/test_emit.py::test_the_document_schema_states_exactly_the_envelope_keys`, which
  reads the key set off the hand-rolled envelope validator itself and asserts the emitted document
  schema states exactly it, required and closed.
- **D12 against D1** is guarded in the same file, by rebuilding both spelling tables from pyyaml's
  live resolver: that resolver IS D1's parser, so neither table is maintained by hand on either
  side.
- **D1 through D5 over one declaration** is what
  `tests/capabilities/test_declare_once_end_to_end.py` does, on a fixture capability seated through
  the real plugin machinery. It is the only place the whole regime is read back off five derived
  surfaces from a single authored field.
- **D10 against live output** cannot be compared by any automated check. Console blocks in guides
  are re-pasted from real runs, never hand-edited.

One pair is an open hole. It is named here so the next person does not have to rediscover it. (D8
was the other, and it was closed by the same change that named it, per this file's own rule about
adding the comparator alongside the derivation.)

- **D6 against the models.** Every path `_resolve_path` renders is pinned as a hand-authored literal
  in `tests/schema/test_errors.py`, so nothing asserts that an error's address is one D3 would ever
  show an operator. D6 does share `_shape.py`'s classifier, which narrows the gap without closing
  it.

## Three oracles, and why no single guard covers the class

1. **Pydantic as the oracle for what is accepted.** Generalizes to any pair where both sides are
   code, and it is the strongest available move because the expectation does not come from us.
   Limited to type-set granularity.
2. **Validation as the oracle for extraction.** The right shape for the gating pair, but it cannot
   be made total dynamically, since its coverage is its blob list. The generalizable closure is
   structural instead: D7 refuses a marker inside any model that validation can construct and no
   walker can reach.
3. **Prose and advice have no code oracle.** They need per-pair comparators, and pasted output has
   to be re-run rather than reasoned about.

## If you are adding a derivation

State which existing derivations it must agree with, and add the comparator in the same change. If
you cannot build one, say so where the derivation lives and say what a disagreement would cost. A
derivation with no comparator is not finished; it is a defect nobody has tripped over yet.

Two traps that hid real defects here, both worth checking your guard against:

- **A fixture that collapses a distinction the operator can see.** `tmp_path` is never under
  `$HOME`, so a correct home-relative rendering and a hand-rolled absolute one are byte-identical in
  tests. Nine broken sites passed a five-thousand-test suite on that. Patch `Path.home()` if your
  guard touches paths.
- **A registry read that answers about the wrong registry.** Without seating, the capability
  registry is core-only, so a sparse-registry case a plugin would fill looks like the shipped state.
  Assert expected MEMBERS, not properties of whatever came back.
