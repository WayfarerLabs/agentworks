# Deprecation removal: locked

**Locked:** 2026-08-05

This effort is complete. The 0.14.0 branch rejects the retired session command, session-template
fields and selectors, older configuration aliases, VM shell option, and legacy VM console through
ordinary CLI or schema validation. Dead Python compatibility surfaces and rename-specific
deprecation bookkeeping are gone. No retired-name hint table or replacement compatibility layer was
introduced.

The canonical surfaces are `agw session resume`, `resume_command`, the tagged `harness_integration`
shape, `[defaults].site`, `[operator]`, `[paths].vscode_workspaces`, `agw vm shell --platform`, and
the top-level `agw console` family. Current operator guidance lives in `cli/README.md`,
`docs/guides/resources.md`, `docs/guides/mise.md`, and the relevant capability READMEs. The retained
generic deprecation channel remains live for no-op secret-backend sections and generic capability
sibling shapes.

The final classified sweep and isolated built-wheel evidence live in `residual-inventory.md`. All
automated repository gates passed, and both final complete-diff reviewers approved the only review
fix. The final breaking conventional commit supplies Release Please with the coherent 0.14.0 upgrade
record, including the phase-1 TOML sunset and aliases that never warned. Nothing in this directory
is required to operate or maintain the current system.
