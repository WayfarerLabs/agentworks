# Mise integration: locked

**Locked:** 2026-08-05

This effort is complete as reconciled against current code, automated tests, samples, and permanent
documentation. Mise is always-installed VM infrastructure. Per-user YAML admin-template and
agent-template resources provide package declarations, optional source-referenced lockfiles,
unlocked fallback policy, release-age filtering, and activation settings. The original catalog
abstractions are gone.

Later implementation deliberately differs from the early design in four ways: YAML resources
superseded classic TOML runtime declarations; no `install_mise` toggle exists; shell activation uses
the managed per-user shell fragment rather than a system-wide profile fragment; and focused
initializer modules replaced the originally proposed file layout. Closeout did not introduce
compatibility or new feature work to imitate the obsolete shapes.

Automated coverage verifies source-reference parsing and local/git fetches, mise configuration
rendering, locked and unlocked installation branches, agent-specific setup, and early manifest and
migration validation. No live VM reinit matrix was performed during reconciliation. These five
manual checks remain explicitly unverified: unlocked install without a lockfile; locked install with
a lockfile; a missing package with `mise_allow_unlocked = false`; a missing package with
`mise_allow_unlocked = true`; and dotfiles-only mise configuration.

The stable operator contract lives in `docs/guides/mise.md`, and reusable source-reference syntax
lives in `docs/guides/source-refs.md`. Nothing in this directory is required to configure or
maintain current mise behavior.
