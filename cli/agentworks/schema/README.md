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
| D10 | Reference prose                 | Kind and capability `TopicProse`                   |
| D11 | Manifest envelope validation    | `manifests/envelope.py`                            |
| D12 | YAML 1.1 spelling support       | `manifests/emit.py` spelling tables                |
| D13 | Layer merge policy              | `merge.py` and model annotation metadata           |

The field tree, `explain`, and generated samples consume D3. Add a consumer of D3 when possible
instead of walking models again.

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
- **Merge policy comes from the model.** D13 reads the same nested annotations for core and
  capability config. Registration rejects a model whose declared input domain cannot honor its
  policy safely.

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

`StructuralUnion` is selector-free and strict. Its closed model arms must be distinguishable by
required and allowed operator-written keys. Registration rejects overlapping or open arms,
validation aliases, a coexisting discriminator, reference markers on the union holder, and any
shorthand-bearing arm that contains a reference marker. These refusals keep validation, schema,
filling, and extraction on the same selector.

Scalar shorthand remains valid for marker-free arms. Reference markers belong on fields inside an
arm. See the capability modeling tiers for when a structural union is appropriate.

## Schema-directed layer merging

Template inheritance and final instance layers use `merge_model()` over raw values. The model tree
is the only merge-policy authority. Merge strategy is code-owned metadata: it is not accepted from
YAML, inline JSON, desired state, or emitted JSON Schema.

The shape defaults are:

- object models and mappings merge by key, recursively applying the child schema on conflicts;
- lists append unequal items in stable order and deduplicate equal items; and
- scalars replace with the incoming value.

An object or list can instead replace its complete previous value, including with `{}` or `[]`:

```python
from typing import Annotated, ClassVar

from agentworks.schema import AgwModel, MergeStrategy


class AuthConfig(AgwModel):
    merge_strategy: ClassVar[MergeStrategy] = MergeStrategy.REPLACE


class ToolConfig(AgwModel):
    auth: AuthConfig
    extra_args: Annotated[list[str], MergeStrategy.REPLACE]
```

Policy precedence at any node is the containing field's `MergeStrategy` metadata, then a selected
mapping-shaped model's `merge_strategy`, then the shape default. A field override can therefore
merge a same-arm union value even when that arm model normally replaces. Mapping value annotations
also participate because the map key identifies each conflict:

```python
services: dict[str, Annotated[ServiceConfig, MergeStrategy.REPLACE]]
```

A containing `REPLACE` wins before union selection. Otherwise both values must select the same
discriminated or structural arm before recursive merging is allowed. Different or unreadable arms
replace the complete union value, even when the containing field says `MERGE`, so values from two
arms cannot form a hybrid. A conflict on an unknown key within a known object likewise replaces that
raw child instead of inventing a merge policy from its runtime shape.

Append-deduplicated list items are atomic. Equality is structural and concrete-type-sensitive over
the closed JSON carrier: exact `None`, `bool`, `int`, finite `float`, and `str`, plus nested lists
and exact-string-keyed dictionaries composed from those values. It never invokes an item's Python
equality implementation. Values outside that carrier, including cycles and non-finite floats, are
unequal and remain available to final validation. There is no list-item identity or recursive item
merge contract today;
[ADR 0023](../../../docs/adrs/0023-declared-schemas-and-the-kind-descriptor.md) records the future
direction without specifying an API.

`merge_contract_error()` validates the static annotation contract. In particular:

- `MERGE` applies only to object-shaped nodes, and `APPEND_DEDUPE` only to lists;
- merged mappings require exact `str` keys, while `REPLACE` is the escape for other key types;
- an append-deduplicated list's element annotation must fit the closed comparison carrier;
- strategy metadata may annotate a field or mapping value, not a mapping key, list element, or
  individual union arm; and
- every reachable participating model refuses `validation_alias`, including below replacement
  boundaries. `serialization_alias` remains valid.

The merger does not validate, construct defaults, coerce, filter, or mutate its inputs. Wrong-shaped
values, `null`, unknown keys, and other malformed raw data survive to the existing final Pydantic
validation boundary. Whole-node replacement is also the safe policy when an author intentionally
uses a model domain broader than the recursive merge contract can interpret.

## Adding or changing a derivation

Name the existing surfaces the derivation must agree with and add an independent comparison in the
same change. Prefer pydantic as the oracle for accepted input and emitted-schema metadata as the
oracle for graph expectations; do not copy the implementation's own literals into its test.

Keep fixtures honest about distinctions operators can see. Patch `Path.home()` when testing
home-relative rendering, and seat plugin implementations before asserting registry membership.
Re-run pasted command output instead of editing it by hand. Outside upgrade and migration material,
write prose about the current contract, not the change that produced it.
