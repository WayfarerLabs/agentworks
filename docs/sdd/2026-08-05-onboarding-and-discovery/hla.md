# HLA: Agentworks Assistance, Discovery, and Management

- Status: Active, trail-sign revision
- FRD: `docs/sdd/2026-08-05-onboarding-and-discovery/frd.md`
- Existing component records: the LLDs in this feature directory. This HLA supersedes their no-topic
  presentation claims; their landed component contracts remain historical implementation records.

## Destination

The shipped system has four layers:

1. One canonical assistance prompt, projected into the README, website, Claude Code package, and
   Codex package.
2. A short no-topic `agw guide` trail sign.
3. Selected guide topics containing authored teaching, safe live facts, and inert action records.
4. Versioned JSON facts from operational list, describe, and doctor commands.

This round changes the boundary between layers 2 and 3. Teaching that accumulated in the no-topic
response moves to `concept-onboarding`; the no-topic response becomes a cheap signpost.

```text
canonical prompt
      |
      v
agw guide --agent              no config or live-system work
      |
      +--> concept-onboarding  startup, adoption, first VM/session
      +--> concept-management  existing-system changes and operation
      +--> other concept topic selected for the current goal
                    |
                    v
           safe facts + inert actions
```

## Trail-sign rendering

After normal argument and evidence validation, the ordinary no-topic path renders a fixed set of
core destination slugs. It bypasses both `build_authored_catalog()` and `_build_schema_catalog()`;
it does not load configuration, construct a runtime registry, open the state database, build a
`GuideView`, or enumerate dynamic kind, implementation, resource, or historical-release topics.

Its small destination set is reviewed authored content:

- first setup or current adoption: `concept-onboarding`;
- ongoing configuration or operation: `concept-management`;
- diagnosis: `concept-troubleshooting`;
- changes between versions: `concept-release-notes`;
- exceptional input conversion: `concept-migration`;
- secret handling: `concept-secrets`;
- product defects: `concept-reporting-bugs`.

The response also points to shell completion and `agw guide --names-only` for the exhaustive topic
inventory. Exact kind, implementation, resource, source-review, manifesto, and historical-release
topics remain addressable without becoming top-level choices.

Agent and human modes share these destinations. Agent mode briefly tells the Agentworks assistant
agent to choose the smallest relevant topic for the operator's request. Human mode gives the
operator a direct starting instruction. Neither form contains the operating contract, source-review
actions, live facts, or an exhaustive index.

The current service builds catalogs and live state before it knows that no selected topic needs
them. The one required machinery change is an early no-topic return after argument and evidence
validation. This is a deletion of unnecessary work, not a new abstraction. The implementation must
coordinate this shared service edit with the parallel simplification pass and must not touch its
contract-validation, projection, or machine-output findings.

`--names-only` keeps its current exhaustive behavior because it serves completion. Selected-topic
requests also keep current catalog resolution and live projection. Only the ordinary no-topic render
short-circuits.

## Onboarding organization

`concept-onboarding` becomes the sole startup walkthrough. Its existing blocks are reorganized, not
replaced by a new topic or state machine:

1. `Overview` identifies first setup and current-adoption assessment.
2. `AgentContract` carries the concise startup posture and authorization guidance removed from the
   no-topic renderer.
3. `Teaching` presents discovery, configuration initialization, explicit provider choices, readiness
   verification, and the first VM/session sequence.
4. Existing live inventory and assessment blocks report ready, disabled, not-ready, and unverifiable
   facts.
5. Existing action records remain the only executable suggestions.
6. Related topics include `concept-source-review`; the full focused and full review actions remain
   on that selected topic and are not copied into onboarding.

The selected onboarding topic remains useful without configuration: authored blocks render, live
facts report unavailable, and missing configuration remains a successful state to resolve. It does
not probe the workstation or run doctor while rendering.

## Selected-topic architecture stays unchanged

The existing contracts remain authoritative:

- `TopicContribution` and the closed block vocabulary carry inert contributed data.
- `GuideView` exposes only already-materialized, sanitized facts and no powers.
- kind and implementation pages reuse schema-reference and sample services;
- resource pages reuse stored and finalized facts;
- `GuideAction` records are inert, scoped, verifiable, and refusal-aware;
- release notes come from the packaged canonical changelog;
- JSON v1 remains separate from markdown guide output.

No new block, action, consent, evidence, catalog, template, or state type is required. The parallel
simplification pass may remove internal machinery under these public behaviors; this round does not
preempt that work.

Each selected concept topic also stands alone under the operator's current instruction. Directly
requesting management, troubleshooting, migration, secrets, release notes, or bug reporting does not
require a prior no-topic or onboarding response to establish a separate startup envelope.

## Remaining phases

The old registry-inventory phase no longer earns a separate implementation phase. Earlier work
already supplies dynamic kind and implementation inventory, specific-resource topics, and
fixture-driven catalog updates. The trail sign deliberately stops presenting those exhaustive facts
at top level while leaving them available after selection.

Closeout uses one provider-backed golden path from an exact reviewed candidate wheel and labels that
pre-publication substitution. The stable bootstrap cannot truthfully install a release that does not
yet exist. After publication, a bounded canonical-prompt smoke installs the exact stable release and
reaches the trail sign. The README and native packages project one canonical prompt, so package
generation and focused install probes establish wrapper parity. Repeating the same costly VM/session
journey through every wrapper would add test ceremony rather than confidence.

## Test and documentation posture

Tests protect behavior and structure, not authored wording. Required coverage is limited to:

- no-topic returns before live-system construction;
- missing configuration succeeds for no-topic and onboarding;
- selected topics, completion, and `--names-only` retain the full catalog;
- a direct ongoing topic works without first rendering no-topic or onboarding;
- source-review action ownership does not move;
- selected-topic live projection and action behavior remain unchanged;
- one real golden path reaches a VM and started session.

Permanent CLI documentation explains the trail sign, the onboarding destination, and exhaustive
topic discovery. No completion change is expected because topic names do not change. No sample
configuration change is expected because no setting is added.

## Risks

- **The short path accidentally weakens topic discovery.** Keep exhaustive names in completion and
  `--names-only`; only presentation changes.
- **Startup guidance disappears instead of moving.** Review `concept-onboarding` as a complete page
  before deleting the no-topic contract.
- **The parallel cleanup changes shared guide files.** Coordinate the one service-flow edit and
  rebase before implementation; do not duplicate or preserve machinery for branch compatibility.
- **A trail sign grows back into an overview.** Keep every detailed instruction in its selected
  topic. A new top-level sentence must help choose a destination or it does not belong.
