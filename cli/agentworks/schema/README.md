# The model layer, and everything derived from it

One authored source: the pydantic model graph. `AgwModel` and `AgwRootModel` subclasses, their
`RefMarker` annotations, their `scalar_shorthand` declarations, their docstrings, and their `Field`
metadata. Every other statement about a resource's shape in this codebase is DERIVED from that
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

|     | Derivation                                              | Where                                                                                                                         | What it walks with                                                                          |
| --- | ------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| D1  | Loader validation                                       | `manifests/decode.py`, `manifests/envelope.py`, `capabilities/config.py`                                                      | pydantic, plus the two before-validators in `base.py` (shorthand fold, owner-template fill) |
| D2  | Emitted JSON Schema                                     | `manifests/emit.py`, `AgwModel.__get_pydantic_json_schema__`, `RefMarker.__get_pydantic_json_schema__`, `_ManifestJsonSchema` | `model_json_schema` plus three correction layers                                            |
| D3  | Field-documentation stream                              | `fields.py::iter_field_docs`                                                                                                  | the classifier (`_shape.py::shape_of`, `accepted_annotation`)                               |
| D4  | Reference extraction, which builds the dependency graph | `extract.py::extract_references`                                                                                              | the same classifier, over RAW blobs, before validation                                      |
| D5  | Owner-template default fill                             | `base.py::_fill_owner_templated_defaults`                                                                                     | must agree with D4 on absent-versus-null; both call `marker.render_default`                 |
| D6  | The error bridge                                        | `errors.py::_resolve_path`                                                                                                    | re-walks the model beside pydantic's `loc`                                                  |
| D7  | Registration conformance                                | `base.py::reference_marker_error`, `model_is_complete`, `shorthand_field_error`                                               | the guard derivation: its job is making the other walkers' blind spots loud                 |
| D8  | Readiness probes over raw config                        | `capabilities/vm_platform/lima.py::not_ready` and kin                                                                         | a hand-rolled read, deliberately non-constructing                                           |
| D9  | Retired-shape advice                                    | `capabilities/retired_shapes.py`, decode's sibling-shape refusal                                                              | hand-authored knowledge of BOTH the old shape and the live model                            |
| D10 | Guides and READMEs                                      | `docs/guides/`, capability `prose` ClassVars                                                                                  | prose restating model facts, including pasted console output                                |
| D11 | The envelope's own document validation                  | `manifests/envelope.py`                                                                                                       | kept deliberately beside D2's `_document_model`; the shared fact is the top-level key set   |
| D12 | The YAML 1.1 spelling tables                            | `emit.py::YAML_11_ONLY_BOOLEANS` and `_INTEGERS`                                                                              | pyyaml's own resolver, restating what D1's parser accepts                                   |

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
- **D2 against D3** is guarded by `tests/manifests/test_accepted_type_parity.py`, whose expectation
  comes from pydantic rather than from our classifier, which is why it cannot agree with a wrong
  answer. Its limit is granularity: it compares JSON type sets, so two `object`-typed derivations
  can still disagree structurally under it.
- **D3 and the sample against D1** is guarded by uncommenting the real sample and loading it, for
  the FIRST arm of every union only, because a document holds one arm.
- **D5 against D4** is single-source by construction.
- **D9 against the live models** is pinned structurally, because pinning both sides as literals
  would let a rename leave the advice and its test stale together.
- **D10 against live output** cannot be compared by any automated check. Console blocks in guides
  are re-pasted from real runs, never hand-edited.

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
