# Guide Contract Low-Level Design

- Status: Current Markdown-shell destination with shell-backed index and guide noun/verb grammar
- Scope: concept discovery, bounded shell expansion, mode selection, and inert release evidence
- FRD: `frd.md` R1 through R5
- HLA: `hla.md`, Shell-backed index through Safety and failure behavior

The immutable implementation journey remains in `plan.md`. This contract replaces the retained
typed-block, action, evidence, and onboarding-assessment machinery with auto-discovered Markdown
concept shells.

## Contract boundary

The guide has three content paths:

1. A reserved no-topic index shell renders with catalog-derived topic rows and no operator state.
2. `agw guide show TOPIC` renders one auto-discovered Markdown shell or exact release topic.
3. Exact historical release-note topics render bounded sections from the packaged changelog as inert
   generated evidence. They are not shells.

The selected-topic path is a small Markdown expander, not a template engine. It supports authored
Markdown and exactly two directive forms: an agent-only fence and a packaged-section include.

## Concept shells and discovery

Each concept is one UTF-8 Markdown file named `<stem>.md` directly under a directory named
`guide-content` in the installed first-party `agentworks` package tree. Discovery starts at
`importlib.resources.files("agentworks")` and walks only that traversable package resource. The
guide does not scan the working tree, current directory, another installed package, or a
filesystem-relative fallback.

Discovery validates every file and orders package-relative directories, then filenames,
deterministically. A filename stem uses lower kebab case and produces the global slug
`concept-<stem>`. Slugs have one global namespace; two directories producing the same slug are a
structural catalog error. The guide never shadows one shell with another or changes the winner based
on discovery order.

The first-party apt and install-command content uses filenames `apt.md` and `install-commands.md`,
producing `concept-apt` and `concept-install-commands`. The former `plugin/<name>/<topic>` grammar
and registration adapter are deleted; separately installed plugins are outside discovery.

Every shell begins with this restricted frontmatter shape:

```markdown
---
description: Help an operator get started with Agentworks.
index-order: 20
---
```

`description` is required, non-empty, single-line text used for discovery and completion metadata.
`index-order` is optional and, when present, is a one-to-four-digit non-negative base-10 integer. It
selects the concept for the concise index. Equal values sort by slug. No other frontmatter key, YAML
feature, or executable value is accepted. The body contains exactly one authored ATX level-1 heading
outside agent-only fences, which supplies the topic title. Setext headings are not accepted anywhere
in the shell. The filename supplies identity; the heading supplies title; frontmatter supplies
description. Python does not register those values per topic.

`agw guide list` discovers names from shell filenames plus the separately generated exact
release-note names. It loads no operator state. Shell structural defects, including duplicate slugs,
fail the catalog request rather than producing a partial or ambiguous catalog.

The no-topic index uses that same complete catalog. A defect in any discovered shell therefore
prevents the index from rendering. This intentionally withdraws the former fixed trail sign's
catalog-free exemption instead of maintaining a second fallback topic list.

Exactly one reserved `_index.md` exists in the core guide-content directory. It follows the same
description, H1, fence, include, and rendering rules, must omit `index-order`, and is excluded from
the ordinary concept namespace. No other underscore-prefixed Markdown filename is valid.

The root `README.md` is not a shell and is not discovered. One custom Hatch build hook materializes
it at `agentworks/_guide_sources/README.md` as an include-only resource:

- a direct wheel reads the repository-root file and maps it to the package path;
- a source distribution reads the repository-root file and vendors it at that package path; and
- a wheel built from the source distribution uses the vendored file already selected with the
  `agentworks` package, without overwriting it.

The hook fails the build if the required source for its mode is absent. A source/editable run whose
package resource is absent may read `<verified-repository-root>/README.md` only after confirming the
fixed layout: `.git`, `README.md`, `cli/pyproject.toml`, and `cli/agentworks/`. It does not use the
working directory or search parent directories. No checked-in generated README mirror, runtime
network fetch, or other repository-root include source exists.

## Directive grammar

Directives are exact standalone HTML-comment lines beginning at column zero, outside Markdown code
fences, and with no leading or trailing whitespace. They may sit between authored top-level Markdown
blocks or sections. Directive-looking text inside a list or blockquote container, inside a code
fence, or with surrounding whitespace is ordinary authored text. Arguments are double-quoted
literals; unknown top-level directives, attributes, or trailing content are structural shell
defects.

There are no variables, loops, conditions, expressions, generic operation names, recursive includes,
arbitrary paths, or extension registry. Directive output is inert Markdown and is never processed
again for directives.

### Agent-only fence

```markdown
<!-- agw:agent-only -->

This context is useful only to an Agentworks assistant agent.

<!-- /agw:agent-only -->
```

Fences may appear anywhere between authored top-level Markdown blocks or sections. This does not
extend into list or blockquote containers. They must be balanced and cannot nest. Human mode removes
the markers and everything between them. Agent mode removes only the markers. Filtering happens
before include resolution, so a human-hidden region cannot read a packaged document.

### Packaged-section include

```markdown
<!-- agw:include path="_guide_sources/README.md" heading="Core Concepts" heading-offset="0" -->
```

`path` is relative to `importlib.resources.files("agentworks")`. It must be a bounded Markdown
resource below that package root with no absolute form, empty, dot, or parent segment, and no
filesystem fallback except the exact verified source-checkout case above. The curated
`_guide_sources/README.md` resource and normal Markdown inside the `agentworks` package are the only
sources. No other package or repository-root file is an include source. The target must be UTF-8
Markdown within the repository's content-size limit.

`heading` matches exactly one H2-H6 ATX heading outside code fences. Matching compares the visible
heading text after removing the ATX marker and optional closing hashes. H1 imports, zero matches,
and multiple matches are structural errors. Expansion inserts the matching heading and its body
through, but not including, the next heading of equal or higher rank.

`heading-offset` is an optional signed base-10 integer and defaults to `0`. The expander adds it to
the level of every ATX heading in the selected section outside code fences. The offset is static for
the include; a shifted heading below H2 or above H6 is a structural error. A Setext heading anywhere
in the selected section is a structural error rather than an unchanged escape from this rule.

Included bytes are inert. The expander does not process directives, frontmatter, or agent-only
markers found in an included document. Includes cannot recurse.

The expander recognizes Markdown link and image destinations in inline forms and reference
definitions. Absolute HTTPS, fragment-only, and empty current-document destinations pass through
unchanged. A relative destination is resolved with POSIX path semantics against the repository
mapping of the Markdown document that contains it. A result outside that mapping, an absolute local
path, a scheme-relative destination, a query string, or a non-HTTPS scheme is a structural error. An
optional fragment is split before path normalization and encoding, then reattached unchanged to the
canonical URL. Thus `cli/command-reference.md#named-consoles` retains `#named-consoles` as a
fragment rather than path data.

Valid relative destinations from normal package documents map to:

```text
link:  https://github.com/WayfarerLabs/agentworks/blob/main/cli/agentworks/<path>
image: https://raw.githubusercontent.com/WayfarerLabs/agentworks/main/cli/agentworks/<path>
```

For the curated `_guide_sources/README.md` mirror, resolution instead starts at the repository root,
so `docs/images/agw-topology.png` becomes
`https://raw.githubusercontent.com/WayfarerLabs/agentworks/main/docs/images/agw-topology.png`.
Normal package documents retain the `cli/agentworks/` prefix shown above. A relative link from the
root mirror uses the equivalent `https://github.com/WayfarerLabs/agentworks/blob/main/<path>` form.

Guide references are current explanatory material, not immutable release evidence. Path segments are
URL-encoded. A reference-style link or image must have its definition inside the same emitted shell
or extracted section. A missing definition, or one shared by a link and image that need different
canonical bases, is a structural error. The rewriter does not fetch, validate, or embed remote
content. Included references resolve relative to the included document rather than the including
shell.

## Rendering and failure behavior

For `agw guide show TOPIC`, rendering proceeds in this order:

1. discover and structurally validate the complete catalog, then resolve the one requested shell or
   exact release topic;
2. filter agent-only fences for the selected mode;
3. resolve visible packaged includes;
4. rewrite visible repository-relative destinations; and
5. sanitize and emit the complete Markdown response.

Structural defects remain nonzero: invalid topic syntax, unknown topics, malformed frontmatter,
invalid filename or heading structure, duplicate slugs, malformed or unbalanced directives,
disallowed include targets, missing or ambiguous include headings, content-bound violations, and
internal invariant failures. Guide success means the requested guidance rendered; it does not assert
installation health.

Rendering never loads configuration, registry, database, resources, secrets, provider state,
network, transports, or subprocesses. A shell points to command-owned help and inspection surfaces
when it needs current facts.

Final output strips C0 controls except line feed and tab, plus DEL and C1 controls, from authored,
included, and generated release-note text.

## Index and list behavior

The no-topic human and agent paths discover the static catalog, render `_index.md` through the
ordinary shell pipeline, then append topics with `index-order` sorted by `(index_order, slug)`. Each
row uses the concept slug and description already owned by frontmatter. Every selected destination
therefore resolves by construction; no duplicated Python tuple remains.

The footer reports `len(ordinary concepts) - len(indexed concepts)` and points to `agw guide list`.
Exact generated release-note topics are deliberately absent from both operands: historical versions
are listable evidence, not omitted authored concepts. `agw guide list` emits all ordinary concept
slugs plus exact packaged release-note topic names, one per line, for both operators and shell
completion.

`guide` is one Typer command group with two real subcommands:

- `agw guide list` emits the stable topic-name stream; and
- `agw guide show TOPIC` accepts exactly one topic and renders it.

Invoking `agw guide` without a subcommand renders the index through the group callback. The callback
retains `--agent/--human` for the bootstrap's `agw guide --agent` form and owns that option for the
whole group. `agw guide --agent show TOPIC` renders the selected topic in agent mode. The same
option before `list` is accepted but does not alter its stable, mode-independent output. `show` does
not duplicate the option after its verb. The unreleased direct/variadic `agw guide TOPIC...` form
and old guide-specific `--names-only` option are removed without aliases.

Typer's ordinary command tree completes `list` and `show`. Only `show`'s single `TOPIC` argument
uses the existing dynamic topic-name source, which calls `agw guide list`. Catalog discovery and
validation are atomic for index, list, show, and completion; no path returns a partial catalog when
an unrelated shell is malformed. There is no reserved positional value, terminal-value schema, or
guide-specific positional parser in the completion generators.

Mode selection retains this precedence:

1. the explicit group-level `--agent` or `--human` option;
2. the exact registered `CLAUDECODE=1` execution signature; and
3. human for TTY stdout, otherwise agent.

Exact `concept-release-notes/vMAJOR-MINOR-PATCH` topics are resolved and rendered directly from
bounded packaged changelog sections. They use no `ReleaseNotes` block, generic topic contribution,
or old catalog union. Their content is escaped inert evidence, performs no network work, and uses no
shell directives. The base `concept-release-notes` guidance is an ordinary shell.

## Removed contract and code

The implementation removes, rather than adapts, the superseded guide framework:

- `Overview`, `Teaching`, `AgentNote`, `ReleaseNotes`, `ActionList`, and `TopicLinks` typed blocks
  and their parsers, serializers, renderer branches, validators, and tests;
- `GuideAction`, `ConsentBoundary`, action-token grammar, action rendering, and action validation;
- `--evidence`, evidence parsing and replay, and evidence-driven guide behavior;
- the onboarding snapshot, assessment, status, derived-plan, verification, and next-action logic;
- `Plugin.guide_topics`, every subsystem or plugin `_load_guide_contributions` adapter and
  `guide_contributions.py` module, the first-party guide-package loader map, manual per-topic
  constructors, related-topic graphs, broken-link resolution, and contribution-specific ownership
  machinery; and
- compatibility layers whose only purpose is to preserve one of those removed shapes.

Useful instructions and links become ordinary reviewed Markdown in their owning shell. Onboarding is
not special and has no projection, private state machine, or assessment protocol. When guidance
suggests an operation that could cross the operator's current authorization, review requires scope
and impact, expected result, and a refusal alternative in the prose; no schema or prose-policing
test replaces that review.

## Structural verification

Focused tests protect behavior and boundaries:

- filename discovery, global slug uniqueness, restricted frontmatter, and the unfenced single-H1
  invariant;
- balanced non-nested agent fences and filtering before include work;
- exact unique H2-H6 ATX-section extraction beneath the one `agentworks` package root, bounded
  static heading offsets, canonical URL rewriting for repository-relative links and images,
  section-local reference definitions, size bounds, and inert non-recursive included text;
- exact packaging of the canonical root README, `concept-core-model` rendering its selected sections
  and images, the actual `#named-consoles` relative fragment, and no other repository-root include
  source;
- nonzero structural failures and proof that selected rendering does not load operator state;
- reserved index discovery, deterministic index ordering, ordinary-only omitted counts, mode
  precedence, real `guide list`/`guide show` subcommands, one-topic show completion, and packaged
  exact release evidence; and
- shell and exact README package-data presence in a direct wheel, source distribution, wheel rebuilt
  from that source distribution, and verified editable-source fallback.

Tests use fixture content to assert structure and behavior. They do not pin, blacklist, snapshot, or
otherwise police the wording of repository-authored Markdown, descriptions, warnings, or prompts.
