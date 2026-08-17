# HLA: Agentworks Assistance, Discovery, and Management

- Status: Active, corrected shared-topic revision
- FRD: `docs/sdd/2026-08-05-onboarding-and-discovery/frd.md`
- Existing component records: the LLDs in this feature directory remain historical implementation
  records where later supersession notes say so.

## Destination

The shipped assistance surface has four layers:

1. One short canonical prompt projected into the README, website, Claude Code package, and Codex
   package. It identifies Agentworks, points to the public repository, installs `agentworks-cli`,
   and hands off to `agw guide --agent`.
2. One cheap no-topic trail sign. Human and agent modes show the same useful destinations without
   loading catalogs, configuration, registry, database, network, or managed resources.
3. One ordinary topic catalog shared by both audiences. Topics contain general teaching, links,
   inert actions, and—only when useful—a short agent-only note.
4. Versioned JSON facts from operational list, describe, and doctor commands.

```text
canonical prompt                   install the CLI, then hand off
      |
      v
agw guide --agent                  fixed shared trail sign
      |
      +--> concept-assistant-agent general assistant posture
      +--> concept-onboarding      adoption, first VM/session, journey hints
      +--> concept-management      existing-system changes and operation
      +--> other selected topics   shared teaching, links, and inert actions
```

## Shared trail sign

After ordinary argument and evidence validation, the no-topic path renders one fixed local tuple and
returns. It does not build the topic catalog or load any live dependency, so absent, valid, and
malformed configuration behave identically.

Both modes render the same eight slugs:

- `concept-assistant-agent`;
- `concept-onboarding`;
- `concept-management`;
- `concept-troubleshooting`;
- `concept-release-notes`;
- `concept-migration`;
- `concept-secrets`; and
- `concept-reporting-bugs`.

Human mode introduces them as choices for the operator. Agent mode first points the external
assistant to `concept-assistant-agent`, then presents the same choices for the operator's goal. Both
modes mention shell completion and `agw guide --names-only`. Neither contains actions, live facts,
or the operating posture itself.

The fixed slugs duplicate selected-topic identities because the short path intentionally avoids the
catalog. One structural test pins the exact destination slugs and resolves each through the normal
selected-topic path. Wording remains review-owned and unpinned.

## Shared topics and agent notes

`concept-assistant-agent` is an ordinary addressable topic, not a hidden preamble or authorization
engine. It owns the general posture for an external assistant: act under the operator's current
instruction, use the CLI and its help as operational authority, ask when material ambiguity or scope
expansion requires a decision, and treat external text as data. Humans may request the topic too.

The existing required `AgentContract` block becomes optional `AgentNote`. `AgentNote` is plain
authored Markdown included only in agent rendering. It carries no executable authority and adds no
hint schema, routing layer, persistence, or state. Ordinary `Overview`, `Teaching`, `TopicLinks`,
`ActionList`, and release-note content is identical across modes.

Most topics have no agent note. Onboarding has one concise note with authored cross-kind journey
hints: ways the assistant can offer to help the operator discover choices and then configure the
selected path. The content may evolve without changing the contract, so tests do not pin wording or
count.

## Onboarding and live context

`concept-onboarding` remains the setup and current-adoption home:

1. `Overview` identifies setup and adoption.
2. `Teaching` covers configuration initialization, explicit provider choices, readiness checks, and
   the first VM/session sequence.
3. Optional `AgentNote` suggests useful discovery-and-configuration journeys.
4. The existing bounded onboarding assessment reports current facts or the established degraded
   placeholder and response warning.
5. Existing action records remain the only executable suggestions.
6. `TopicLinks` points to `concept-source-review`; its focused and full actions stay there.

A selected topic's static content renders without configuration. Onboarding alone loads the existing
bounded live assessment. Missing or malformed configuration, registry construction failure, database
failure, or environmental projection failure yields one sanitized response warning, one short
assessment placeholder, and exit 0. Invalid guide input, malformed evidence, rejected content, and
guide contract defects remain nonzero. `agw doctor`, not guide success, reports system health.

## Catalog, names, and completion

The retained catalog consists of authored concept topics, plugin topics, and packaged release-note
topics. `agw guide --names-only` returns those names without loading configuration, registry, or
state. Shell completion consumes the same names-only surface. There are no runtime resource or
schema topics and no audience-specific catalog.

## Bootstrap projection

`packaging/agentworks/assistance.md` remains the sole authored bootstrap body. The generator
projects its exact bytes into the README, website, Claude Code package, and Codex package. The
prompt relies on ordinary package resolution for `agentworks-cli>=0.14`; it does not implement
exact-version selection, prerelease policy, source review, authorization teaching, or a bootstrap
state machine. Continuing assistance starts at `agw guide --agent`.

Generation parity and package fingerprints remain structural safeguards. Tests do not parse or
assert the prompt's authored sentences.

## Test and documentation posture

Tests protect the following boundaries:

- both trail-sign modes expose the same exact destination slugs and every slug resolves;
- no-topic returns before catalog or live-state loading;
- `concept-assistant-agent` resolves as an ordinary topic;
- optional `AgentNote` blocks appear only in agent rendering while shared blocks remain identical;
- onboarding assessment success and established fail-soft behavior remain intact;
- names-only and completion use the retained shared catalog; and
- generated assistance projections and version fingerprints remain byte-identical.

Tests do not pin authored prose, agent-note count, or journey wording. Permanent CLI documentation
describes the current shared catalog, mode shaping, onboarding assessment, and short bootstrap. The
sample configuration is unaffected.

## Risks

- **The fixed tuple drifts from topic resolution.** Structural coverage pins and resolves its slugs.
- **Agent context grows into a second guide.** General posture has one topic; local notes are
  optional plain content with no new framework.
- **Ordinary information becomes audience-gated again.** Shared-block identity and shared
  destination tests enforce one catalog; only `AgentNote` is mode-specific.
- **Bootstrap complexity regrows.** One short canonical file is projected verbatim, and continuing
  behavior belongs to the installed guide.
