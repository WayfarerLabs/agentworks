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

First thing after cloning, copy the example local config and edit it for whatever assistant(s) you
use, then regenerate the outputs:

```bash
cp rulesync.local.jsonc.example rulesync.local.jsonc
# edit rulesync.local.jsonc, setting "targets" to your tool(s)
./scripts/rulesync-upgen.sh
```

`rulesync.local.jsonc` is gitignored; only your local assistant's generated files (`.claude/`,
`.cursor/`, etc.) get produced for you and stay out of the repo.

### What gets committed

GitHub Copilot is the one shared target (declared in `rulesync.jsonc`). Its generated output lives
at `.github/copilot-instructions.md`, `.github/instructions/`, `.github/agents/`, and
`.github/skills/` and **is** checked in so Copilot Code Review can see the project's rules and
subagents on every PR. CI verifies this output stays in sync with `.rulesync/` sources via
`rulesync generate --check` — if you edit a source file, regenerate via the script above and commit
the result.

Source files in `.rulesync/` are the canonical input; never edit generated output directly.

## Spec-Driven Development

Significant development efforts follow the SDD workflow. See [docs/sdd/](docs/sdd/) for existing
specs and the `sdd` rule in `.rulesync/` for the full workflow description.

## Conventional Commits

All commit messages follow the [Conventional Commits](https://www.conventionalcommits.org/)
specification.

## Code Quality

- **Python**: ruff (linting + formatting), mypy (type checking), pytest
- **Markdown**: markdownlint-cli2, prettier, cspell
- Custom cspell dictionary is maintained in `.cspell.json`

### Running the markdown linters

The npm-based linters (cspell, markdownlint-cli2, prettier) and the rulesync drift check are pinned
via per-tool `.<tool>-version` files. The same versions run in CI. To run them locally:

```bash
./scripts/lint.sh        # check only -- exactly what CI runs
./scripts/lint.sh --fix  # auto-fix where each tool can; re-check; report what remains
```

`--fix` covers prettier formatting, markdownlint auto-fixable rules, and rulesync regeneration.
cspell cannot auto-fix unknown words; the script flags them and points you at `.cspell.json` to
either correct the spelling or add a word.
