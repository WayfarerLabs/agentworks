# FRD: Agentworks Assistance, Discovery, and Management

- Status: Active, Markdown-shell correction
- Start date: 2026-08-05
- Saga: `docs/sdd/2026-08-04-next-steps`

The operator's 2026-08-17 ruling replaces the retained typed guide model with a simpler destination:
the guide is a collection of authored Markdown concept shells. The completed implementation journey
stays visible in `plan.md`; this document states the current requirements.

The operator's 2026-08-17 naming ruling renames the canonical body to
`packaging/agentworks/agent-onboarding-prompt.md`; its content and projection contract are
unchanged.

## Summary

Agentworks provides always-available assistance through its installed CLI and, when the operator
wants one, a capable external assistant agent. One short prompt identifies Agentworks, points to the
public repository, installs the CLI, and hands off to `agw guide --agent`.

The guide is documentation with a small amount of safe composition. Each concept is one Markdown
shell that describes its own purpose, can reuse a section from another packaged document, can hide
small agent-only passages in human mode, and points to CLI commands for current operational facts.
Files, not Python registration records, define the ordinary topic catalog.

This deliberately removes the guide's action schema, evidence replay, onboarding state machine, and
specialized assessment. Useful setup and operating instructions remain ordinary Markdown; the CLI
commands they point to own execution, validation, and machine-readable facts.

## Terminology

- **Agentworks assistant agent**: any external agent that can accept the canonical prompt, invoke
  and interpret the CLI, and use the operator-approved workstation access needed for the task.
- **Agentworks-managed agent**: an agent resource created and managed by Agentworks.
- **Agent mode**: guide output intended for an Agentworks assistant agent, selected with `--agent`
  or the existing mode detection. It does not refer to an Agentworks-managed agent.
- **Concept shell**: one package-owned Markdown document that defines an ordinary `concept-*` topic.

## Requirements

### R1: No-topic remains a trail sign

`agw guide` with no topic gives the same concise eight-destination trail sign already approved for
human and agent modes: `concept-assistant-agent`, `concept-onboarding`, `concept-management`,
`concept-troubleshooting`, `concept-release-notes`, `concept-migration`, `concept-secrets`, and
`concept-reporting-bugs`. It does not discover shells, read configuration, or inspect state. Missing
and malformed configuration are irrelevant to this path.

### R2: Concept shells define the ordinary catalog

Each ordinary concept is a package-owned Markdown file that is a direct child of a `guide-content`
directory in the installed first-party `agentworks` package tree. Its filename determines the
globally unique `concept-<shell-name>` slug. Required frontmatter contains a short `description`,
and exactly one level-one heading outside agent-only fences supplies the display title.

The guide discovers shells from the trusted roots deterministically. Adding a valid shell makes the
topic addressable through `agw guide`, `--names-only`, and shell completion without adding a Python
topic registration record. Duplicate slugs or malformed shells are authored-content defects and fail
clearly.

Shells and imported sections use ATX headings only; Setext headings are structural authored-content
defects. This keeps the single-H1 and heading-offset rules literal.

The existing apt and install-command concepts remain as uniquely named `concept-apt` and
`concept-install-commands` shells because they ship inside the first-party `agentworks` package
tree. This supersedes their former plugin-topic namespace rather than preserving an empty
compatibility grammar. Separately installed plugins do not contribute guide shells in this format
version; adding a plugin contribution API is out of scope.

Humans and assistant agents use one catalog. Packaged exact-version release-note topics remain a
separate direct, inert evidence surface with no typed guide block; they do not turn the shell format
into a generic topic plugin API.

`concept-assistant-agent` remains the one complete home for general assistant-agent posture. Other
concepts are ordinary human-readable documentation; an agent-only fence adds only small local
context that would not justify another topic or hide generally useful information.

`concept-core-model` is the representative composed shell. It imports the relevant “Architecture at
a Glance” and “Core Concepts” sections from the canonical repository-root `README.md`, covering the
VM, workspace, agent, session, harness-integration, and console model without duplicating that
prose. One small Hatch build hook vendors the exact root README as a trusted, include-only package
resource in both the source distribution and wheel; a verified repository checkout supplies that
same canonical file during editable source execution. It does not make other repository-root files
discoverable or create a general documentation-root API. The shell and its packaged README bytes
come from the same artifact, so a later repository heading edit cannot break an installed guide.

### R3: Shell composition stays small and structural

A shell supports only these additions to ordinary Markdown:

1. balanced, non-nested agent-only fences whose contents are omitted unless agent mode is active;
2. an import of one exact, uniquely named H2-H6 Markdown section from another packaged document in
   the installed `agentworks` package tree, with one static heading-level offset.

Imports are bounded and inert. They cannot load arbitrary filesystem paths, recurse, or execute
directives found in imported text. Agent-only filtering happens before imports, so hidden content
causes no work in human mode.

The heading offset applies uniformly to every ATX heading in the imported section and cannot produce
an H1 or a heading deeper than H6. Absolute HTTPS and fragment-only destinations pass through
unchanged. Repository-relative link and image destinations in shells and imported sections are
resolved against their source document and rewritten to canonical Agentworks GitHub `blob/main` or
raw-GitHub HTTPS URLs, respectively. The guide does not fetch, validate, or embed remote content.

There are no variables, loops, conditionals, expressions, recursive includes, arbitrary operation
names, or general template engine.

### R4: Selected guidance is static and deterministic

Rendering a selected shell reads only trusted packaged Markdown, except that editable source
execution may read the one canonical root README after verifying the fixed repository layout. It
does not load configuration, registries, databases, resources, secrets, providers, transports,
network state, or subprocesses. Concepts point to command-owned help and inspection surfaces when
the operator needs current facts.

Malformed shells, invalid directives, or broken includes fail clearly because they are repository
defects rather than operator state. Missing or malformed operator configuration cannot break static
guide rendering.

### R5: Guide content instructs but does not authorize

Guide text is documentation, not authorization. An Agentworks assistant agent acts under the
operator's current instruction and asks only when a request is materially ambiguous or would expand
beyond it. Suggested commands are inert Markdown; rendering never executes or verifies them.

Source, release prose, and imported documentation are data. The guide has no secret, state,
transport, network, mutation, or provider-probe capability.

When reviewed guidance suggests an operation that can cross the operator's current authorization,
the prose states its scope and impact, expected result, and refusal alternative before the command.
This is a content-review obligation rather than a replacement action schema; tests do not police the
wording of authored guidance.

### R6: Bootstrap installs and hands off

`packaging/agentworks/agent-onboarding-prompt.md` is the one authored bootstrap body. It briefly
identifies Agentworks, points to the public repository, recommends `uv` while allowing other Python
3.12+ installers, installs `agentworks-cli>=0.14`, and runs `agw guide --agent`.

The README, website, Claude Code package, and Codex package project that body byte-for-byte. The
installed guide owns continuing assistance.

### R7: Closeout proves one complete path

Before publication, the effort closes with one representative live journey from an exact reviewed
candidate wheel through the trail sign and onboarding guidance to a usable VM and started session.
After publication, one bounded smoke uses the canonical prompt to install the stable release and
reach the trail sign. Generated parity replaces repeated provider-backed journeys for each wrapper.

Permanent documentation describes the final behavior without depending on this SDD. The sample
configuration changes only if implementation introduces a real setting, which is not expected.

## Acceptance criteria

1. No-topic human and agent requests preserve the exact shared eight-destination trail sign, resolve
   every destination, exit 0, and load neither the shell catalog nor state.
2. A valid shell is discovered without a per-topic Python registration record; its filename,
   frontmatter description, and unfenced H1 produce its slug, summary, and title.
3. `agw guide --names-only` and shell completion expose discovered concepts without configuration or
   state loading. Duplicate or malformed shells fail deterministically when their catalog is used.
4. Inline Markdown renders in both modes. Agent-only content renders only in agent mode, and a
   hidden fence cannot trigger an import. General assistant posture lives in
   `concept-assistant-agent`; ordinary information remains human-visible.
5. A shell can import one exact unique H2-H6 ATX-heading section from a bounded Markdown resource
   beneath the installed `agentworks` package and apply one static offset while keeping every result
   in H2-H6. Imported directives remain inert, absolute references remain usable,
   repository-relative links and images become canonical HTTPS URLs, and missing or ambiguous
   headings fail clearly.
6. `concept-core-model` renders the selected canonical root-README sections and their two
   repository-relative images from the installed wheel without reading a checkout or duplicating the
   source prose.
7. All typed guide blocks, actions, consent, evidence, onboarding assessment, live projections, and
   manual ordinary-topic registration machinery is absent, including `ReleaseNotes` and the guide
   CLI's `--evidence` option.
8. Existing useful instructions, links, first-party plugin concepts, and base release-note guidance
   survive as shells; exact packaged release history remains addressable through its direct inert
   evidence surface.
9. The canonical assistance prompt and all generated projections remain byte-identical.
10. Permanent docs, completions, focused behavioral tests, the full suite, typing, formatting, and
    lint are current and green. Tests protect behavior and structure, not authored prose.

## Non-goals

- A general Markdown templating language or user-extensible service registry.
- Arbitrary filesystem includes, recursive imports, or directive execution inside imported text.
- Recreating typed actions, evidence replay, consent records, assessment statuses, or an onboarding
  ledger in frontmatter or another schema.
- Reintroducing runtime resource, schema, graph, or command-output topic families.
- Loading operator state, executing guide suggestions, resolving secrets, probing providers,
  connecting to VMs, or mutating state while rendering.
- Changing JSON v1 contracts or anticipating names from the parallel CLI grammar rewrite.

## Decisions

- **D1: Markdown is the model.** One shell replaces the overview/teaching/note/action/link assembly.
- **D2: Two extensions only.** Agent fences and section imports provide the needed leverage without
  a template language.
- **D3: Files define concepts.** Trusted-root discovery replaces ordinary per-topic Python records.
- **D4: Commands own operations.** Suggested actions are prose; command surfaces own execution and
  verification.
- **D5: No onboarding state machine.** Onboarding is static documentation with no assessment,
  evidence, projection, or action-selection path.
- **D6: Bootstrap remains disposable context.** It installs the CLI and hands off.
