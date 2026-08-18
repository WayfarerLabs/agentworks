# Contributing

This is designed to be an opinionated project with high standards for code quality, documentation,
and design. Contributions must meet these standards and follow the guidelines outlined in this
document. I highly recommend creating discussions or issues to propose and discuss changes before
putting in the work to implement them, especially for larger changes.

All that said, I'd love to see high-quality contributions of all sizes, from fixing typos to adding
major features.

## AI Coding Assistants

This project is designed to be developed with AI coding assistants. We use
[Rulesync](https://rulesync.dyoshikawa.com/) to manage shared AI configuration (rules, skills,
subagents) across tools. If you are contributing with an AI assistant, you should too.

### Initialize your workspace

Claude Code, Codex CLI, and GitHub Copilot work out of the box: their generated context is committed
to the repo, so a fresh clone already has it. No setup step needed for those tools.

If you use a different assistant (Cursor, Gemini, etc.), copy the example local config, add your
tool(s), and regenerate the outputs:

```bash
cp rulesync.local.jsonc.example rulesync.local.jsonc
# edit rulesync.local.jsonc, setting "targets" to your tool(s)
./scripts/rulesync-upgen.sh
```

`rulesync.local.jsonc` is gitignored, and the extra tools' generated files (`.cursor/`, `.gemini/`,
etc.) stay out of the repo.

### What gets committed

Three targets are shared, declared in `rulesync.jsonc`, and their generated output **is** checked
in:

- **GitHub Copilot** (`.github/copilot-instructions.md`, `.github/instructions/`, `.github/agents/`,
  `.github/skills/`) so Copilot Code Review sees the project's rules and subagents on every PR.
- **Claude Code** (`CLAUDE.md`, `.claude/agents/`, `.claude/rules/`, `.claude/skills/`) and **Codex
  CLI** (`AGENTS.md`, `.codex/`) so those agents have full context on a fresh clone with no setup.
  Only the rulesync-generated directories are committed; personal files such as
  `.claude/settings.local.json` stay gitignored.

CI verifies all three stay in sync with `.rulesync/` sources via
`./scripts/rulesync-upgen.sh --check`. If you edit a source file, regenerate via the script above
and commit the result.

Source files in `.rulesync/` are the canonical input; never edit generated output directly.

## Spec-Driven Development

Significant development efforts follow the SDD workflow. See [docs/sdd/](docs/sdd/) for existing
specs and the `sdd` skill (`.rulesync/skills/sdd/SKILL.md`) for the full workflow description.

Once an SDD's `locked.md` lands on `main`, its feature directory is locked. CI enforces this via
`./scripts/check-locked-sdds.sh` (run on every PR and push to `main`): the only changes it permits
under a locked directory are updating `locked.md` itself or deleting the directory in full down to
the `locked.md` tombstone. Introducing `locked.md` in the same PR as the final SDD edits is allowed;
the check compares against the merge-base with `main`, so only a pre-existing lockfile freezes the
directory.

## Conventional Commits

All commit messages follow the [Conventional Commits](https://www.conventionalcommits.org/)
specification.

Release Please parses `BREAKING CHANGE:` footers as trailers. Keep each footer to one paragraph with
no blank lines or indented code blocks. No continuation line may begin with a trailer-like `token:`;
keep before/after examples inline instead.

## Code Quality

- **Python**: ruff (linting + formatting), mypy (type checking), pytest
- **Markdown structure**: markdownlint-cli2
- **Markdown / JSON / YAML formatting**: prettier
- **Spelling across markdown, Python, YAML, and TOML**: cspell (custom dictionary in
  `.cspell.json`). JSON/JSONC are intentionally excluded; they're identifiers and config, not prose.

### Running the Python tests

From `cli/`, the default pytest configuration uses all available workers:

```bash
uv run pytest tests/ -m 'not integration'
```

Pass `-n 0` for a deliberately single-process debugging run. Tests that genuinely share an external
resource must use a named `xdist_group` with a one-line comment explaining the constraint; do not
serialize tests preemptively.

### Running the file-quality linters

The npm-based linters (cspell, markdownlint-cli2, prettier) are pinned via per-tool
`.<tool>-version` files. Node itself is pinned in `.node-version`. CI invokes the same script
described below, so what runs locally is exactly what runs in CI.

```bash
./scripts/lint-files.sh        # check only (exactly what CI runs)
./scripts/lint-files.sh --fix  # auto-fix where each tool can, re-check, report what remains
```

`--fix` covers prettier formatting and markdownlint-cli2 auto-fixable rules. cspell cannot auto-fix
unknown words; the script flags them and points you at `.cspell.json` to either correct the spelling
or add a word.

### Editing rulesync sources

Files under `.rulesync/` are markdown; they get linted by markdownlint-cli2 and prettier just like
the rest of the repo. Rulesync's _generated_ output (the committed shared-target output under
`.github/`, `.claude/`, `.codex/`, and the root `CLAUDE.md` / `AGENTS.md`) is deliberately excluded
from the linters via each tool's config; otherwise the linters and rulesync would fight (prettier
reformats a file, next `rulesync generate` overwrites it, repeat).

**Lint before you regenerate.** Prettier may reformat the source, and running it after regeneration
leaves the generated output out of sync with the prettified source, so CI's drift check will fail.
The right order is:

1. Edit the `.rulesync/` source.
2. `./scripts/lint-files.sh --fix` prettifies the source (and the rest of the repo).
3. `./scripts/rulesync-upgen.sh` regenerates the committed output for all shared targets. Your
   `rulesync.local.jsonc` targets can be anything; upgen always refreshes the shared output
   regardless.
4. Commit both the source and the generated files.

To verify the committed shared-target output is up to date without regenerating, use
`./scripts/rulesync-upgen.sh --check`. CI invokes the same script.
