# Model layer and derived surfaces

The pydantic model graph is the single authored source for resource shape. It includes `AgwModel`
and `AgwRootModel` subclasses, reference markers, scalar shorthands, field metadata, docstrings, and
explicit structural unions. Validation, JSON Schema, documentation, graph extraction, defaults, and
errors derive from that graph.

[ADR 0023](../../../docs/adrs/0023-declared-schemas-and-the-kind-descriptor.md) records the design
decision. [`../capabilities/README.md`](../capabilities/README.md) explains how capability authors
model config. This document covers the derivation layer.

## Derived surfaces

|     | Surface                         | Primary implementation                             |
| --- | ------------------------------- | -------------------------------------------------- |
| D1  | Loader validation               | pydantic via manifest decode and capability config |
| D2  | Emitted JSON Schema             | `manifests/emit.py` and model schema hooks         |
| D3  | Field documentation             | `fields.py::iter_field_docs` and `_shape.py`       |
| D4  | Resource-reference extraction   | `extract.py::extract_references`                   |
| D5  | Owner-template default fill     | `fill.py::filled_defaults`                         |
| D6  | Validation-error paths          | `errors.py::_resolve_path`                         |
| D7  | Registration conformance        | marker and structural-union checks                 |
| D8  | Readiness over raw config       | platform `not_ready` implementations               |
| D9  | Release-scoped migration advice | retired-shape declarations and manifest decode     |
| D10 | Guides and reference prose      | `docs/guides/` and capability `TopicProse`         |
| D11 | Manifest envelope validation    | `manifests/envelope.py`                            |
| D12 | YAML 1.1 spelling support       | `manifests/emit.py` spelling tables                |

The field tree, `describe-kind`, generated samples, and guide service records consume D3. Add a
consumer of D3 when possible instead of walking models again.

## Soundness requirements

- **Extraction equals validation for graph-affecting fields.** A document that validates with a
  resource reference must produce that edge. D7 refuses model shapes D4 cannot traverse.
- **Emitted schema never rejects loader-accepted input.** The schema may under-report a plain
  cross-field constraint, but it may not reject a valid manifest.
- **Documented types match accepted types.** D3 and D2 must expose the same scalar, collection, and
  table spellings.
- **Defaults are rendered once.** D5 fills owner-templated defaults before D1 and D4 inspect the
  blob.
- **Readiness reads declared shape.** Tests validate ordinary D8 inputs through the platform model.
  Explicit malformed-input tests may bypass validation to prove the probe remains total.
- **Migration advice follows the live model.** D9 tests derive the replacement structure from the
  current model instead of pinning both sides as independent literals.
- **Envelope and YAML corrections have independent checks.** D11 is compared with the emitted
  document schema. D12 is rebuilt from pyyaml's resolver in tests.

The main comparators are:

- `tests/schema/test_extract_completeness.py` and `test_extract_totality.py` for D1, D2, D4, and D7;
- `tests/capabilities/test_conformance.py` for unreachable reference shapes and structural unions;
- `tests/manifests/test_emit.py` for loader/schema and envelope/schema parity;
- `tests/manifests/test_accepted_type_parity.py` for D2/D3 type parity;
- `tests/schema/test_owner_templates.py` for D5;
- `tests/capabilities/test_declare_once_end_to_end.py` for D1 through D5 from one declaration.

D6 has no independent comparator against D3: its tests pin error paths as literals rather than
checking that field documentation can address them.

## Structural unions

`StructuralUnion` is selector-free. Its closed model arms must be distinguishable by required and
allowed operator-written keys. Registration rejects overlapping or open arms, validation aliases, a
coexisting discriminator, reference markers on the union holder, and any shorthand-bearing arm that
contains a reference marker. These refusals keep validation, schema, filling, and extraction on the
same selector.

Scalar shorthand remains valid for marker-free arms. Reference markers belong on fields inside an
arm. See the capability modeling tiers for when a structural union is appropriate.

## Adding or changing a derivation

Name the existing surfaces the derivation must agree with and add an independent comparison in the
same change. Prefer pydantic as the oracle for accepted input and emitted-schema metadata as the
oracle for graph expectations; do not copy the implementation's own literals into its test.

Keep fixtures honest about distinctions operators can see. Patch `Path.home()` when testing
home-relative rendering, and seat plugin implementations before asserting registry membership.
Re-run pasted command output instead of editing it by hand. Outside upgrade and migration material,
write prose about the current contract, not the change that produced it.
