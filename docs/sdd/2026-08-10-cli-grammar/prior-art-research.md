# Focused CLI Prior-Art Research

<!-- cspell:words Graphviz -->

- Status: Research input for FRD and HLA
- Date: 2026-08-15
- Scope: Type explanation, graph query shape, and output ownership

## Findings

### Explain remains the right type-documentation verb

Kubernetes uses `kubectl explain TYPE` for a resource's fields and structure. That supports the
rename from `describe-kind` to `explain` and the existing separation among type documentation,
sample generation, schema emission, and concrete-object inspection.

Kubernetes also supports dotted field operands, but Agentworks capability implementation names may
contain dots. Agentworks should not copy that operand grammar. If field selection is later needed,
an explicit `--field PATH` option keeps `KIND/NAME` unambiguous.

Kubernetes does not use bare `explain` as its resource inventory. Keeping `resource kinds` is the
conservative launch choice.

### Neighborhood and path queries are different operations

Cargo's `tree` command provides the closest precedent for a human dependency neighborhood. It has
explicit reverse traversal and depth controls and defines how repeated packages appear. These are
the right questions for the initial graph view: orientation, depth, cycles, repetition, and stable
ordering.

Nix's `why-depends` answers a different question: why one node reaches another. It renders a path,
not a neighborhood. This supports a future `graph path FROM TO` subcommand rather than interpreting
two operands to `graph show` specially.

### A graph namespace should be query-oriented

Terraform's `graph` command emits DOT because Graphviz is its explicit consumer. Agentworks has no
equivalent launch consumer. Human terminal output plus the repository's JSON envelope is sufficient
until a real visualization workflow requires another encoding.

The useful namespace division is by query:

- `graph show` for a neighborhood or filtered graph;
- a possible future `graph path` for a two-node reachability explanation.

Dividing by source noun, such as `graph resource` and `graph session`, would reproduce storage
boundaries in the public grammar and make cross-source relationships harder to express.

### Local output conventions are stronger than mixed external precedent

Kubernetes uses `--output`, Docker uses `--format` for Go templates, and GitHub CLI uses `--json`
for field selection. There is no uniform external spelling. Agentworks already has
`--output human|json` and a versioned JSON envelope, so graph should extend that local contract.

## Design consequences

- Rename only the current type-reference command; do not add field paths or bare explain behavior.
- Give the graph group a named neighborhood subcommand so path queries have a clean future home.
- Use explicit direction and depth contracts and deterministic repetition and cycle behavior.
- Keep graph read-only and relation-focused; mutations remain with the noun that owns them.
- Do not ship DOT, Mermaid, or path queries without a named consumer.
- Use `--output` for encodings and a new closed machine-output command ID.

## Sources

| Source                                                                                                 | Use                                                         |
| ------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------- |
| [Kubernetes: kubectl explain](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_explain/) | Type and field documentation                                |
| [Kubernetes command reference](https://kubernetes.io/docs/reference/kubectl/)                          | Separation of inventory, explanation, and object operations |
| [Cargo: cargo tree](https://doc.rust-lang.org/cargo/commands/cargo-tree.html)                          | Direction, depth, repeated nodes, and human tree output     |
| [Nix: why-depends](https://nix.dev/manual/nix/stable/command-ref/new-cli/nix3-why-depends)             | Separate path-query semantics                               |
| [Terraform: graph](https://developer.hashicorp.com/terraform/cli/commands/graph)                       | DOT only when Graphviz is a named consumer                  |
| [GitHub CLI formatting](https://cli.github.com/manual/gh_help_formatting)                              | Contrast with the local structured-output contract          |
