# HLA: Agentworks Assistance, Discovery, and Management

- Status: Active, trail-sign revision
- FRD: `docs/sdd/2026-08-05-onboarding-and-discovery/frd.md`
- Existing component records: the LLDs in this feature directory. This HLA supersedes their no-topic
  presentation and guide-failure claims; landed component contracts remain historical implementation
  records.

> **Phase 4 supersession (PR #556):** Selected guide topics no longer contain schema or runtime
> resource blocks, and names-only discovery no longer adds schema or live resource topics. Authored
> concept, plugin, and release topics remain; only `concept-onboarding` loads live context, through
> a direct bounded assessment projector rather than `GuideView`. The live-block and schema-catalog
> sections below remain as the design record this deletion superseded.

## Destination

The shipped system has four layers:

1. One canonical assistance prompt projected into the README, website, Claude Code package, and
   Codex package.
2. A short no-topic `agw guide` trail sign.
3. Selected guide topics containing static teaching, optional live facts, and inert action records.
4. Versioned JSON facts from operational list, describe, and doctor commands.

This round changes layers 2 and 3. The no-topic response becomes a cheap signpost. The complete
startup body moves to onboarding. Selected topics load live context only when one of their existing
blocks needs it, and live failures degrade the page instead of failing a valid guide request.

```text
canonical prompt
      |
      v
agw guide --agent                 fixed trail sign, no catalogs or live state
      |
      +--> concept-onboarding     startup, adoption, first VM/session
      +--> concept-management     existing-system changes and operation
      +--> other selected topic   static teaching, optional live blocks
                                           |
                                           v
                                  one warning + short placeholders
                                  when live context is unavailable
```

## Trail-sign rendering

After normal argument and evidence validation, the ordinary no-topic path renders one fixed local
destination tuple. It bypasses both `build_authored_catalog()` and `_build_schema_catalog()` and
returns before configuration, registry, database, `GuideView`, network, or managed-resource work. It
therefore behaves identically for absent, valid, and malformed configuration.

The tuple is the only no-topic destination source. Agent mode renders all seven entries:

- `concept-onboarding`;
- `concept-management`;
- `concept-troubleshooting`;
- `concept-release-notes`;
- `concept-migration`;
- `concept-secrets`; and
- `concept-reporting-bugs`.

Human mode consumes the same tuple but renders only two choices: onboarding for a new installation,
and management plus exhaustive discovery for an existing installation. Both modes mention shell
completion and `agw guide --names-only`. Neither contains action records, live facts, or the startup
posture.

The fixed slugs necessarily duplicate identities owned by selected topic contributions because the
short path cannot build their catalog. One structural test resolves every slug through the normal
selected-topic path. Wording remains review-owned and unpinned.

No-topic also does not report authored or schema contribution issues. That is deliberate. A rejected
selected topic remains the scoped failure surface; unrelated invalid contributions do not turn the
trail sign into a diagnostic report.

## Selected-topic dependency rule

The existing closed block vocabulary supplies the dependency signal. No new contribution field or
topic allowlist is introduced.

Configuration-independent blocks are:

- `Overview`, `Teaching`, and `AgentContract`;
- `ActionList` and `TopicLinks`;
- `ReleaseNotes`; and
- `FieldReference` and `Sample`, whose installed schema services are configuration-free.

Live blocks are `InstanceList`, `State`, and `Relationships`. The derived onboarding assessment is
also live. After catalog resolution, the service loads configuration, registry, and database once
only when at least one selected topic contains a live block or requests the onboarding assessment.
Multi-topic output shares that one attempt.

Onboarding evidence is parsed before projection but applied only when the derived assessment needed
to verify it exists. If that assessment is unavailable, well-formed evidence remains unapplied, the
derived-assessment block becomes unavailable, and the shared warning records the omission. Malformed
evidence and evidence that available facts prove invalid retain their existing nonzero failures.

If live context succeeds, rendering follows the existing path. If it fails because configuration is
absent or malformed, registry construction fails, the database is unavailable, or a topic view
cannot be built:

1. every static block still renders;
2. each affected live block renders one short unavailable placeholder;
3. the response prepends one `Guide context is incomplete` section;
4. that section frames each distinct sanitized root problem once and lists its omitted topic/block
   identities; and
5. the valid request exits 0.

The implementation reuses the existing system error, runtime issue collection, terminal sanitizer,
and unavailable rendering path. A small response-local aggregation step replaces repeated error
sentences. It adds no public diagnostic record, identifier vocabulary, persistence, or API.

A syntactically valid `kind/name` request for a known kind is not declared unknown merely because
configuration prevented registry lookup. It renders a generic degraded requested-topic document and
the shared warning. Malformed slugs, unknown kinds, unknown static topics when catalogs are
available, malformed verification evidence, verification evidence that available facts prove
invalid, conflicting options, rejected requested contributions, and internal guide rendering defects
remain errors and nonzero.

This establishes the exit-code boundary: 0 means the valid guide request rendered, possibly with
clearly incomplete live context. It does not mean configuration or resources are healthy. Doctor
owns that determination.

## Names-only and completion

`--names-only` remains the shell-completion source and therefore emits names only. It builds the
authored and schema catalogs, returns their valid names, and augments them with live resource names
when configuration and registry construction succeed. If live context fails, it returns the static
names with exit 0 and omits only unestablished live names. It emits no diagnostic prose because that
would corrupt completion input.

This is a best-effort discovery contract, not a health check and not a promise that live names are
available under broken configuration. Optimizing its existing registry cost belongs to the
simplification pass's guide-machinery work, not this content round.

## Onboarding organization

`concept-onboarding` is the sole home of the complete startup assistance posture. Its existing
blocks are reorganized, not replaced by a new topic or state machine:

1. `Overview` identifies first setup and current-adoption assessment.
2. `AgentContract` carries the complete startup posture removed from no-topic.
3. `Teaching` presents discovery, configuration initialization, explicit provider choices, readiness
   verification, and the first VM/session sequence.
4. Existing live inventory and assessment blocks report current facts or the standard degraded
   placeholders and response warning.
5. Existing action records remain the only executable suggestions.
6. Related topics include `concept-source-review`; its focused and full actions stay there.

Other selected topics stand alone under the current operator instruction. They do not depend on an
earlier startup exchange and do not duplicate the onboarding contract.

## Existing architecture stays authoritative

The existing contribution, projection, action, release-note, schema, and JSON contracts remain
authoritative. This revision introduces no new block, action, consent, evidence, catalog, template,
state, machine-output, or authorization type. The simplification pass may remove internal machinery
under these behaviors; this effort does not preserve or replace it.

The old registry-inventory phase no longer earns separate work. Earlier code already supplies
dynamic kind and implementation inventory, specific-resource topics, and fixture-driven catalog
updates. The trail sign stops presenting those exhaustive facts at top level.

## Acceptance and release relationship

Closeout uses one provider-backed path from an exact reviewed candidate wheel and labels that
pre-publication substitution. After publication, a bounded canonical-prompt smoke installs the exact
stable release and reaches the trail sign. Generated package parity and focused installation tests
cover native wrappers.

These are child-effort acceptance checks. They do not supersede the saga's release-PR candidate,
publication, or final custom-domain gates.

## Test and documentation posture

Tests protect behavior and structure, not authored wording. Required new coverage is limited to:

- no-topic returns before either catalog or any live dependency for absent, valid, and malformed
  configuration;
- human and agent trail signs have their intended structural destination sets;
- every fixed destination resolves through the selected-topic path;
- a static-only topic never loads configuration;
- malformed configuration on live topics yields exit 0, all static blocks, one diagnostics section,
  and a placeholder for every omitted live block;
- multi-topic output deduplicates one shared root failure;
- a valid exact resource request degrades rather than becoming falsely unknown;
- well-formed onboarding evidence remains unapplied and degrades when its live assessment is
  unavailable;
- invalid requests and requested contribution defects remain nonzero; and
- names-only preserves static names when live names cannot be established.

Permanent CLI documentation explains the trail sign, the selected-topic degradation model, and
best-effort topic discovery. No completion code or sample configuration change is expected.

## Risks

- **A successful guide response is mistaken for healthy state.** The one prominent warning and
  permanent documentation state that doctor owns health.
- **Partial pages hide omissions.** Every omitted block remains visible as a short placeholder, and
  the response-level warning names affected topic/block identities.
- **Diagnostics become repetitive.** Root problems are deduplicated once per response; placeholders
  never repeat the detailed message.
- **Static and live dependencies drift.** The closed block vocabulary drives the decision, and a new
  block must be classified in the same exhaustive predicate.
- **The fixed destination tuple drifts from topic resolution.** One structural test resolves every
  slug without pinning prose.
- **The parallel cleanup changes shared files.** Coordinate with the simplification pass's unchecked
  Wave 1 guide-machinery item and preserve its file ownership before implementation.
