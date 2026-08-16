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
5. Add graph syntax only when a named consumer needs semantics the focused traversal cannot express.

## Verb vocabulary

These meanings are the proposed durable CLI grammar. At implementation handoff, track a separate
focused CLI-conventions follow-up to audit them against the complete command surface and promote the
accepted glossary; this SDD is neither its permanent home nor a requirement to perform that broader
audit in this effort.

| Verb       | General operator question or action                                                        |
| ---------- | ------------------------------------------------------------------------------------------ |
| `list`     | Which concrete members exist, optionally narrowed by filters?                              |
| `show`     | What factual projection does this named scope or read-only query return?                   |
| `describe` | What domain-specific synthesis about this concrete object is uniquely useful here?         |
| `explain`  | What configuration shape is accepted, and what does each part mean?                        |
| `doctor`   | Is the system ready or healthy, what evidence says otherwise, and what should happen next? |
| `edit`     | Where is the operator-owned declaration, and open or locate it for modification.           |
| `sample`   | Produce an inert example declaration that the operator may choose to write.                |
| `schema`   | Emit the machine-readable accepted shape, or install it at its canonical destination.      |
| `create`   | Create and persist a new concrete object.                                                  |
| `delete`   | Remove a concrete object under the command's confirmation contract.                        |

`describe` is intentionally the narrowest and most skeptical word in the table. A domain command
keeps it only when it synthesizes something that `list`, `show`, `explain`, `doctor`, and authoring
commands do not already provide. `secret describe` passes that test because backend mapping and
resolution preview are secret-specific synthesis.

## Capability explanation

The simple implementation form remains `resource explain CAPABILITY-KIND/IMPLEMENTATION`. No
capability exposes multiple facet models today; the planned first consumer is harness integration.
When that effort introduces the descriptor shape, the command renders its title, description,
overview, and every offered facet model in separate labeled sections by default. It must not flatten
facet models into one apparent schema. A later `--facet` filter is an optional convenience only,
never required for complete output.

The capability-kind form owns the shared vocabulary. For example,
`resource explain harness-integration` should explain the `vm`, `user`, `workspace`, and `session`
configuration facets, including that admin and agent consumers both select the `user` facet. The
implementation form explains how one implementation's facets play together. Config presence is not
an operational support claim, and the output must not present it as one.

The consuming resource still owns placement. A future field-level explanation may follow the
consumer's reference and facet mapping to provide a host-centered authoring shortcut, while the
capability implementation remains the schema authority.

## Landing decisions

| Future need                                     | Likely home                                           | Boundary                                                                             |
| ----------------------------------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Explain one nested field                        | `resource explain TARGET --field PATH`                | An option avoids ambiguity with dotted implementation names                          |
| Compare or search accepted fields               | `resource explain` extension                          | Only with stable schema-reference selection semantics                                |
| Enumerate resource kinds                        | Existing `resource kinds`                             | Inventory is not explanation                                                         |
| Filter a neighborhood by node kind              | `graph show --kind KIND[,KIND...]`                    | Define whether filtering hides paths or only output before adding it                 |
| Show all relations of selected kinds            | A future focus-optional `graph show` extension        | Outside the single-focus launch                                                      |
| Explain why one node reaches another            | Future `graph path FROM TO`                           | A path is a distinct query, not two `show` operands                                  |
| Add live-instance focal nodes or relation kinds | Graph service extensions                              | Compose read-only domain facts; do not insert them into the frozen resource registry |
| Qualify a relationship with a capability facet  | Reference producer, then graph projection             | Add with the first real producer; never infer from kinds or `usage` prose            |
| Add DOT, Mermaid, or another encoding           | `graph show --output VALUE`                           | Only with a named consumer and stable encoding contract                              |
| Stream relation changes                         | Future `graph watch`                                  | Temporal consistency differs from snapshot rendering                                 |
| Show effective managed environment              | Existing `env show`                                   | Environment is a projection, not a graph node or edge                                |
| Diagnose readiness or installation health       | Existing kind-specific describe or `doctor`           | Graph does not probe or remediate                                                    |
| Locate or modify a declaration                  | Existing `resource edit`, sample, and schema commands | Authoring and file ownership are not graph queries                                   |
| Grant access or edit ordered membership         | Existing owning-noun commands                         | Graph displays relationships but does not mutate them                                |
| Broader CLI consistency                         | A separate focused hygiene effort                     | Price and review independently                                                       |
