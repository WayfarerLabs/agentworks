# Session resume rename: locked

**Locked:** 2026-08-05

This effort is complete. Version 0.13.0 introduced `agw session resume`, `resume_command`, and the
`HarnessIntegration.resume` vocabulary. The 0.14.0 removal deleted the temporary `session restart`
command, its completion and warning paths, and all `restart_command` normalization and migration
support. Old spellings now fail through ordinary command or configuration validation; no bespoke
retired-name hints remain.

The removal is implemented by commit `6d44a12c`. Its focused tests prove that the old command is
unknown, the old shell field is rejected, and canonical resume behavior remains. The release-facing
0.13.0 transition record remains in `cli/CHANGELOG.md`; the coherent 0.14.0 breaking release record
is owned by the deprecation-removal effort's final release phase.

Current operator behavior lives in `cli/README.md` and `docs/guides/resources.md`. The permanent
harness-integration contract lives in `cli/agentworks/capabilities/harness_integration/README.md`.
The classified 0.14.0 residual sweep found old tokens only in rejection tests, historical changelog
and SDD records, or unrelated mechanical restart terminology. Nothing in this directory is required
to understand the current system.
