# HLA: Agentworks Assistance, Discovery, and Management

- Status: Active, Markdown-shell correction
- FRD: `docs/sdd/2026-08-05-onboarding-and-discovery/frd.md`
- Guide LLD: `docs/sdd/2026-08-05-onboarding-and-discovery/guide-contract-lld.md`

The immutable implementation journey is in `plan.md`. Earlier typed-block, action, evidence, and
onboarding-assessment designs are historical; this document states the current architecture.

## Destination

The assistance surface has four layers:

1. One short canonical prompt installs the CLI and hands off to `agw guide --agent`.
2. One fixed no-topic trail sign points toward useful concepts without loading the catalog or state.
3. One auto-discovered catalog renders package-owned Markdown concept shells.
4. Operational CLI commands own execution and machine-readable facts.

```text
canonical prompt
      |
      v
fixed no-topic trail sign
      |
      v
selected concept shell
      +-- inline Markdown
      +-- optional agent-only regions
      +-- optional packaged section imports
      |
      v
current CLI commands for any operation
```

## Fixed trail sign

The no-topic path preserves the approved shared tuple—`concept-assistant-agent`,
`concept-onboarding`, `concept-management`, `concept-troubleshooting`, `concept-release-notes`,
`concept-migration`, `concept-secrets`, and `concept-reporting-bugs`—and returns before catalog
discovery. Human and agent wording may differ briefly, but both modes point to the same concepts and
neither path loads configuration, registry, database, network, or managed resources.

## Shell catalog

The catalog starts at `importlib.resources.files("agentworks")`, walks the installed first-party
package tree, and discovers direct `.md` children of directories named `guide-content`. It never
scans another installed package, the working tree, or candidate code. Core, subsystem, and curated
plugin concepts therefore share one convention instead of a root or topic registry.

Each shell has:

- required frontmatter containing only `description` in the first format version;
- a filename stem that maps to the global `concept-<stem>` slug;
- exactly one H1 outside agent-only regions, used as the title; and
- one Markdown body containing any structural directives.

Global slug collisions and malformed shells are catalog errors. `--names-only` and completion use
the same discovery path without loading configuration or operator state. Exact packaged release-note
subtopics remain a separate direct evidence source because they are version records rather than
authored concepts.

The former first-party plugin topics become globally unique `concept-apt` and
`concept-install-commands` shells. Their old plugin namespace has no compatibility consumer and is
removed. Separately installed plugins do not gain a shell contribution API in this version.

`concept-assistant-agent` owns general assistant-agent posture. Other shells are shared operator
documentation; their agent-only fences contain only local handling context, never generally useful
content hidden from humans.

The canonical repository-root `README.md` is the sole include source outside the normal package
documents. A custom Hatch build hook vendors its exact bytes at
`agentworks/_guide_sources/README.md` for direct wheels and source distributions; a wheel built from
the source distribution uses that already-vendored package copy. A verified repository-layout
fallback reads the canonical root file during editable source execution. Discovery never treats the
mirror as a shell. `concept-core-model` imports its “Architecture at a Glance” and “Core Concepts”
sections. No other repository-root document becomes package data or an include root in this format
version. Shells and include sources are validated and shipped in the same artifact; later repository
edits cannot change an installed guide.

## Shell expansion

Expansion is a fixed pipeline, not a general interpreter:

1. load and validate the selected shell within package-data bounds;
2. remove agent-only regions when human mode is active;
3. expand visible package-section imports as inert Markdown, applying their fixed heading offset;
4. rewrite repository-relative link and image destinations in emitted Markdown; and
5. frame the resulting Markdown.

Agent fences are balanced and non-nested. Imports name a relative Markdown resource beneath the
installed `agentworks` package, an exact H2-H6 ATX heading, and an optional integer heading offset
whose default is zero. The extractor accepts exactly one match outside fenced code and stops at the
next heading of equal or higher rank. It shifts every ATX heading outside fenced code by the same
amount and rejects a result outside H2-H6. Imported text is never parsed again for directives, so
composition is one level deep. Setext headings are rejected in shells and selected sections.

The expander rewrites only repository-relative Markdown destinations. Absolute HTTPS and
fragment-only destinations pass through. Relative destinations are resolved lexically against the
source Markdown resource and required to remain inside its known repository mapping. Links become
canonical GitHub `blob/main` URLs; images become canonical raw-GitHub `main` URLs. Inline and
reference-style forms share this rule and keep their definitions inside the same emitted shell or
extracted section. Relative-link fragments are split before path normalization and then reattached;
query handling is out of scope. The guide never fetches, validates, or embeds remote content.

## Safety and failure behavior

Shell discovery uses trusted package data only. Imports use trusted package data plus the single
canonical root-README fallback in a verified editable checkout. Rendering loads no configuration,
registry, database, resources, secrets, providers, transports, network state, or subprocesses. A
malformed shell, invalid directive, duplicate slug, missing or ambiguous import heading, invalid
topic, or incompatible CLI option remains nonzero. Missing or malformed operator state is irrelevant
to this static path.

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

`packaging/agentworks/assistance.md` remains the canonical bootstrap projected into the README,
website, Claude Code package, and Codex package. It installs the CLI and hands off; it does not grow
guide behavior.

## Verification posture

Tests cover discovery, frontmatter shape, slug collisions, H1 structure, mode filtering, bounded
unique-heading imports, inert imported directives, root-README package inclusion,
repository-relative destination rewriting, no-topic bypass, names-only, and completion. Boundary
tests prove rendering does not load configuration or operational state.

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
- **Removed guide logic returns in frontmatter.** Frontmatter contains description only; actions and
  assessments stay ordinary prose or command-owned behavior.
