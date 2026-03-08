---
name: rulesync-for-ai-codev
description: "Managing AI coding assistant rules and skills with Rulesync"
targets: ["*"]
---

# Rulesync

This project uses [Rulesync](https://rulesync.dyoshikawa.com/) to maintain a
single source of truth for AI coding assistant configuration.

## Structure

- `.rulesync/rules/*.md` -- always-on context (loaded every session)
- `.rulesync/skills/*/SKILL.md` -- on-demand context (invoked when needed)
- `rulesync.jsonc` -- shared config (features, baseDirs)
- `rulesync.local.jsonc` -- personal tool targets (gitignored)
- `.rulesync-version` -- pinned rulesync version

## Generated Output

Tool-specific files (`.claude/`, `.cursor/`, `CLAUDE.md`) are generated output.
Never edit them directly. They are overwritten on regenerate.

## Making Changes

1. Edit source files under `.rulesync/`
2. Run `./ops/scripts/rulesync-upgen.bash` to install, update, and regenerate
3. Commit both source and generated files

## Reference

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup instructions and the
[Rulesync documentation](https://rulesync.dyoshikawa.com/) for full details on
rules, skills, targets, sources, and other features.
