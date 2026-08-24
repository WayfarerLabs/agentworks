---
name: rulesync-for-ai-codev
description: 'Managing AI coding assistant rules, skills, and subagents with Rulesync'
---
# Rulesync

This project uses [Rulesync](https://rulesync.dyoshikawa.com/) to maintain a single source of truth
for AI coding assistant configuration across tools (Claude Code, Copilot, Codex CLI, Cursor, and so
on).

## Structure

- `.rulesync/rules/*.md`: always-on context (loaded every session). The one deliberate exception is
  `cli-conventions.md`, which carries frontmatter `globs` so it is delivered path-scoped to the CLI
  sources it governs; every other rule is delivered unconditionally.
- `.rulesync/skills/*/SKILL.md`: on-demand context (invoked when needed)
- `.rulesync/subagents/*.md`: specialized assistant personas
- `rulesync.jsonc`: shared config; declares `targets: ["copilot", "claudecode", "codexcli"]` and the
  enabled features
- `rulesync.local.jsonc`: personal tool targets (gitignored); create it from
  `rulesync.local.jsonc.example`, then edit it for your tool setup and preferences
- `.rulesync-version`: pinned rulesync version

## What gets committed

Three targets are shared, and their generated output **is** checked in:

- **Copilot**, under `.github/` (`copilot-instructions.md`, `instructions/`, `agents/`, `skills/`),
  so Copilot Code Review has the project's rules and subagents whenever it runs.
- **Claude Code** (`CLAUDE.md`, `.claude/{agents,rules,skills}/`) and **Codex CLI** (`AGENTS.md`,
  `.codex/`), so those agents have full context on a fresh clone without any setup step. Only the
  rulesync-generated directories are committed; personal files (e.g. `.claude/settings.local.json`)
  stay gitignored via a catch-all-plus-re-include in `.gitignore`.

CI runs `./scripts/rulesync-upgen.sh --check` (which calls `rulesync generate` for all three shared
targets) to catch drift. Generated output for any _other_ target (`.cursor/`, `.gemini/`, etc.) is
gitignored. Never edit any generated output directly; rerun `./scripts/rulesync-upgen.sh` instead.

The markdown linters (cspell, markdownlint-cli2, prettier) scan the whole repo, but each is
configured to **skip rulesync's committed outputs** (the `.github/` copilot trees plus `CLAUDE.md`,
`.claude/`, `AGENTS.md`, and `.codex/`). The sources under `.rulesync/` are still linted along with
everything else; only the generated copies are excluded. Without that exclusion the linters and
rulesync would fight: prettier would reformat a generated file and the next `rulesync generate`
would overwrite it, producing perpetual drift.

## Making changes

When you edit anything under `.rulesync/`:

1. **Lint first.** `.rulesync/**/*.md` files go through markdownlint-cli2 and prettier like any
   other markdown. Run `./scripts/lint-files.sh --fix`. Prettier may reformat them.
2. **Then regenerate.** `./scripts/rulesync-upgen.sh` always refreshes the committed shared-target
   output regardless of your personal `rulesync.local.jsonc` targets.
3. **Commit both source and generated files.**

Doing step 2 before step 1 produces drift between the source (now reformatted by prettier) and the
generated output (still matches the pre-reformat source). CI will fail in that case.

To verify the committed shared-target output is up to date without regenerating, run
`./scripts/rulesync-upgen.sh --check`.

## Reference

See [CONTRIBUTING.md](../../../CONTRIBUTING.md) for setup instructions and the
[Rulesync documentation](https://rulesync.dyoshikawa.com/) for full details on rules, skills,
subagents, targets, sources, and other features.
