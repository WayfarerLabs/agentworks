# Future Landing Map

- Status: Non-normative design guidance
- Date: 2026-08-15
- Rule: This map identifies likely durable homes. It adds no implementation requirements to the
  focused CLI grammar correction.

## Placement principles

1. Put a feature under the operator question it answers, not the source that stores its data.
2. Keep `graph` read-only and relational; mutation stays with the noun that owns the relationship.
3. Keep `explain` about accepted configuration shape, not runtime condition or concrete values.
4. Extend an existing list, doctor, edit, environment, or kind-specific command before creating a
   generic concrete-object card.
5. Add graph syntax only when a named consumer needs semantics the fixed launch view cannot express.

## Landing decisions

| Future need                                     | Likely home                                           | Boundary                                                                             |
| ----------------------------------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Explain one nested field                        | `resource explain TARGET --field PATH`                | An option avoids ambiguity with dotted implementation names                          |
| Compare or search accepted fields               | `resource explain` extension                          | Only with stable schema-reference selection semantics                                |
| Enumerate resource kinds                        | Existing `resource kinds`                             | Inventory is not explanation                                                         |
| Select only dependencies or dependents          | `graph show --direction VALUE`                        | Add when a consumer needs less than the fixed both-directions view                   |
| Traverse beyond direct neighbors                | `graph show --depth N`                                | Requires explicit cycle, repetition, and bound semantics                             |
| Filter a neighborhood by node kind              | `graph show --kind KIND[,KIND...]`                    | Define whether filtering hides paths or only output before adding it                 |
| Show all relations of selected kinds            | A future focus-optional `graph show` extension        | Outside the single-focus launch                                                      |
| Explain why one node reaches another            | Future `graph path FROM TO`                           | A path is a distinct query, not two `show` operands                                  |
| Add live-instance focal nodes or relation kinds | Graph service extensions                              | Compose read-only domain facts; do not insert them into the frozen resource registry |
| Add DOT, Mermaid, or another encoding           | `graph show --output VALUE`                           | Only with a named consumer and stable encoding contract                              |
| Stream relation changes                         | Future `graph watch`                                  | Temporal consistency differs from snapshot rendering                                 |
| Show effective managed environment              | Existing `env show`                                   | Environment is a projection, not a graph node or edge                                |
| Diagnose readiness or installation health       | Existing kind-specific describe or `doctor`           | Graph does not probe or remediate                                                    |
| Locate or modify a declaration                  | Existing `resource edit`, sample, and schema commands | Authoring and file ownership are not graph queries                                   |
| Grant access or edit ordered membership         | Existing owning-noun commands                         | Graph displays relationships but does not mutate them                                |
| Broader CLI consistency                         | A separate focused hygiene effort                     | Price and review independently                                                       |
