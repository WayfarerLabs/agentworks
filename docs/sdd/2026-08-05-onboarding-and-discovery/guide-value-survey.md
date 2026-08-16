# Guide Value Survey

Status: reviewed survey with operator disposition, 2026-08-15. Implementation remains separate.

## Result

At commit `b3330b1f`, the guide has 200 fixed block instances plus four blocks repeated for every
configured resource. Of the fixed instances, 59 earn their place, 49 have settled command owners
now, and 92 have partial, temporary, or not-yet-shipped command owners. The operator approved
removing all 141 command-duplicating fixed blocks, the dormant kind fallback, and the runtime
resource topics in one effort before the CLI grammar rewrite. The resulting unreleased gap on `main`
is accepted; 0.14.0 remains gated on the settled command replacements.

| Classification     | Fixed blocks | Meaning                                                              |
| ------------------ | -----------: | -------------------------------------------------------------------- |
| Keep               |           59 | Concept, workflow synthesis, navigation, evidence, or bounded action |
| Cut: current owner |           49 | Raw facts already owned by a settled command                         |
| Cut: release-gated |           92 | Raw facts whose settled replacement belongs to the CLI grammar child |

The cuttable surface is large: 141 of 200 fixed block instances, plus every configured-resource
topic, are command output wearing guide clothes or collateral to those views. The code prize is
smaller than that ratio suggests because the blocks are generated from shared patterns, and the
onboarding assessment still needs a reduced fact projection. It is nevertheless substantial: five of
eleven block variants, the schema renderer, the generic resource-topic renderer, and most of
`GuideView` can leave after the smaller onboarding projector reaches behavioral parity.

## Rubric and scope

- **Keep** content that teaches a concept, combines facts into a conclusion, routes between
  concepts, presents packaged release evidence, or records an operation with its authorization and
  refusal paths.
- **Cut: current owner** for a raw listing, state, schema, or sample when a settled command owns the
  answer.
- **Cut: release-gated** for the same content when its current owner is incomplete, scheduled for
  removal, or not yet shipped. `resource describe-kind` currently owns overview and field facts but
  becomes `resource explain`; `resource describe` currently owns instance and inbound relationship
  facts but is removed; and `graph show KIND/NAME` becomes the settled relational view.
- A block is judged as a whole. By operator ruling on 2026-08-15, a raw aggregation of several lists
  is not higher-level synthesis; it is cut when each row already has a command owner.
- Both cut classes execute in one guide-deletion effort. The distinction records command coverage,
  not implementation phases. The temporary gap is permitted only on unreleased `main`; 0.14.0 does
  not ship before the CLI grammar child restores the settled destinations.
- The no-topic trail sign, response framing, unavailable placeholders, and headings are renderer
  structure rather than `GuideBlock` instances. They are outside the count and remain.

The 200-block count consists of 48 concrete authored blocks, 13 generated historical release-note
blocks, 138 schema-generated blocks, and the derived onboarding plan. Runtime resource topics add
four blocks per configured resource. Current registration has 14 declarable kinds, four capability
kinds, and 14 capability implementations.

## Authored block inventory

This table has one row for every concrete authored block. Action counts describe records inside the
single `ActionList` block; they are not additional blocks.

| Topic                             | Block            | Decision           | Command replacement or reason                                                                                                                                                                             |
| --------------------------------- | ---------------- | ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `concept-management`              | `overview`       | Keep               | Resource-management concept                                                                                                                                                                               |
| `concept-management`              | `agent-contract` | Keep               | Operating and authorization posture                                                                                                                                                                       |
| `concept-management`              | `teaching`       | Keep               | Cross-command management workflow                                                                                                                                                                         |
| `concept-management`              | `inventory`      | Cut: current owner | `agw resource kinds --output json` and `agw resource list --include-disabled --output json`; use the relevant `vm`, `workspace`, `agent`, `session`, or `console list --output json` for operational rows |
| `concept-management`              | `related`        | Keep               | Contextual navigation                                                                                                                                                                                     |
| `concept-manifesto`               | `overview`       | Keep               | Project values and rationale                                                                                                                                                                              |
| `concept-manifesto`               | `agent-contract` | Keep               | Application of those values                                                                                                                                                                               |
| `concept-manifesto`               | `teaching`       | Keep               | Design synthesis                                                                                                                                                                                          |
| `concept-manifesto`               | `related`        | Keep               | Contextual navigation                                                                                                                                                                                     |
| `concept-migration`               | `overview`       | Keep               | Migration concept and scope                                                                                                                                                                               |
| `concept-migration`               | `agent-contract` | Keep               | Migration authorization posture                                                                                                                                                                           |
| `concept-migration`               | `teaching`       | Keep               | Ordered migration and recovery workflow                                                                                                                                                                   |
| `concept-migration`               | `actions` (12)   | Keep               | Bounded operations with authorization, verification, and refusal paths                                                                                                                                    |
| `concept-migration`               | `related`        | Keep               | Contextual navigation                                                                                                                                                                                     |
| `concept-onboarding`              | `overview`       | Keep               | Repeatable adoption concept                                                                                                                                                                               |
| `concept-onboarding`              | `agent-contract` | Keep               | Startup and continuing-work posture                                                                                                                                                                       |
| `concept-onboarding`              | `teaching`       | Keep               | Setup sequence and current-adoption interpretation                                                                                                                                                        |
| `concept-onboarding`              | `inventory`      | Cut: current owner | `agw resource kinds --output json` and `agw resource list --include-disabled --output json`; the derived assessment remains                                                                               |
| `concept-onboarding`              | `related`        | Keep               | Contextual navigation                                                                                                                                                                                     |
| `concept-release-notes`           | `overview`       | Keep               | Current-versus-historical release concept                                                                                                                                                                 |
| `concept-release-notes`           | `agent-contract` | Keep               | Untrusted-evidence posture                                                                                                                                                                                |
| `concept-release-notes`           | `teaching`       | Keep               | Version and range interpretation                                                                                                                                                                          |
| `concept-release-notes`           | `release-notes`  | Keep               | Packaged evidence has no separate CLI owner                                                                                                                                                               |
| `concept-release-notes`           | `actions` (1)    | Keep               | Bounded fallback with authorization and refusal paths                                                                                                                                                     |
| `concept-release-notes`           | `related`        | Keep               | Contextual navigation                                                                                                                                                                                     |
| `concept-reporting-bugs`          | `overview`       | Keep               | External-reporting concept                                                                                                                                                                                |
| `concept-reporting-bugs`          | `agent-contract` | Keep               | Redaction and submission authorization posture                                                                                                                                                            |
| `concept-reporting-bugs`          | `teaching`       | Keep               | Reproduction and reporting workflow                                                                                                                                                                       |
| `concept-secrets`                 | `overview`       | Keep               | Secret declaration and source concept                                                                                                                                                                     |
| `concept-secrets`                 | `agent-contract` | Keep               | Secret-value boundary                                                                                                                                                                                     |
| `concept-secrets`                 | `teaching`       | Keep               | Cross-command secret workflow                                                                                                                                                                             |
| `concept-secrets`                 | `inventory`      | Cut: current owner | `agw resource list --include-disabled --output json` owns the capability-implementation rows this block renders                                                                                           |
| `concept-source-review`           | `overview`       | Keep               | Source-review purpose and scope                                                                                                                                                                           |
| `concept-source-review`           | `agent-contract` | Keep               | Candidate-source trust posture                                                                                                                                                                            |
| `concept-source-review`           | `teaching`       | Keep               | Focused, full, and declined review choices                                                                                                                                                                |
| `concept-source-review`           | `actions` (2)    | Keep               | Bounded review records with authorization and refusal paths                                                                                                                                               |
| `concept-source-review`           | `related`        | Keep               | Contextual navigation                                                                                                                                                                                     |
| `concept-troubleshooting`         | `overview`       | Keep               | Diagnostic concept                                                                                                                                                                                        |
| `concept-troubleshooting`         | `agent-contract` | Keep               | Diagnosis-versus-repair boundary                                                                                                                                                                          |
| `concept-troubleshooting`         | `teaching`       | Keep               | Cross-command troubleshooting workflow                                                                                                                                                                    |
| `plugin/apt/overview`             | `overview`       | Keep               | Optional catalog concept                                                                                                                                                                                  |
| `plugin/apt/overview`             | `agent-contract` | Keep               | Plugin enablement posture                                                                                                                                                                                 |
| `plugin/apt/overview`             | `teaching`       | Keep               | Dependency and selection synthesis                                                                                                                                                                        |
| `plugin/apt/overview`             | `actions` (2)    | Keep               | Bounded enable and verify records                                                                                                                                                                         |
| `plugin/install-command/overview` | `overview`       | Keep               | Optional catalog concept                                                                                                                                                                                  |
| `plugin/install-command/overview` | `agent-contract` | Keep               | Plugin enablement posture                                                                                                                                                                                 |
| `plugin/install-command/overview` | `teaching`       | Keep               | Selection and execution-model synthesis                                                                                                                                                                   |
| `plugin/install-command/overview` | `actions` (2)    | Keep               | Bounded enable and verify records                                                                                                                                                                         |

The 13 exact-version `concept-release-notes/vX-Y-Z` topics each contain one generated
`release-notes` block. Keep the pattern. It makes packaged historical evidence addressable and no
other command provides it.

The derived `concept-onboarding/derived-plan` block brings the non-schema fixed total to 62 and the
kept total to 59 after the three authored inventories are cut. Keep it: it turns resource, instance,
relationship, and verification facts into adoption findings and next actions, which no single
command does. Before `_dynamic_topic` or the generic `GuideView` leaves, a smaller onboarding
projector must reproduce this assessment's inputs and behavior.

## Schema-generated block inventory

Rows describe repeated generation patterns and name every member of each group below. Counts are
block instances, not topic counts.

| Generated topics           | Block           | Count | Decision           | Command replacement or dependency                                |
| -------------------------- | --------------- | ----: | ------------------ | ---------------------------------------------------------------- |
| Declarable kinds           | `overview`      |    14 | Cut: release-gated | `agw resource explain KIND`; CLI grammar child                   |
| Declarable kinds           | `inventory`     |    14 | Cut: current owner | `agw resource list --kind KIND --include-disabled --output json` |
| Declarable kinds           | `fields`        |    14 | Cut: release-gated | `agw resource explain KIND`; CLI grammar child                   |
| Declarable kinds           | `sample`        |    14 | Cut: current owner | `agw resource sample KIND`                                       |
| Capability kinds           | `overview`      |     4 | Cut: release-gated | `agw resource explain KIND`; CLI grammar child                   |
| Capability kinds           | `fields`        |     4 | Cut: release-gated | `agw resource explain KIND`; CLI grammar child                   |
| Capability kinds           | `inventory`     |     4 | Cut: current owner | `agw resource list --kind KIND --include-disabled --output json` |
| Capability implementations | `overview`      |    14 | Cut: release-gated | `agw resource explain KIND/NAME`; CLI grammar child              |
| Capability implementations | `state`         |    14 | Cut: current owner | `agw resource list --kind KIND --include-disabled --output json` |
| Capability implementations | `relationships` |    14 | Cut: release-gated | `agw graph show KIND/NAME`; CLI grammar child                    |
| Capability implementations | `instances`     |    14 | Cut: release-gated | `agw graph show KIND/NAME`; CLI grammar child                    |
| Capability implementations | `fields`        |    14 | Cut: release-gated | `agw resource explain KIND/NAME`; CLI grammar child              |

Declarable kinds: `admin-template`, `agent-template`, `apt-package`, `apt-source`, `git-credential`,
`named-console-template`, `secret`, `secret-source`, `session-template`, `system-install-command`,
`user-install-command`, `vm-site`, `vm-template`, and `workspace-template`.

Capability kinds: `git-credential-provider`, `harness-integration`, `secret-backend`, and
`vm-platform`.

Capability implementations: `git-credential-provider/azdo`, `git-credential-provider/github`,
`harness-integration/claude-code`, `harness-integration/codex`, `harness-integration/shell`,
`secret-backend/env-var`, `secret-backend/onepassword`, `secret-backend/prompt`,
`vm-platform/aws-ec2`, `vm-platform/azure-vm`, `vm-platform/gcp-gce`, `vm-platform/lima`,
`vm-platform/proxmox`, and `vm-platform/wsl2`.

The destination spellings above are intentional. `resource describe-kind` answers field-reference
questions today, but the saga has already assigned its replacement to `resource explain`.
`resource describe` also answers instance and inbound-relationship questions today, but it is
scheduled for removal in favor of `graph show`. The operator accepted deleting the guide views
before those settled commands land because the gap exists only on unreleased `main` and the CLI
grammar rewrite gates 0.14.0.

## Runtime-generated block inventory

Each configured resource not already represented by a schema target receives this four-block topic.
The population is unbounded and configuration-dependent.

| Block           | Decision           | Command replacement or dependency                                           |
| --------------- | ------------------ | --------------------------------------------------------------------------- |
| `state`         | Cut: current owner | `agw resource list --kind KIND --include-disabled --output json`            |
| `relationships` | Cut: release-gated | `agw graph show KIND/NAME`; CLI grammar child                               |
| `instances`     | Cut: release-gated | `agw graph show KIND/NAME`; CLI grammar child                               |
| `related`       | Cut: release-gated | `agw resource explain KIND`; remove this navigation with its resource topic |

`service._dynamic_topic` also has a kind-only fallback containing one `inventory` block. It has no
current instances because all 18 registered kinds are schema targets. Cut this dormant fallback in
the same effort; it is additional to the 141 fixed-block count, and
`agw resource list --kind KIND --include-disabled --output json` owns the answer.

## Machinery consequences

| Cut family                                         | Last guide consumers and likely deletion                                                                                                                                                                                                                                                                   | What remains                                                                                                                                                       |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Three authored inventories                         | `GuideRoot`, `_CONCEPT_RESOLVER_ROOTS`, `GuideView.inventory`, concept inventory projection, and `_live_instance_facts` in `guide/view.py`; their contribution flags and tests                                                                                                                             | The onboarding assessment still projects resources, instances, and relationships into findings                                                                     |
| `FieldReference`                                   | The block variant and parser branches in `guide/contract.py`; `_schema_value`, `_field_rows`, selector traversal, `_field_reference`, and schema dispatch in `guide/render.py`; schema-topic construction and catalog merging in `guide/service.py`; guide schema-adapter tests                            | The manifest reference tree and its `resource explain`, sample, and JSON Schema consumers                                                                          |
| `Sample`                                           | The block variant, `_fenced_yaml`, sample render dispatch, and schema-topic sample entries                                                                                                                                                                                                                 | `agentworks.manifests.samples` and `agw resource sample`                                                                                                           |
| Schema topics as a group                           | `_schema_topic`, `_build_schema_catalog`, schema-name merging in guide resolution and `--names-only`, and most of `tests/guide/test_schema_adapter.py`                                                                                                                                                     | Config-free schema authority and all non-guide presentation paths                                                                                                  |
| Runtime resource topics                            | `_dynamic_topic`, registry-resource guide-name expansion, dynamic topic lookup, and resource-topic rendering tests, but only after its onboarding-assessment use moves with parity to the smaller projector                                                                                                | Resource and graph commands remain the fact owners                                                                                                                 |
| `State`, `Relationships`, `InstanceList` rendering | Together with `FieldReference` and `Sample`, five nominal block variants become removable across contract parsing, exports, live-context classification, headings, and render dispatch. `GuideView` can be replaced by the smaller onboarding snapshot projector after the last rendered live block leaves | `GuideIdentity`, `GuideResourceFact`, `GuideRelationship`, and `GuideInstanceFact` remain useful assessment records unless the assessment itself is reshaped       |
| Fail-soft live context                             | The per-block view map and omitted-live-block classification shrink to the onboarding assessment path                                                                                                                                                                                                      | One aggregated warning and the onboarding unavailable placeholder still serve malformed or unavailable live context                                                |
| Release evidence                                   | Nothing is cut                                                                                                                                                                                                                                                                                             | `agentworks.release_notes`, the release renderer, and exact-version topic generation remain live                                                                   |
| Operational JSON                                   | No adapter loses a consumer                                                                                                                                                                                                                                                                                | Guide output is Markdown-only. `machine_output.py` and per-command JSON projectors are independent; guide actions continue to cite those commands for verification |

This is not a reason to refactor operational command facts or the shared `ResourceKind.instances`
hooks. Those have non-guide consumers. It is also not the simplification pass's G5 JSON/human
renderer problem: the survey removes a parallel guide presentation layer, while operational human
and JSON renderers remain a separate design question.

## Recommended execution boundary

1. After corrected PR #548 lands, the onboarding effort owns one deletion PR covering all 141 fixed
   command-duplicating blocks, the dormant fallback, every runtime resource topic, connective
   command signposts, and the contract, renderer, service, view, and test machinery orphaned by the
   removal. This one-time ownership exception avoids coordinating content and machinery waves.
2. Preserve the onboarding assessment. Before deleting `_dynamic_topic` or generic `GuideView`,
   replace their assessment use with the smallest projector that reproduces the retained assessment
   facts and behavior.
3. The CLI grammar child subsequently lands `resource explain` and `graph show` and owns updating
   command spellings. The temporary gap is accepted on unreleased `main`; the saga release gate
   forbids 0.14.0 until those settled destinations ship.
4. The simplification pass does not repeat guide machinery assigned to this one-wave deletion. Its
   remaining work and the CLI grammar rewrite stay independent.

The survey therefore recommends one removal wave: 141 fixed blocks, the dormant fallback, and all
runtime resource topics. The current-owner versus release-gated distinction remains useful evidence
about temporary command coverage, but it no longer sequences implementation.

## Evidence anchors

- Authored block assembly and topic registration:
  `cli/agentworks/guide/contributions.py:49-77,460-524`,
  `cli/agentworks/secrets/guide_contributions.py:24-39`, and the two
  `cli/agentworks/plugins/*/guide_contributions.py` adapters.
- Historical release-topic generation and schema/runtime topic construction:
  `cli/agentworks/guide/service.py:117-280`.
- Live-context selection and assessment projection: `cli/agentworks/guide/service.py:296-573` and
  `cli/agentworks/guide/view.py:28-288`.
- Block rendering: `cli/agentworks/guide/render.py:105-352,410-501`.
- Current replacement commands: `cli/agentworks/cli/commands/resource.py:51-307,387-426` and the
  operational list commands under `cli/agentworks/cli/commands/`.
