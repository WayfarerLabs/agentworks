# Guide Contract Low-Level Design

- Status: Current Markdown-shell destination
- Scope: concept discovery, bounded shell expansion, mode selection, and inert release evidence
- FRD: `frd.md` R1 through R5
- HLA: `hla.md`, Fixed trail sign through Safety and degradation

The immutable implementation journey remains in `plan.md`. This contract replaces the retained
typed-block, action, evidence, and onboarding-assessment machinery with auto-discovered Markdown
concept shells.

## Contract boundary

The guide has three content paths:

1. A fixed no-topic trail sign renders without discovering shells or loading live state.
2. Selected `concept-*` topics render auto-discovered Markdown shells.
3. Exact historical release-note topics render bounded sections from the packaged changelog as inert
   generated evidence. They are not shells or live projections.

The selected-topic path is a small Markdown expander, not a template engine. It supports authored
Markdown and exactly four directive forms: an agent-only fence, a packaged-section include,
`resource-kinds`, and `resource-list`.

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

Every shell begins with this restricted frontmatter shape:

```markdown
---
description: Help an operator get started with Agentworks.
---
```

`description` is required, non-empty, single-line text used for discovery and completion metadata.
No other frontmatter key, YAML feature, or executable value is accepted. The body contains exactly
one authored ATX level-1 heading outside agent-only fences, which supplies the topic title. The
filename supplies identity; the heading supplies title; frontmatter supplies description. Python
does not register those values per topic.

`agw guide --names-only` discovers names from shell filenames plus the separately generated exact
release-note names. It invokes no live projection. Shell structural defects, including duplicate
slugs, fail the catalog request rather than producing a partial or ambiguous catalog.

## Directive grammar

Directives are standalone HTML-comment lines outside Markdown code fences. Directive-looking text
inside a code fence is ordinary authored text. Arguments are double-quoted literals; unknown
directives, attributes, or trailing content are structural shell defects.

There are no variables, loops, conditions, expressions, generic operation names, recursive includes,
arbitrary paths, or extension registry. Directive output is inert Markdown and is never processed
again for directives.

### Agent-only fence

```markdown
<!-- agw:agent-only -->

This context is useful only to an Agentworks assistant agent.

<!-- /agw:agent-only -->
```

Fences may appear wherever ordinary Markdown can appear. They must be balanced and cannot nest.
Human mode removes the markers and everything between them. Agent mode removes only the markers.
Filtering happens before include resolution or live projection, so a human-hidden region cannot read
a packaged document or invoke a service.

### Packaged-section include

```markdown
<!-- agw:include path="capabilities/README.md" heading="Kinds" -->
```

`path` is relative to `importlib.resources.files("agentworks")`. It must be a bounded Markdown
resource below that package root with no absolute form, empty, dot, or parent segment, and no
filesystem fallback. No other package is an include source. The target must be packaged UTF-8
Markdown within the repository's content-size limit.

`heading` matches exactly one H2-H6 ATX heading outside code fences. Matching compares the visible
heading text after removing the ATX marker and optional closing hashes. H1 imports, zero matches,
and multiple matches are structural errors. Expansion inserts the matching heading and its body
through, but not including, the next heading of equal or higher rank.

Included bytes are inert. The expander does not process directives, frontmatter, or agent-only
markers found in an included document. Includes cannot recurse.

### Live resource projections

```markdown
<!-- agw:resource-kinds -->

<!-- agw:resource-list -->
```

These directives take no arguments. Each calls one direct, read-only adapter over the existing
presentation-neutral service facts used by the corresponding operational CLI command. The guide does
not invoke the CLI as a subprocess, parse rendered command output, or expose a generic service
registry.

The selected shells determine which adapters run. An adapter runs at most once per response and only
when at least one visible directive needs it. A static shell loads no configuration, registry,
database, or other live context. A directive removed by human-mode filtering does not cause its
adapter to run.

The adapters may read only the local facts needed for resource kinds or the configured resource
list. They cannot resolve secrets, inspect unrelated workstation state, access the network, connect
to a VM, perform remote work, mutate Agentworks state, invoke provider probes, or retain a
capability, transport, resolver, database, or callable beyond the request.

## Rendering and failure behavior

For selected shells, rendering proceeds in this order:

1. resolve and structurally validate every requested shell atomically;
2. filter agent-only fences for the selected mode;
3. resolve visible packaged includes;
4. invoke each required live adapter once;
5. replace directives with inert Markdown facts or unavailable placeholders; and
6. sanitize and emit the complete Markdown response.

An environmental failure while reading live resource facts does not fail a valid guide request. The
response emits one sanitized diagnostics warning containing each distinct root failure once, places
a short unavailable marker at every affected directive, renders all remaining content, and exits 0.
One shared failure used by several slots therefore appears once in the warning and produces several
short markers. Diagnostics identify the omitted projection without echoing secrets, raw persisted
values, backend-authored terminal controls, or unsafe exception text.

Structural defects remain nonzero: invalid topic syntax, unknown topics, malformed frontmatter,
invalid filename or heading structure, duplicate slugs, malformed or unbalanced directives,
disallowed include targets, missing or ambiguous include headings, content-bound violations, and
internal invariant failures. Guide success means the requested guidance rendered; it does not assert
installation health.

Final output strips C0 controls except line feed and tab, plus DEL and C1 controls, from authored,
included, projected, placeholder, and diagnostic text.

## Retained independent behavior

The no-topic human and agent trail signs keep their fixed shared destination tuple and return before
shell discovery, include loading, or live adapter construction. The fixed tuple deliberately
duplicates selected-topic slugs so malformed configuration or shell content cannot affect the cheap
entry path.

Mode selection retains this precedence:

1. explicit `--agent` or `--human`;
2. the exact registered `CLAUDECODE=1` execution signature; and
3. human for TTY stdout, otherwise agent.

Exact `concept-release-notes/vMAJOR-MINOR-PATCH` topics are resolved and rendered directly from
bounded packaged changelog sections. They use no `ReleaseNotes` block, generic topic contribution,
or old catalog union. Their content is escaped inert evidence, performs no network work, and uses
neither shell directives nor live adapters. The base `concept-release-notes` guidance is an ordinary
shell.

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
not special: it may use the same two live projections as any other shell, but it has no private
state machine or assessment protocol.

## Structural verification

Focused tests protect behavior and boundaries:

- filename discovery, global slug uniqueness, restricted frontmatter, and the unfenced single-H1
  invariant;
- balanced non-nested agent fences and filtering before include or service work;
- exact unique H2-H6 ATX-section extraction beneath the one `agentworks` package root, size bounds,
  and inert non-recursive included text;
- the two-directive allowlist, lazy once-per-response projection, and no invocation for static or
  human-hidden content;
- one response-level warning, per-slot unavailable markers, exit 0 for environmental failures, and
  nonzero structural failures;
- denial of secret resolution, probes, network, VM, remote-work, mutation, and arbitrary filesystem
  access;
- fixed trail-sign bypass, mode precedence, names-only discovery, completion, and packaged exact
  release evidence; and
- shell and include package-data presence in a built wheel.

Tests use fixture content to assert structure and behavior. They do not pin, blacklist, snapshot, or
otherwise police the wording of repository-authored Markdown, descriptions, warnings, or prompts.
