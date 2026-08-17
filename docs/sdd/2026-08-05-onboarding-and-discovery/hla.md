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
      +-- optional resource-kinds/resource-list projections
      |
      v
current CLI commands for any operation
```

## Fixed trail sign

The no-topic path preserves the approved shared eight-slug tuple and returns before catalog
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
the same discovery path without loading configuration or live services. Exact packaged release-note
subtopics remain a separate direct evidence source because they are version records rather than
authored concepts.

## Shell expansion

Expansion is a fixed pipeline, not a general interpreter:

1. load and validate the selected shell within package-data bounds;
2. remove agent-only regions when human mode is active;
3. expand visible package-section imports as inert Markdown;
4. invoke visible named live projections lazily;
5. frame the resulting Markdown and any deduplicated diagnostics.

Agent fences are balanced and non-nested. Imports name a relative Markdown resource beneath the
installed `agentworks` package plus an exact H2-H6 ATX heading. The extractor accepts exactly one
match outside fenced code and stops at the next heading of equal or higher rank. Imported text is
never parsed again for directives, so composition is one level deep.

Only two live directives exist: `resource-kinds` and `resource-list`. Their implementations call
presentation-neutral service functions already behind the corresponding CLI surfaces. There is no
generic operation registry, parameter expression language, subprocess invocation, or extension hook.

## Safety and degradation

Shell discovery and static imports use trusted package data only. Live projections are read-only and
probe-suppressed. The guide cannot resolve a secret, contact a provider, connect to a VM, perform
remote work, or mutate state.

An environmental projection failure becomes one sanitized response-level warning per root error and
a short placeholder at each affected directive. Static content and unrelated projections still
render, and a valid request exits 0. A malformed shell, invalid directive, duplicate slug, missing
or ambiguous import heading, invalid topic, or incompatible CLI option remains nonzero.

Filtering precedes expansion. Therefore content hidden from human mode cannot cause an import, live
read, warning, or error in that mode.

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

## Release notes and bootstrap

The general release-notes concept becomes a shell. Bounded exact-version packaged history remains a
direct inert rendering path, not a typed guide block or shell service, and performs no network work.

`packaging/agentworks/assistance.md` remains the canonical bootstrap projected into the README,
website, Claude Code package, and Codex package. It installs the CLI and hands off; it does not grow
guide behavior.

## Verification posture

Tests cover discovery, frontmatter shape, slug collisions, H1 structure, mode filtering, bounded
unique-heading imports, inert imported directives, the two-operation allowlist, lazy invocation,
fail-soft framing, no-topic bypass, package inclusion, names-only, and completion. Boundary tests
deny secrets, network, transports, probes, mutation, and subprocess execution.

Tests do not assert authored wording, duplicate shell prose, or recreate removed schemas in test
fixtures. Permanent CLI documentation describes the shell model. Sample configuration is unaffected.

## Risks

- **The shell format becomes a template language.** The grammar remains closed to three structural
  features and has no generic evaluator or operation registry.
- **Auto-discovery weakens ownership.** Discovery is restricted to explicit trusted package roots,
  and global duplicate slugs fail deterministically.
- **Included docs become executable.** Imported text is inserted inertly and never parsed again.
- **Live guidance repeats failures.** Root diagnostics are response-scoped and deduplicated; each
  slot carries only a short placeholder.
- **Removed guide logic returns in frontmatter.** Frontmatter contains description only; actions and
  assessments stay ordinary prose or command-owned behavior.
