# User Perspective

- Status: Initial perspective (operator draft, expanded)
- Date: 2026-08-05
- Baseline: Agentworks 0.13.0 (`v0.13.0`)

## Purpose

This document records the perspective of the people who use and build Agentworks, so the technical
workstreams in this SDD stay anchored to who they serve. It is an input to later requirements and
design work, not a functional specification.

The other perspectives look at the system from the inside out. This one looks from the outside in:
what does the accumulating capability actually feel like to adopt, operate, and extend?

## Personas

The audience roughly breaks down into two personas:

- **Operators** use Agentworks to manage their agentic workloads.
- **Developers** build Agentworks components such as capabilities, plugins, and the core itself.

Developers are almost certainly themselves operators, otherwise they would not be building for
Agentworks. Operators are not necessarily developers, so their needs must be considered separately.

A cross-cutting fact about both personas: they increasingly act through agents. An operator may
point an agent at Agentworks to onboard, configure, or troubleshoot; a developer already builds
Agentworks itself through an agentic process. Every surface this perspective asks for should
therefore be consumable by humans and agents alike, which in practice means a discoverable CLI,
machine-readable output, and shipped skills that teach an agent the same things the docs teach a
human. That "skills plus CLI" pattern recurs through every operator need below; it is a single
investment that serves all of them.

## Operator Needs

### Onboarding

Agentworks already has a ton of functionality (and complexity). Initial feedback from new operators
is that it is a lot to take in.

The delivery model to design for (plan A, and possibly all we need): an operator already has Claude
Code, Codex, or another harness on their workstation and is looking for a more robust way to do
their work. They should be able to use that vanilla, non-Agentworks-managed harness to set
Agentworks up and manage it. Concretely, Agentworks publishes harness-specific plugins or
marketplace entries in its own repository; the operator adds one straight from GitHub, asks their
agent to set up Agentworks, and the agent executes the onboarding process described in the skill,
installing, configuring, and using the CLI as needed.

An important boundary makes this the right shape: an Agentworks-managed agent is not the onboarding
vehicle, because we do not want an agent modifying the system it is running in. The onboarding agent
deliberately sits outside Agentworks, so the agentic-artifacts layer (which delivers content into
managed sessions) is not the delivery path for onboarding skills; harness-native plugin channels
are.

Critical aspects of the experience itself:

- It must hit all the major capabilities of Agentworks: config, resources, plugins, secrets, VMs,
  sessions, and the rest.
- It must be dynamic, so that as new functionality is added it is automatically incorporated into
  the onboarding experience rather than depending on a hand-written tour that goes stale. The
  natural source of truth is the same registries, schemas, and samples the declarative-schema work
  makes authoritative.
- It must support interactive and non-interactive paths. Some operators want a guided experience;
  others want to script their onboarding or replay it across machines.
- It should support both human and agentic onboarding. A robust set of skills and the appropriate
  CLI commands could go a long way: an operator points their agent of choice at the onboarding
  process and the agent walks them through it, or does it for them.
- It should be progressive. The golden path (a first working session) should be minutes, with the
  deeper capabilities discoverable from there rather than front-loaded.
- It must be idempotent and rerunnable. Onboarding is not a one-shot wizard; operators should be
  able to revisit it at any time to confirm they are getting the most out of Agentworks as it
  evolves, with already-done steps recognized rather than redone.
- It must be conspicuously respectful of security. Probing the operator's machine happens with
  consent and explanation, never silently: for example, ask before looking for existing SSH keys,
  and make clear that key contents will not be read, rather than blindly scanning `~/.ssh`.
  Onboarding is where trust is established, and the experience should demonstrate the platform's
  security posture from the first minute.

### Capability Discovery

We already have a number of interesting capabilities, and more will be added. Operators need to
discover what exists, understand what each capability is for, and see how to adopt it.

Like onboarding, discovery should be derived rather than curated: the capability-kind descriptor and
registry work gives the framework an authoritative inventory of kinds and implementations, and the
describe surfaces and live samples from the declarative-schema work give each entry usable
documentation. The same skills-plus-CLI approach applies: `agw` should be able to answer "what can
you do and how do I use it" well enough that an agent can answer it too.

`agw doctor` belongs to this story as the discovery surface for "what is configured, what is ready,
and what is wrong," and should stay coherent with the inventory surfaces rather than evolving
separately.

### Schema Discovery

Virtually everything in Agentworks has schema, whether core objects or capabilities. Operators need
to understand the possible schemas of the resources they work with and get help authoring and
changing their own declarations.

This need is served directly by the declarative-schema effort: registration-time models emitting
JSON Schema, live-rendered samples, and a describe surface mean schema help cannot drift from
behavior. The operator-facing forms to consider:

- Editor integration (emitted JSON Schema wired into YAML editing) for humans.
- CLI describe and sample commands for both humans and agents.
- Validation errors that teach: source-located, with the canonical shape shown, not just rejected.

### Session Visibility

Operators need to know what their agents did and are doing: a durable transcript to review after the
fact, live observation without disturbing the session, and eventually limited structured control
from secondary clients (including mobile). This is the observability track's charter, restated as an
operator need. The memory-learning loop matters here too: operators benefit when what one session
learned is distilled and made available to future sessions, through a reviewed path they control.

### Resource Stewardship

Operators run fleets of VMs that cost money and attention. They need visibility into what is running
versus idle, and they should not have to babysit shutdown. The concrete ask: a platform feature
where a VM can be configured to auto-suspend itself after a period of inactivity, with the session
event stream as the general activity signal. Liveness heartbeats do not count as activity; "the
workload is alive" and "the operator or agent is doing something" are different facts, and
auto-suspend keys on the latter.

### Upgrade Experience

Agentworks is evolving fast and shipping breaking releases. Operators need that to be routine rather
than scary: a deprecation runway with clear warnings, doctor reporting of everything deprecated in
their setup, mechanical migration tooling where shapes change, and release notes that tell them
exactly what to do. The 0.14 cleanup release is the immediate test case, including the surfaces
being retired that never warned at all.

### Safety and Trust

Operators are handing real credentials and real repositories to agentic workloads. They need the
platform's guarantees stated honestly and kept: scope containment (a session operation never mutates
upstream resources), secrets never persisted into transcripts, artifact state, or resolved
configuration, drift detected and reported rather than silently overwritten, and best-effort
observation described as best-effort rather than oversold as audit.

## Developer Needs

There are already several efforts aimed at developer needs. Consolidating the list:

- **Well-designed, well-documented APIs and data models.** Documentation generated from the models
  themselves (schemas, samples, describe surfaces) so it cannot drift from behavior.
- **A "trust but verify" approach to new functionality.** The framework should confirm that a
  contribution conforms to its expectations at registration time: kind conformance, required
  metadata, constructibility, operations, and schema/API version compatibility, with guard tests and
  doctor checks backing it at runtime.
- **Cheap extension.** Adding an implementation, and eventually a kind, should be a registration
  against a descriptor plus a config model, not a coordinated edit across adapter tables, graph
  stamps, publishers, and snapshot logic. Contract test kits per extension point let a developer
  prove conformance locally.
- **Fast, honest feedback.** Shared fixture helpers so schema and surface changes do not mean
  bespoke per-file test churn (phase 1's lesson), and gates that fail loudly and early.
- **A clear internal/public boundary.** Which contracts are stable and versioned versus internal and
  movable, stated explicitly, with the external plugin API promised only when it is real.
- **An agent-first development experience.** Agentworks is itself built through agentic development;
  the rules, skills, and SDD process are part of the developer surface and deserve the same care as
  code. What we ship to operators as skills-plus-CLI, we should be dogfooding as developers.
- **Observability as a debugging tool.** The same event stream operators use to review sessions
  gives integration developers ground truth for what their integration actually observed and did.
- Probably other things this list is still missing; treat it as a floor, not a ceiling.

## Questions for the Remaining SDD Artifacts

- What concrete form does onboarding take (a guided command, a generated tour, a docs path, some
  combination), and where does it live?
- How exactly does dynamic onboarding derive from registries, schemas, and samples so it cannot go
  stale, and what is the authoring model for the connective narrative it still needs?
- Which CLI surfaces need machine-readable output guarantees for agentic use, and is that a
  per-command decision or a global output-mode contract?
- Which harnesses get first-party onboarding plugins or marketplace entries first, and how do those
  plugin versions track CLI releases?
- What consent vocabulary and probing rules make onboarding trustworthy: what may be looked for,
  what is never read, what is always asked first?
- What does "trust but verify" require at registration time versus runtime, and how much of it is
  the descriptor work versus the plugin-API work?
- How are the onboarding and discovery efforts sequenced against the schema-emission and descriptor
  work they want to consume?
- What feedback loop tells us onboarding and discovery are actually working for new operators?
