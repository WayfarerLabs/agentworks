# CLI Grammar Rework, Prior-Art Research

<!-- cspell:words Graphviz -->

- Status: Research input for the verb-contract review
- Date: 2026-08-10
- Scope: Type and instance inspection, graph traversal, structured output, exit status, and
  destructive-operation grammar

## Executive summary

The strongest prior art supports the core split already under consideration:

- `explain` documents a type and its fields. Kubernetes uses a dotted field path directly in the
  operand, which validates reserving `KIND.FIELD.PATH` now without implementing drill-down yet.
- `describe` or `inspect` addresses concrete objects. A type-qualified identity is the reliable way
  to span heterogeneous object kinds; Agentworks' `KIND/NAME` token avoids Docker's separate
  disambiguation option.
- A dependency command should default to a human tree and name traversal direction and depth
  explicitly. Cargo supplies the closest interaction precedent. Nix shows that a future "why does A
  reach B?" query is a shortest-path question, distinct from ordinary neighborhood rendering.
- Output-selection vocabulary is not consistent across tools. In Agentworks, the recently settled
  `--output human|json` contract is stronger local prior art than Docker's template-oriented
  `--format` or GitHub CLI's field-selecting `--json`.
- Click's native distinction is exactly the proposed `0` success, `1` abort/domain failure, and `2`
  usage error. POSIX reserves additional meanings for child execution failures and signals, which
  supports preserving child exit statuses only on commands whose purpose is to run a child.
- `--force` has no universal safety meaning. Docker uses it both to suppress confirmation and to
  kill a running container. Agentworks should therefore define it locally and narrowly instead of
  claiming an ecosystem-wide convention.

These sources do not settle the open Agentworks vocabulary decisions. They narrow the honest choices
and identify which claims are precedent-backed versus project-specific.

## Findings and design implications

### 1. `explain` is type documentation, including dotted field paths

Kubernetes defines `kubectl explain TYPE` as documentation for a resource's fields and structure. It
identifies nested fields as `TYPE.FIELD[.FIELD]` and offers recursive field rendering. This is a
direct precedent for:

```text
agw resource explain secret
agw resource explain secret.backend_mappings
```

The second form remains future work, but reserving the grammar now is justified. The precedent does
not support using bare `explain` as the primary kind inventory: Kubernetes keeps that concern in
`kubectl api-resources`. Absorbing `resource kinds` into bare `resource explain` is therefore an
Agentworks discoverability choice that needs its own operator ruling, not something inherited from
kubectl.

Design implications:

- Keep `explain` config-independent and schema-derived.
- Parse the target so the kind or capability implementation identity is distinct from the dotted
  field path.
- Present the bare-invocation behavior as an explicit local decision.

### 2. Concrete-object inspection needs a type-qualified identity

Kubernetes accepts `TYPE/NAME` for concrete resources. Docker's generic `inspect NAME|ID...` instead
needs `--type` when different object kinds share a name. Agentworks already prohibits `/` inside
resource names, so `KIND/NAME` provides both the identity and disambiguation in one token.

Docker defaults generic inspection to JSON, while Kubernetes `describe` is a human-oriented
aggregation that may fetch related objects. Neither output contract should be copied wholesale:

- Agentworks already has existing human projections and a versioned JSON contract.
- The proposed card intentionally excludes relationships so `graph` owns them.
- Kind-specific facts must come from the same service record used by both renderers, not from a
  string-only plugin hook.

Design implications:

- Use one `KIND/NAME` parser and completion source across generic `describe`, `graph`, and any other
  node-addressed views.
- Keep relationship removal as an Agentworks responsibility boundary, despite kubectl's broader
  `describe` precedent.
- If kind-locked aliases remain, pin service facts and serialized output, not merely similar prose.

### 3. Tree traversal has established direction and depth controls

Cargo's `tree` command defaults to an indented dependency tree, uses `--invert TARGET` for reverse
dependencies, and defines `--depth 1` as direct dependencies. It also lets users select edge kinds
and marks deduplicated repeated nodes.

The useful lessons for Agentworks are:

- A tree is an honest human default for a dependency view.
- Depth needs an exact zero/one/unbounded contract.
- Reverse traversal should say which relation it shows. `--up` and `--down` are compact but require
  an Agentworks-specific orientation rule; `--dependencies` and `--dependents`, or a single
  `--reverse`, are more self-describing precedents.
- A general graph is not necessarily a tree. Repeated nodes, multiple focal roots, and cycles need
  explicit rendering and deduplication rules.

This source does not justify a `--format` encoding selector. Cargo's `--format` is a template for
each package label, while its tree structure stays the same.

### 4. Path explanation is a separate future query

Nix `why-depends PACKAGE DEPENDENCY` shows a shortest path through its reference graph, with an
option to show every contributing edge instead. That contract answers a different question from
"show the neighborhood around this node."

Design implications:

- Preserve a future two-node path-query grammar.
- Do not overload multi-focal graph rendering to infer that two operands request a path.
- Retain edge provenance now if it already exists, but do not build new provenance machinery only
  for a deferred path renderer.

### 5. DOT is justified only by a named renderer consumer

Terraform's `graph` command emits DOT specifically so users can pipe it into Graphviz to create an
image. Graphviz documents DOT as its graph language and exposes format selection at the rendering
tool boundary.

This is strong support for the seed's consumer test:

- If Agentworks names Graphviz interoperability as a supported workflow, DOT is justified.
- If the day-one consumers are terminal readers and API/tooling clients, tree plus the versioned
  JSON envelope is complete. DOT and Mermaid should wait.

Terraform also exposes internal operation graph variants. Agentworks should not mirror that
complexity unless it has genuinely different graph truths to select, rather than renderer options.

### 6. Local structured-output contracts outrank external spelling

Comparable tools use incompatible shapes:

- Kubernetes generally uses `--output` for encodings and renderer choices.
- Docker uses `--format` for Go templates; generic `inspect` defaults to JSON.
- GitHub CLI uses `--json FIELD,...` to opt into selected JSON fields, then `--jq` or `--template`
  for further transformation.

Agentworks already settled an explicit per-command `--output human|json` option and a versioned JSON
envelope. Introducing `--format tree|json` for `graph` would create a second spelling for the same
machine-output decision unless day one includes multiple human renderings.

Design implications:

- Prefer `--output human|json`, with the human renderer using a tree.
- Keep the default deterministic rather than silently switching encodings when stdout is not a TTY.
- Use terminal detection only for presentation properties such as color or character set, never for
  the data contract.

### 7. Exit status has a sound framework-native baseline

Click documents these native outcomes:

- `0` for successful completion and explicit help;
- `1` for abort;
- `2` for incorrect invocation that displays help.

POSIX treats zero as success and nonzero as failure, while reserving `126` and `127` for child
execution failures and values above `128` for signal termination conventions. It also recognizes
commands such as `diff` where `1` is a meaningful negative result rather than an execution error.

Design implications:

- Adopt `0` success, `1` completed domain-negative result or domain failure, and `2` usage.
- Preserve a child status only when running the child is the command's contract.
- Specify whether aggregate proof commands continue through every requested item and return `1` if
  any proof is negative.
- Do not promise that every possible internal error has a unique numeric code.

### 8. Confirmation bypass and force are project vocabulary

Docker uses `--force` both to skip confirmation on prune commands and to kill a running container
during removal. Terraform uses the more explicit `--auto-approve` to skip an apply confirmation and
couples JSON mode to non-interactive execution.

The ecosystem therefore offers no single meaning that Agentworks can inherit for `--force`.
Agentworks can make its own surface more predictable by reserving:

- `--yes` for confirmation bypass;
- a verb-specific kill or rebuild flag for destructive execution mode;
- `--force` for a single, documented invariant override, if the operator keeps that ruling.

This is a local consistency improvement, not a claim that other CLIs agree.

## Refuted or do-not-rely-on claims

| Claim                                                             | Disposition                                                                                                                   |
| ----------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| kubectl proves bare `explain` should list kinds.                  | Refuted. Kubernetes directs kind inventory to `api-resources`; bare Agentworks behavior is a local choice.                    |
| A command named `describe` conventionally includes relationships. | Do not rely on it. Kubectl does, Docker inspect does not establish that boundary, and Agentworks has a dedicated graph owner. |
| `--format` is the standard spelling for JSON selection.           | Refuted by mixed practice. Kubernetes uses `--output`; Docker's `--format` means templating; GitHub CLI uses `--json`.        |
| Graph commands conventionally emit DOT.                           | Refuted. Terraform does because Graphviz is its named consumer; Cargo defaults to a terminal tree.                            |
| `--force` has a universal meaning.                                | Refuted. Docker alone uses it for at least confirmation bypass and kill-plus-remove behavior.                                 |
| Non-TTY output should silently become JSON.                       | Unsupported by the reviewed precedents and contrary to Agentworks' explicit output contract.                                  |

## Open questions carried into the verb-contract review

1. Does bare `resource explain` list kinds, retain `resource kinds`, or show help? Prior art does
   not decide this.
2. Should the generic card be top-level `agw describe KIND/NAME`, remain under `agw resource`, or
   exist in both generic and kind-locked forms? Prior art supports the identity grammar, not the
   command home.
3. Are graph direction flags the terse `--up/--down/--both`, or self-describing relationship names?
   The answer must pin orientation and combination rules.
4. Is Graphviz an actual supported day-one consumer? If not, omit DOT.
5. Does JSON output imply non-interactive behavior on every inspector and proof command, or is that
   solely the global `--non-interactive` option's responsibility?

## Sources

| Source                                                                                                             | Quality                                       | Angle used                                                                    |
| ------------------------------------------------------------------------------------------------------------------ | --------------------------------------------- | ----------------------------------------------------------------------------- |
| [Kubernetes: kubectl explain](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_explain/)             | Primary, official generated command reference | Type documentation, dotted field paths, recursive fields, output spelling     |
| [Kubernetes: kubectl command reference](https://kubernetes.io/docs/reference/kubectl/)                             | Primary, official reference                   | `TYPE/NAME`, get/list/describe/edit separation                                |
| [Docker: docker inspect](https://docs.docker.com/reference/cli/docker/inspect/)                                    | Primary, official reference                   | Generic object inspection, ambiguity, JSON default, template formatting       |
| [Cargo: cargo tree](https://doc.rust-lang.org/beta/cargo/commands/cargo-tree.html)                                 | Primary, official reference                   | Human tree, reverse traversal, depth, edge selection, deduplication           |
| [Nix: why-depends](https://nix.dev/manual/nix/stable/command-ref/new-cli/nix3-why-depends)                         | Primary, official reference                   | Shortest-path dependency explanation and all-path expansion                   |
| [Terraform: graph](https://developer.hashicorp.com/terraform/cli/commands/graph)                                   | Primary, official reference                   | DOT as an explicit Graphviz interoperability contract                         |
| [Graphviz: DOT output](https://graphviz.org/docs/outputs/canon/)                                                   | Primary, official reference                   | DOT language and renderer boundary                                            |
| [GitHub CLI: formatting](https://cli.github.com/manual/gh_help_formatting)                                         | Primary, official reference                   | Field-selected JSON and post-projection formatting                            |
| [Click: exceptions and exit codes](https://click.palletsprojects.com/en/stable/exceptions/)                        | Primary, official framework documentation     | Success, abort, and usage status behavior                                     |
| [POSIX utility conventions](https://pubs.opengroup.org/onlinepubs/9699919799.2016edition/utilities/V3_chap01.html) | Primary standard                              | Zero/nonzero portability contract                                             |
| [POSIX exit](https://pubs.opengroup.org/onlinepubs/009695399/utilities/exit.html)                                  | Primary standard                              | Reserved child and signal status meanings                                     |
| [Docker: system prune](https://docs.docker.com/reference/cli/docker/system/prune/)                                 | Primary, official reference                   | `--force` as confirmation bypass                                              |
| [Docker: container rm](https://docs.docker.com/reference/cli/docker/container/rm/)                                 | Primary, official reference                   | `--force` as kill plus removal                                                |
| [Terraform: apply](https://developer.hashicorp.com/terraform/cli/commands/apply)                                   | Primary, official reference                   | Explicit approval bypass and JSON/non-interactive coupling                    |
| [Command Line Interface Guidelines](https://clig.dev/)                                                             | Secondary practitioner guidance               | Human-first design, composability, help, stdout/stderr, and exit-code framing |
