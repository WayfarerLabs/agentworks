# Prior Art: Onboarding and Discovery

## Executive summary

The strongest pattern is not one help system but a combination of three:

1. Go and Git keep concept topics addressable and listable beside command help.
2. PowerShell lets installed modules contribute conceptual help through a constrained file format.
3. Kubernetes and Terraform derive reference facts from live or registered schemas while retaining
   authored prose for teaching and caveats.

Agentworks should combine those strengths. `agw guide` should expose stable, completion-friendly
topic identities; accept inert, colocated topic data from every contributor; and render dynamic
facts from the finalized registry, graph, and schema sources. Reference documentation and teaching
content should share sources without becoming the same presentation. Rust's focused error
explanations reinforce keeping each topic small and directly addressable.

## Findings and design consequences

### Contributed conceptual topics: PowerShell

PowerShell modules can ship conceptual `about_*` topics alongside command and provider help. The
topics are local to the installed module, use a recognizable filename and required header shape, and
become discoverable through the same `Get-Help` surface as built-in topics. `Get-Help about_*` lists
the installed conceptual set.

Design consequences:

- Treat topic contribution as a first-class registration contract, not as a central switchboard.
- Reserve an unmistakable prefix for concepts. Agentworks uses the settled `concept-` prefix.
- Validate contributed data at registration so malformed content fails before rendering.
- Keep the content format deliberately smaller than an executable template language.

Sources:
[Writing Help for PowerShell Modules](https://learn.microsoft.com/en-us/powershell/scripting/developer/module/writing-help-for-windows-powershell-modules?view=powershell-7.5),
[Get-Help](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/get-help?view=powershell-7.5).

### Live schema walks: kubectl explain

`kubectl explain` obtains field information from the server's OpenAPI schema, supports nested field
paths, and points users to `kubectl api-resources` for the live inventory. This separates two useful
questions: what resource types exist, and what fields a type contains.

Design consequences:

- Keep inventory and reference facts live and derived.
- Reuse wave 2's field-documentation walk and schema sources rather than parsing rendered CLI text.
- Let guide pages link to deeper reference subtopics instead of recursively inlining an entire
  schema.
- Preserve the distinction between reference output and teaching output.

Sources: [kubectl explain](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_explain/),
[kubectl api-resources](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_api-resources/).

### Concept guides beside command help: Git

`git help <command-or-doc>` addresses both commands and documentation, while `git help --guides`
lists concept guides explicitly. The concept set is discoverable without mixing every guide into the
primary command list.

Design consequences:

- One guide command can resolve kinds, instances, implementations, and concepts.
- The no-topic page should list the taxonomy and available topics.
- Topic lookup should be exact and deterministic. Collisions should be registration errors rather
  than precedence rules users must learn.

Source: [git-help](https://git-scm.com/docs/git-help).

### A compact built-in topic index: go help

The Go command prints commands and additional help topics as separate groups, then uses the same
`go help <name>` lookup form for either. Topics are short identifiers suitable for shell use and
cross-reference one another.

Design consequences:

- Keep top-level guide output compact and progressively disclose detail.
- Use stable slugs and completion rather than fuzzy lookup.
- Render requested topics as independent documents so agents can fetch only what they need.

Source: [Command go](https://go.dev/cmd/go/).

### Focused explanations: rustc --explain

Rust compiler diagnostics carry stable error codes, and `rustc --explain <code>` expands one code
into a longer explanation. The explanation remains separate from the machine-readable diagnostic
format.

Design consequences:

- Keep guide markdown and machine-readable operational output as separate contracts.
- Prefer stable topic identities that remediation messages can reference directly.
- Keep topics focused rather than turning the top-level guide into a manual dump.

Sources:
[rustc command-line arguments](https://doc.rust-lang.org/stable/rustc/command-line-arguments.html),
[rustc JSON output](https://doc.rust-lang.org/rustc/json.html).

### Schema plus prose generation: Terraform providers

Terraform provider documentation generation combines descriptions from registered schemas with
authored examples and templates. Provider schemas are also consumed by validation and language
server tooling. This demonstrates the value of one schema source with multiple presentations, but
also shows why prose and examples remain necessary around generated field facts.

Design consequences:

- Wave 2's model and field-doc sources should remain presentation-free.
- Rich prose must be registered beside the kind or implementation it teaches.
- Describe should render concise reference prose plus field facts. Guide should render the same
  overview source plus teaching and live-state blocks.
- CI should prove generated or rendered docs remain synchronized with registration.

Sources:
[Implement documentation generation](https://developer.hashicorp.com/terraform/tutorials/providers-plugin-framework/providers-plugin-framework-documentation-generation),
[Terraform provider schemas](https://developer.hashicorp.com/terraform/plugin/framework/handling-data/schemas).

## Refuted or do-not-rely-on

- Do not infer that a general-purpose template engine is needed. None of the useful properties above
  requires expressions or contributed execution.
- Do not equate reference docs with onboarding. Schema-derived field facts answer shape questions,
  while onboarding needs sequencing, consent, security disclosure, and next actions.
- Do not scrape terminal output to derive guide facts. The prior art derives from schemas and
  registries, and Agentworks already has structured service-layer records.
- Do not copy PowerShell's filename and indentation constraints. Its contribution model is useful;
  its legacy text format is not.
- Do not adopt fuzzy topic matching. Exact slugs plus completion are safer for scripts and agents.

## Questions the research does not settle

- Prior art did not settle how onboarding success should be measured. The HLA defers general product
  feedback collection for the first release; acceptance runs keep their own timing and intervention
  evidence. A focused `concept-reporting-bugs` topic handles encountered defects without becoming a
  general feedback channel.
- Which wave 2 CLI names will expose schema and field-reference presentations.
- Whether future external plugins need localized guide content. The first contract should not claim
  localization support it does not implement.

## Source quality

| Source                             | Quality                             | Useful angle                               |
| ---------------------------------- | ----------------------------------- | ------------------------------------------ |
| Microsoft PowerShell documentation | Primary product documentation       | Module-contributed concepts and discovery  |
| Kubernetes kubectl reference       | Primary generated CLI documentation | Live inventory and schema walks            |
| Git documentation                  | Primary product documentation       | Unified command and concept lookup         |
| Go command documentation           | Primary product documentation       | Compact topic index and lookup             |
| Rust compiler documentation        | Primary product documentation       | Focused explanations and output separation |
| HashiCorp Terraform documentation  | Primary framework documentation     | Schema-derived docs combined with prose    |
