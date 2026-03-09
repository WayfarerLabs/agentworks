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

All files should be linted and formatted when editing. Please see the specific skills for additional
details:

- Markdown - `lint-fmt-markdown` skill
- HCL - `lint-fmt-hcl` skill

Please use cspell to check spelling, especially in documentation. A custom dictionary is maintained
at `.cspell.json`. Please feel free to add words to this as needed. Use `gwat-pkgrun-cspell` if
available, otherwise `npx cspell@8.19.4`.

Do not use em dashes, double dashes, or other special characters. Write like a programmer in an IDE.
