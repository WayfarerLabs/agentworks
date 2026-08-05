# Harness integration rename: locked

**Locked:** 2026-08-05

This effort is complete. Agentworks uses `harness-integration` for the capability kind,
`harness_integration` for the session-template selector, and `harness_integration_state` for the
persisted session-state column. Version 0.13.0 performed the rename and migrated persisted state.
The 0.14.0 removal in commit `6d44a12c` deleted acceptance, warning aggregation, and migration
rewrites for the old `harness` and `harness_config` session-template inputs. Those keys now fail
ordinary unknown-key validation.

The removal retained the generic manifest warning boundary, pure registry construction, canonical
TOML-to-YAML migration, tagged `harness_integration` decoding and inheritance, and database
migration v31. Focused rejection and canonical-path tests, together with the deprecation-removal
residual sweep, provide the closeout evidence. The prior selector-only YAML rewrite dependency was
removed when its last consumer disappeared.

The permanent naming, capability contract, configuration rules, and plugin-author guidance live in
`cli/agentworks/capabilities/harness_integration/README.md`, with operator examples in
`docs/guides/resources.md` and the architectural decision in
`docs/adrs/0020-harness-integration.md`. The 0.14.0 generated release record is owned by the
deprecation-removal effort's final release phase. Nothing in this directory is required to
understand the current system.
