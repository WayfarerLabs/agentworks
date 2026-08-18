# HLA: Agentworks Assistance, Discovery, and Management

- Status: Active, shell-backed index and grammar correction
- FRD: `docs/sdd/2026-08-05-onboarding-and-discovery/frd.md`
- Guide LLD: `docs/sdd/2026-08-05-onboarding-and-discovery/guide-contract-lld.md`

The immutable implementation journey is in `plan.md`. Earlier typed-block, action, evidence, and
onboarding-assessment designs are historical; this document states the current architecture.

## Destination

The assistance surface has four layers:

1. One short canonical prompt installs the CLI and hands off to `agw guide --agent`.
2. One reserved index shell frames a concise catalog-derived concept index without loading state.
3. One auto-discovered catalog renders package-owned Markdown concept shells.
4. Operational CLI commands own execution and machine-readable facts.

```text
canonical prompt
      |
      v
shell-backed concept index
      |
      v
`guide show` for one selected concept shell
      +-- inline Markdown
      +-- optional agent-only regions
      +-- optional packaged section imports
      |
      v
current CLI commands for any operation
```

## Shell-backed index

One reserved `_index.md` document in the core guide-content directory owns the no-topic framing and
uses the ordinary shell renderer, including agent-only fencing. It is not an addressable concept.
After rendering that shell, the index renderer appends every ordinary concept carrying
`index-order`, sorted by order and then slug. Equal values are valid. The generated footer reports
the number of ordinary concepts not selected for the index and points to `agw guide list`.

Exact generated release-note topics remain available through `agw guide list` and
`agw guide show TOPIC`, but do not enter the omitted-concept count. Every catalog-backed path
(index, list, show, and topic completion) validates the same complete static catalog atomically; an
unrelated malformed shell therefore prevents each path instead of yielding partial guidance. None of
these paths loads configuration, registry, database, network, or managed resources.

The guide is a normal Typer command group. Its callback renders the index only when no subcommand is
selected. `list` emits the stable name stream. `show` accepts exactly one topic and renders that
shell or exact release section. One group-level `--agent/--human` option selects rendering mode for
both the no-subcommand index and `show`; it may precede either subcommand and has no effect on
mode-independent `list` output. `show` does not duplicate that option. This keeps command completion
structural: Typer owns the group option plus the `list` and `show` verbs, while only `show`'s topic
argument uses dynamic topic completion.

## Shell catalog

The catalog starts at `importlib.resources.files("agentworks")`, walks the installed first-party
package tree, and discovers direct `.md` children of directories named `guide-content`, except the
exact author-facing `README.md` in each such directory. It never scans another installed package,
the working tree, or candidate code. Core, subsystem, and curated plugin concepts therefore share
one convention instead of a root or topic registry.

Each shell has:

- required `description` frontmatter plus one optional bounded non-negative `index-order`;
- a filename stem that maps to the global `concept-<stem>` slug;
- exactly one H1 outside agent-only regions, used as the title; and
- one Markdown body containing any top-level structural directives.

Global slug collisions and malformed shells are catalog errors. `agw guide list` and completion use
the same discovery path without loading configuration or operator state. Exact packaged release-note
subtopics remain a separate direct evidence source because they are version records rather than
authored concepts.

The former first-party plugin topics become globally unique `concept-apt` and
`concept-install-commands` shells. Their old plugin namespace has no compatibility consumer and is
removed. Separately installed plugins do not gain a shell contribution API in this version.

`concept-assistant-agent` owns general assistant-agent posture. Other shells are shared operator
documentation; their agent-only fences contain only local handling context, never generally useful
content hidden from humans.

The canonical repository-root `README.md` and `docs/manifesto.md` are the only include sources
outside normal package documents. A custom Hatch build hook vendors their exact bytes beneath
`agentworks/_guide_sources/` for direct wheels and source distributions; a wheel built from the
source distribution uses those already-vendored package copies. A verified repository-layout
fallback reads the same two canonical files during editable source execution. Discovery never treats
either mirror as a shell. `concept-core-model` imports the README's “Architecture at a Glance” and
“Core Concepts” sections; `concept-manifesto` imports the manifesto's complete H1 section with a
fixed heading offset. No other repository-root document becomes package data or an include root in
this format version. Shells and include sources are validated and shipped in the same artifact;
later repository edits cannot change an installed guide.

## Shell expansion

Expansion is a fixed pipeline, not a general interpreter:

1. load and validate the selected shell within package-data bounds;
2. remove agent-only regions when human mode is active;
3. expand visible package-section imports as inert Markdown, applying their fixed heading offset;
4. rewrite repository-relative link and image destinations in emitted Markdown; and
5. frame the resulting Markdown.

Agent fences are balanced and non-nested. Imports name a relative Markdown resource beneath the
installed `agentworks` package, an exact H1-H6 ATX heading, and an optional integer heading offset
whose default is zero. The extractor accepts exactly one match outside fenced code and stops at the
next heading of equal or higher rank. It shifts every ATX heading outside fenced code by the same
amount and rejects a result outside H2-H6. Imported text is never parsed again for directives, so
composition is one level deep. Setext headings are rejected in shells and selected sections.

Both control forms execute only as exact standalone HTML-comment lines beginning at column zero,
between authored top-level Markdown blocks or sections. Directive-looking comments inside list or
blockquote containers, inside code fences, or with leading or trailing whitespace remain ordinary
Markdown.

The expander rewrites only repository-relative Markdown destinations. Absolute HTTPS, fragment-only,
and empty current-document destinations pass through. Relative destinations are resolved lexically
against the source Markdown resource and required to remain inside its known repository mapping.
Links become canonical GitHub `blob/main` URLs; images become canonical raw-GitHub `main` URLs.
Inline and reference-style forms share this rule and keep their definitions inside the same emitted
shell or extracted section. Relative-link fragments are split before path normalization and then
reattached; query handling is out of scope. The guide never fetches, validates, or embeds remote
content.

## Safety and failure behavior

Shell discovery uses trusted package data only. Imports use trusted package data plus the two exact
canonical README and manifesto fallbacks in a verified editable checkout. Rendering loads no
configuration, registry, database, resources, secrets, providers, transports, network state, or
subprocesses. A malformed shell, invalid directive, duplicate slug, missing or ambiguous import
heading, invalid topic, or incompatible CLI option remains nonzero. Missing or malformed operator
state is irrelevant to this static path.

Filtering precedes expansion. Therefore content hidden from human mode cannot cause an import or
structural failure in that mode.

## Removed architecture

The implementation deletes rather than adapts these guide-owned surfaces:

- `Overview`, `Teaching`, `AgentNote`, `ReleaseNotes`, `ActionList`, and `TopicLinks` block
  assembly;
- `GuideAction`, guide consent records, action validation, and action rendering;
- verification evidence parsing, replay, and the guide CLI's `--evidence` option;
- onboarding snapshots, assessment statuses, evidence transitions, and action selection;
- ordinary per-topic contribution constructors and manual registration; and
- parsers, serializers, render branches, and tests whose only consumer is removed.

Existing useful prose—including commands, expected outcomes, refusal guidance, and related-topic
links—moves into ordinary shells. Operational commands remain the authority for actual behavior.
Suggestions that may cross the operator's current authorization must state scope and impact,
expected result, and refusal alternative in reviewed prose; no new schema enforces those sentences.

## Release notes and bootstrap

The general release-notes concept becomes a shell. Bounded exact-version packaged history remains a
direct inert rendering path, not a typed guide block or shell service, and performs no network work.

`packaging/agentworks/agent-onboarding-prompt.md` remains the canonical bootstrap projected into the
README, website, Claude Code package, and Codex package. It installs the CLI and hands off; it does
not grow guide behavior.

## Verification posture

Tests cover discovery, frontmatter shape, slug collisions, H1 structure, mode filtering, bounded
unique-heading imports, inert imported directives, root-README package inclusion,
repository-relative destination rewriting, reserved index-shell discovery, catalog-derived index
ordering, `guide list`, single-topic `guide show`, and ordinary subcommand/argument completion.
Boundary tests prove rendering does not load configuration or operational state.

Tests do not assert authored wording, duplicate shell prose, or recreate removed schemas in test
fixtures. Permanent CLI documentation describes the shell model. Sample configuration is unaffected.

## Risks

- **The shell format becomes a template language.** The grammar remains closed to two structural
  features and has no generic evaluator or operation registry.
- **Auto-discovery weakens ownership.** Discovery is restricted to explicit trusted package roots,
  and global duplicate slugs fail deterministically.
- **Included docs become executable.** Imported text is inserted inertly and never parsed again.
- **Imported references grow a general URL resolver.** Rewriting is limited to repository-relative
  Markdown link and image destinations under two known source mappings and fixed canonical GitHub
  bases. Remote content and bytes remain outside the guide.
- **Removed guide logic returns in frontmatter.** Frontmatter contains description plus optional
  index ordering only; actions and assessments stay ordinary prose or command-owned behavior.
