# Nerf Tools -- Tombstone

## 2026-03-25 -- Locked

All plan items complete. Specs accurate as of this date.

## 2026-06-16 -- Artifacts removed (tombstone)

The original `frd.md`, `hla.md`, and `plan.md` for this SDD have been deleted. They described an
in-tree build of the nerf-tools Claude Code plugin (agentworks owning the build, bundling its own
`nerf-config.yaml`, depending on the `nerftools` Python package, and planting the plugin onto every
VM at init). That whole approach has been replaced: nerftools now ships as a standalone Claude Code
marketplace/plugin and agentworks just registers it through the same `claude_marketplaces` /
`claude_plugins` mechanism that handles any other plugin.

Reading the original SDD today would describe a counterfactual -- the dependency, the
`nerf-config.yaml`, and the plugin-build code paths no longer exist in the codebase. That meets the
"work has been substantively replaced" deletion trigger in `.claude/skills/sdd/SKILL.md`.

The original artifacts are preserved in git history. See commit `ceb9e05` (the tip of `main` at the
time of the removal) and the rip-out PR for issue #119 for the deletion itself.
