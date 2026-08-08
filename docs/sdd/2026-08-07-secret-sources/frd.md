# FRD: Secret Sources (Wave 3)

- Status: Seed, ready for an effort lead
- Date: 2026-08-07
- Seeded by: the roadmap lead. This is the roadmap's wave 3 child
  (`docs/sdd/2026-08-04-next-steps/`), unblocked by wave 2's landing (PR #414): the per-source
  config model wants the schema machinery, and the descriptor carries the interim exception this
  wave removes. The effort lead owns the HLA and plan; the roadmap lead reviews PRs. Per the sdd
  skill, this FRD becomes the effort lead's on merge of this seeding PR.

## Purpose

Secrets get the two-level model the roadmap settled: a `secret-backend` is the implementation kind
(the `vm-platform` analog), and a `secret-source` is a declarable configured instance of one (the
`vm-site` analog), exposing key-value secrets through that backend with per-source config. This ends
the constructed-singleton special case, makes multi-account and multi-vault setups declarable, and
evolves the resolution API from boolean-shaped answers to typed outcomes. The code already names
this direction as its own graduation signal (`secrets/backends.py`: "graduate the backend to a
declarable instance kind, the secret-backend analog of vm-site").

## Requirements

- R1. `secret-source` is a declarable resource kind: each source names its backend implementation,
  carries that backend's per-source config model (validated like all capability config, one blob at
  a time), and exposes KV secrets. A source maps to exactly one backend; per-source mapping to
  multiple backends is not required.
- R2. **The simple case keeps its current spelling** (settled reference shape, operator,
  2026-08-05): zero-config backends get synthesized sources under their current names (`env-var`,
  `prompt`), so existing per-secret references and `[secret_config].backends` chains keep working
  unchanged, and the model has exactly one concept (sources). Synthesized sources appear on
  discovery surfaces (describe, samples, guide) like declared ones. They are the canonical
  defaults-with-override case under the collision semantics settled with the installer-plugins child
  (`docs/sdd/2026-08-07-installer-plugins/frd.md`, C4): an operator declaring a source under a
  synthesized name replaces the synthesized row, with the substitution surfaced in provenance by
  `describe` and `doctor`, never silently.
- R3. Every per-secret reference names a source. Direct backend references become a deprecated
  compatibility path (warn in one release, reject in the next), never a permanent second branch. The
  runway has two carriers: the settings-side chain (`[secret_config].backends`) rides the kept
  config deprecation channel, while manifest-side per-secret references need their own carrier added
  into the shared deprecation surface (the per-source-carrier pattern recorded at the kept channel,
  reusing `--no-deprecations` and the suppression reporting), built as part of this wave. This is
  the "rebuild one" branch `target-state.md`'s manifest channel-gap item anticipated, and this wave
  closes that item.
- R4. Resolution API evolution: typed per-secret outcomes with explicit failure categories,
  policy-aware interaction requirements (the non-interactive discipline the verification surfaces
  established), timeouts and cleanup, and bounded-lifetime source clients. The simple case must not
  get more verbose.
- R5. The descriptor's constructed-singleton interim exception is removed: the `secret-backend`
  registry stores classes or factories like every other kind, with a chosen construction point and
  the resolve machinery updated. Lifecycle layering per `target-state.md`'s descriptor rulings:
  source resolution runs upstream of every other capability's runup.
- R6. **A source's config MUST NOT reference secrets** (v1), enforced structurally at registration
  conformance (no secret-reference-annotated fields in a source config model), keeping resolution
  single-stage and the chain a simple order. Interactive authentication remains source-client
  behavior and stays legal. Sources resolve in active-chain order (`[secret_config].backends`,
  already hard-error references after wave 2, becomes a chain of source names).
- R7. Relocation rides this wave per the descriptor contract: the secrets machinery moves into the
  `capabilities/` tree on the shared contract.
- R8. Map-keyed hosting lands in the descriptor (contract amendment, 2026-08-07, recorded in
  `capability-descriptor-contract.md`): the descriptor records where a map-keyed capability config
  is hosted, and schema emission consumes it so the `onepassword` mapping gets completions, key
  checking, and `op://` validation in editors. This closes the escalation the wave 2 lockfile
  recorded, whose trigger has fired.
- R9. Discovery and teaching ride the change: describe/schema/samples derive from the new kind's
  models by construction, guide topics contribute through the universal contract, sample-config and
  completions updated, and the deprecation warning's remediation names the rewrite.

## Settled constraints (inherited; do not reopen)

- C1. Secret backends are ordinary capabilities, full stop (operator mandate, 2026-08-05); the
  backend/source split is the mechanism for the mandate, not an exemption from it.
- C2. The synthesized-source reference model is settled (operator, 2026-08-05); do not reopen the
  direct-reference-forever alternative.
- C3. The anchored-projections review question applies to the resolution surfaces: what does a
  source client see, where is that view enforced, and secrets never enter persisted state or
  resolved configuration.
- C4. Compatibility posture: the deprecation runway rides the kept config channel; remediation is
  precise errors plus guide content, no migration tooling.
- C5. The `development-principles` bad-complexity test applies; in particular, the simple case
  (env-var and prompt, no declared sources) must remain invisible machinery to the operator.

## Acceptance

- AC1. A 0.13-shaped config with no declared sources behaves identically: env-var and prompt resolve
  through synthesized sources with unchanged spellings, and `agw doctor` and `agw secret verify`
  report through them.
- AC2. A declared source (the `onepassword` account case) is a manifest resource with validated
  config, readiness, and describe/schema/sample surfaces derived from its models.
- AC3. A direct backend reference warns through its carrier (config channel for the settings chain,
  the new manifest carrier for per-secret references) with the exact rewrite named; the same
  reference under the next release's posture hard-errors (mechanism proven in tests; the release
  flip itself is scheduled work).
- AC4. Editing an `onepassword` `backend_mappings` table in a schema-associated editor offers
  completions and key checking (R8 landed end to end).
- AC5. Resolution outcomes are typed: at minimum unavailable, refused-interaction, timeout, and
  resolution-failure are distinguishable by category in `agw secret verify` output and in the
  operation-boundary errors.

## Named future consumer: token minting (decide the fork in the HLA)

Credential minting (creating a token on demand via API, e.g. a GitHub App installation token) is
planned near-term (operator, 2026-08-08) and this wave's HLA decides where it models:

- **As a secret source** (the roadmap lead's lean): a `github-app`-style source mints on resolve;
  `git-credential` config is unchanged forever (its `token` field keeps naming a secret), expiry and
  re-mint ride the bounded-lifetime client and typed-outcome machinery this wave builds, and
  per-token scoping rides the per-secret mapping. No breaking change at any point.
- **As a git-credential variant**: a discriminated union on the credential config per the
  variant-modeling contract (the required field sets differ, so the shape test passes). If this
  model wins, the one-arm restructure of `git-credential` MUST land before the 0.14.0 cut (riding
  the cushioned release, defaulting to the stored arm per the omission-history rule) so that minting
  later arrives as a purely additive arm rather than a post-release break.

The HLA records the choice and its rationale; if the variant model is chosen, the restructure is
raised to the roadmap lead immediately so the 0.14 window is not lost.

## Open questions for the effort lead

- The backend construction point (registration-time factory versus first-use) and the source-client
  lifecycle shape (R4's bounded lifetimes).
- The readiness shape the `secret-source` kind declares (capability classmethod over config versus
  consuming-resource hook); the descriptor records the choice made (descriptor contract, open
  question carried from wave 2).
- Whether the backend's per-secret `mapping_model` re-homes onto the source (the descriptor contract
  and the wave 2 plan record this as wave 3's call).
- How synthesized sources are represented internally (true registry rows versus a projection) so
  long as R2's surface behavior holds.
- The deprecation window release mapping (which release warns, which rejects), proposed to the
  operator with the plan.
