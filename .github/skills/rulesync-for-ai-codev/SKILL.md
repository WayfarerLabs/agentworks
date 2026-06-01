---
name: rulesync-for-ai-codev
description: 'Managing AI coding assistant rules, skills, and subagents with Rulesync'
---
# Rulesync

This project uses [Rulesync](https://rulesync.dyoshikawa.com/) to maintain a single source of truth
for AI coding assistant configuration across tools (Claude Code, Copilot, Codex CLI, Cursor, and so
on).

## Structure

- `.rulesync/rules/*.md`: always-on context (loaded every session)
- `.rulesync/skills/*/SKILL.md`: on-demand context (invoked when needed)
- `.rulesync/subagents/*.md`: specialized assistant personas
- `rulesync.jsonc`: shared config; declares `targets: ["copilot"]` and the enabled features
- `rulesync.local.jsonc`: personal tool targets (gitignored); pick whatever you use locally
- `.rulesync-version`: pinned rulesync version

## What gets committed

Copilot is the project's one shared target. Its generated output lives under `.github/`
(`copilot-instructions.md`, `instructions/`, `agents/`, `skills/`) and **is** checked in so Copilot
Code Review has access to the project's rules and subagents on every PR. CI runs
`rulesync generate --check` against this output to catch drift.

Generated output for any other target (`.claude/`, `.cursor/`, `CLAUDE.md`, `.codex/`, etc.) is
gitignored. Never edit any generated output directly; rerun the generator instead.

The markdown linters (cspell, markdownlint-cli2, prettier) scan the whole repo, but each is
configured to **skip rulesync's generated outputs** (`.github/copilot-instructions.md` and the
`.github/{instructions,agents,skills}/` trees). The sources under `.rulesync/` are still linted
along with everything else; only the generated copies are excluded. Without that exclusion the
linters and rulesync would fight: prettier would reformat a generated file and the next
`rulesync generate` would overwrite it, producing perpetual drift.

## Making changes

When you edit anything under `.rulesync/`:

1. **Lint first.** `.rulesync/**/*.md` files go through markdownlint-cli2 and prettier like any
   other markdown. Run `./scripts/lint.sh --fix`. Prettier may reformat them.
2. **Then regenerate.** `./scripts/rulesync-upgen.sh` always refreshes the committed copilot output
   regardless of your personal `rulesync.local.jsonc` targets.
3. **Commit both source and generated files.**

Doing step 2 before step 1 produces drift between the source (now reformatted by prettier) and the
generated output (still matches the pre-reformat source). CI will fail in that case.

To verify the committed copilot output is up to date without regenerating, run
`./scripts/rulesync-upgen.sh --check`.

## Reference

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup instructions and the
[Rulesync documentation](https://rulesync.dyoshikawa.com/) for full details on rules, skills,
subagents, targets, sources, and other features.
