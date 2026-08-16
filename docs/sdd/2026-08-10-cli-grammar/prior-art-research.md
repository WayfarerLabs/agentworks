# Focused CLI Prior-Art Research

<!-- cspell:words Graphviz -->

- Status: Research input for FRD and HLA
- Date: 2026-08-15
- Scope: Type explanation, graph query shape, and output ownership

## Findings

### Explain remains the right type-documentation verb

Kubernetes uses `kubectl explain TYPE` for a resource's fields and structure. That supports the
rename from `describe-kind` to `explain` and the separation among type documentation, sample
generation, schema emission, and concrete-object inspection.

Kubernetes also supports dotted field operands, but Agentworks capability implementation names may
contain dots. If field selection is later needed, an explicit `--field PATH` option keeps
`KIND/NAME` unambiguous. Kubernetes does not use bare `explain` as its resource inventory, so
keeping `resource kinds` is the conservative choice.

### Neighborhood and path queries are different operations

Cargo's `tree` command shows that dependency views can support explicit reverse traversal and depth
controls, and that repeated nodes need defined rendering. Agentworks has a concrete launch consumer:
a dependents query from a VM platform must be able to continue through sites to the live VMs using
them. That supports direction and depth controls at launch, with explicit cycle and repetition
semantics in HLA.

Nix's `why-depends` answers a different question: why one node reaches another. It renders a path,
not a neighborhood. That supports a future `graph path FROM TO` rather than interpreting two
operands to `graph show` specially.

### A graph namespace should be query-oriented

Terraform emits DOT because Graphviz is its explicit consumer. Agentworks has no equivalent launch
consumer, so terminal output plus the existing JSON envelope is sufficient.

`graph show` names a neighborhood query and leaves `graph path` available for later. Dividing by
source noun, such as `graph resource` and `graph session`, would reproduce storage boundaries and
make cross-source relationships harder to express.

### Local output conventions are stronger than mixed external precedent

Kubernetes uses `--output`, while GitHub CLI uses `--json` for field selection. Agentworks already
has `--output human|json` and a versioned JSON envelope, so graph should extend that local contract.

## Sources

| Source                                                                                                 | Use                                                         |
| ------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------- |
| [Kubernetes: kubectl explain](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_explain/) | Type and field documentation                                |
| [Kubernetes command reference](https://kubernetes.io/docs/reference/kubectl/)                          | Separation of inventory, explanation, and object operations |
| [Cargo: cargo tree](https://doc.rust-lang.org/cargo/commands/cargo-tree.html)                          | Future direction, depth, and repeated-node semantics        |
| [Nix: why-depends](https://nix.dev/manual/nix/stable/command-ref/new-cli/nix3-why-depends)             | Separate path-query semantics                               |
| [Terraform: graph](https://developer.hashicorp.com/terraform/cli/commands/graph)                       | DOT only with a named Graphviz consumer                     |
| [GitHub CLI formatting](https://cli.github.com/manual/gh_help_formatting)                              | Contrast with the local structured-output contract          |
