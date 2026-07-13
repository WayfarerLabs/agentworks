---
description: "General style and formatting guidelines"
globs: ["**/*"]
---

# Code Style

Please follow the style guidelines specified in the `.editorconfig` file.

File names should generally be in lowercase with dashes separating words (lower-kebab-case) unless
there are strong conventions for the language or framework (e.g. PascalCase for C# classes).

Files should be kept brief and focused wherever possible. The goal is to keep files to 500 lines or
less. Files should not exceed 1000 lines unless absolutely necessary.

Comments should generally use normal casing unless there is a conflicting convention. Use
punctuation as appropriate (e.g. period only for complete sentences, etc.).

All files should be linted and formatted when editing. The repository's npm-based linters (cspell,
markdownlint-cli2, prettier) are pinned via per-tool `.<tool>-version` files and run through
`./scripts/lint-files.sh` (use `--fix` to auto-fix where possible). CI runs the same versions.

A custom cspell dictionary is maintained at `.cspell.json`. Please feel free to add words to this as
needed.

Do not use em dashes in prose, IN ANY FORM: not the Unicode character, and not the `--` double-dash
imitation. Both count as em dashes under this rule. Use a comma, colon, semicolon, parentheses, or
two sentences instead. This applies everywhere prose lives: docs, code comments, docstrings, commit
messages, and operator-facing strings. Literal `--` that is not punctuation is fine (CLI flags like
`--site`, YAML document separators, code operators, decorative section-divider comments). Avoid
other typographic special characters (curly quotes, ellipsis characters, arrows) for the same
reason: write like a programmer in an IDE. A lot of existing text predates this clarification; fix
violations opportunistically when editing nearby rather than in bulk sweeps.
