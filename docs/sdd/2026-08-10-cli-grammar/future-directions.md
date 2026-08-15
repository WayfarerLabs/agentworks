# Future Landing Map

- Status: Non-normative design guidance
- Date: 2026-08-15
- Rule: This map identifies durable homes. It does not add implementation requirements to the
  focused CLI grammar correction.

## Placement principles

1. Put a feature under the operator question it answers, not the source that happens to store its
   data.
2. Keep `graph` read-only and relational. Relationship mutation stays with the noun that owns the
   relationship.
3. Keep `explain` about accepted configuration shape. Runtime condition and concrete values belong
   elsewhere.
4. Extend an existing list, guide, doctor, edit, environment, or kind-specific command before
   creating a generic concrete-object card.
5. Add a graph encoding or query only when a named consumer needs semantics that `show` cannot
   express.

## Landing decisions

| Future need                                          | Durable home                                                           | Boundary                                                                                            |
| ---------------------------------------------------- | ---------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| Explain one nested field                             | `resource explain TARGET --field PATH`                                 | Schema-derived and config-independent; the option avoids ambiguity with dotted implementation names |
| Compare or search accepted fields                    | `resource explain` extension                                           | Only if the schema-reference service can define stable selection semantics                          |
| Enumerate resource kinds                             | Existing `resource kinds`                                              | Inventory is not explanation                                                                        |
| Show a node's dependency neighborhood                | `graph show`                                                           | Initial graph question; read-only typed nodes and edges                                             |
| Explain why one node reaches another                 | Future `graph path FROM TO`                                            | Separate shortest-path or all-path contract; do not overload two `show` operands                    |
| Show all relations of selected kinds                 | A future focus-optional `graph show` extension                         | Same facts and renderer, but explicitly outside the focal-node launch                               |
| Add live-instance node and edge kinds                | Graph service relation providers                                       | Compose read-only domain facts; never insert them into the frozen resource registry                 |
| Add DOT, Mermaid, or another encoding                | `graph show --output VALUE`                                            | Only with a named consumer and a stable encoding contract                                           |
| Stream relation changes                              | A future `graph watch` only if demanded                                | Temporal events and consistency are a different contract from snapshot rendering                    |
| Show effective managed environment                   | Existing `env show`                                                    | Environment is a projection, not a graph node or edge                                               |
| Diagnose readiness or installation health            | Existing kind-specific describe or `doctor`                            | Graph may carry a safe status annotation but does not perform probes or remediation                 |
| Show resource identity, origin, and description      | Existing `resource list`, `resource kinds`, and kind-specific commands | Do not recreate generic `resource describe`                                                         |
| Locate or modify a declaration                       | Existing `resource edit`, sample, and schema commands                  | Authoring and file ownership are not graph queries                                                  |
| Grant agent access to workspaces                     | Existing agent relationship commands                                   | Mutation belongs to the agent-side owner; graph only displays the result                            |
| Add or remove console membership                     | Existing console membership commands                                   | Preserve ordered mutation semantics outside graph                                                   |
| Verify a connection or resolve a secret source       | Existing noun-specific verifier or `doctor`                            | These may interact or use credentials; graph must not                                               |
| Broader flag, confirmation, or lifecycle consistency | A separate focused CLI hygiene effort                                  | Price and review it independently; do not reopen this SDD                                           |

## Extension seams to preserve now

- The first graph subcommand should have a service boundary that returns typed nodes, edges, source
  provenance, and deterministic order before either human or JSON rendering.
- Edge kinds should distinguish declared references from live-instance usage. Future providers can
  add kinds without changing existing meanings.
- Node identity should be parsed once at the graph boundary, but the launch need not claim that all
  future database-backed names already share one global grammar.
- Source acquisition should be demand-driven. Adding a live relation provider later must not make
  declaration-only queries depend on the database.
- Machine output should add fields compatibly within its versioning policy or receive a new schema
  version; human prose is not the integration contract.
- The graph group should reserve subcommands for distinct queries, not renderer aliases or source
  categories.

## Ideas deliberately left homeless

A feature should not be reserved in grammar merely because it is imaginable. Generic node cards,
arbitrary graph query languages, interactive graph browsers, historical graph state, mutation
through graph edges, and universal live-object identity need concrete consumers and their own SDDs
before they receive command names.
