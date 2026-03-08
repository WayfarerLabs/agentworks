# Contributing

This is designed to be an opinionated project with high standards for code
quality, documentation, and design. Contributions must meet these standards and
follow the guidelines outlined in this document. I highly recommend creating
discussions or issues to propose and discuss changes before putting in the work
to implement them, especially for larger changes.

All that said, I'd love to see high-quality contributions of all sizes, from
fixing typos to adding major features.

## AI Coding Assistants

This project is designed to be developed with AI coding assistants. We use
[Rulesync](https://rulesync.dyoshikawa.com/) to manage shared AI configuration
(rules, skills, etc.) across tools. If you are contributing with an AI
assistant, you should too.

Source files live in `.rulesync/`. Generated output (`.claude/`, `.cursor/`,
`CLAUDE.md`) should never be edited directly. To regenerate after changes:

```bash
./ops/scripts/rulesync-upgen.bash
```

Personal tool targets go in `rulesync.local.jsonc` (gitignored). Copy the
example to get started:

```bash
cp rulesync.local.jsonc.example rulesync.local.jsonc
```

## Spec-Driven Development

Significant development efforts follow the SDD workflow. See
[docs/sdd/](docs/sdd/) for existing specs and the `sdd` rule in `.rulesync/` for
the full workflow description.

## Conventional Commits

All commit messages follow the
[Conventional Commits](https://www.conventionalcommits.org/) specification.

## Code Quality

- **Python**: ruff (linting + formatting), mypy (type checking), pytest
- **Markdown**: markdownlint, prettier, cspell
- Custom dictionaries are maintained in `.cspell.json`
