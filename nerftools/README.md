# nerftools

Build and manage nerf tools: defanged, scoped wrappers for AI agent use.

Nerf tools wrap CLI commands with safety guardrails: validated parameters, restricted flags,
pre-flight checks, and a 2D threat model for permission management. A Python CLI reads YAML
manifests and generates self-contained bash scripts, rulesync skills, and Claude Code plugins.

**IMPORTANT: Nerf tools should not be considered universally safe.** Different tools have different
threat profiles. Rather, the goal is that each nerf tool should limit usage of the underlying CLI
tool to a set of operations with a roughly-equivalent threat profile (as expressed in its declared
threat model) so that permissions can be broadly granted (e.g. in tools like Claude Code) with
confidence that the agent can't perform operations outside the declared threat profile.

## Quick start

```bash
# Validate manifests
uv run nerf validate

# Generate executable scripts
uv run nerf generate --target bin --outdir ./bin

# Generate rulesync skills
uv run nerf generate --target skills --outdir ./skills

# Generate a Claude Code plugin
uv run nerf generate --target claude-plugin --outdir ./claude-plugin
```

## Manifests

A manifest is a single YAML file that declares tools within a package. Multiple manifests can
contribute to the same package, with tools merged using last-wins semantics. Each tool uses one of
three execution modes:

- **template**: build a command from explicit parameters and a `{{kind.name}}` template
- **passthrough**: forward all tokens after a deny-list scan
- **script**: run an inline bash script

No special directory structure is required. A manifest is just a `.yaml` file passed to the CLI.

The `default-manifests/` directory contains the manifests that ship with nerftools. These are
included automatically unless `--no-default` is passed. Custom manifests can be added alongside or
instead of the defaults:

```bash
# Defaults + your custom manifest
uv run nerf generate --target bin --outdir ./bin path/to/my-tools.yaml

# Only your custom manifests, skip defaults
uv run nerf generate --target bin --outdir ./bin --no-default path/to/my-tools.yaml

# Validate a custom manifest in isolation
uv run nerf validate --no-default path/to/my-tools.yaml
```

When both default and custom manifests define tools in the same package (same `package.name`),
tools are merged at the individual tool level with last-wins semantics. A custom `git-commit`
replaces the default `git-commit`, but the other default git tools remain. Package metadata
(description, skill_group, skill_intro) is kept from the first manifest that defines the package.

See [docs/guides/nerf-manifest.md](../docs/guides/nerf-manifest.md) for the full manifest
reference.

## Default packages

| Package      | Tools | Description                                                      |
| ------------ | ----- | ---------------------------------------------------------------- |
| git          | 11    | Git workflow with commit, push, fetch, tag, amend, revert, reset |
| az-repos     | 3     | Azure Repos PR management                                        |
| az-pipelines | 3     | Azure Pipelines monitoring                                       |
| az-boards    | 7     | Azure Boards work items (query, view, create, update)            |
| nx           | 6     | Nx monorepo workspace operations                                 |
| tg           | 10    | Terragrunt infrastructure management                             |
| pkgrun       | 3     | npm package runners (cspell, markdownlint, prettier)             |
| stdutils     | 4     | Unix utilities (find, grep) with safety guardrails               |
| uv           | 4     | Python dev tools via uv run (pytest, ruff, mypy)                 |

## Threat model

Every tool declares what it reads and writes using a 2D threat profile:

```yaml
threat:
  read: workspace # none | workspace | machine | remote | admin
  write: remote # none | workspace | machine | remote | admin
```

Operators grant permissions by threat ceiling rather than enumerating tools:

```bash
# Allow all tools that read/write within the workspace
nerfctl-grant-by-threat --read workspace --write workspace

# Also allow remote-read tools (e.g. git fetch, az boards)
nerfctl-grant-by-threat --read remote --write workspace
```

## Development

```bash
cd nerftools

# Install dependencies and run tests
uv run pytest tests/ -v

# Lint
uv run ruff check nerftools/ tests/
uv run mypy nerftools/

# Validate all default manifests
uv run nerf validate
```

Requires Python 3.12+ (pinned via `.python-version`).

## Project structure

```text
nerftools/
  nerftools/           Python package
    manifest.py        Data model, loading, validation
    builder.py         Bash script generation (3 execution modes)
    rendering.py       Shared display helpers (maps-to, usage tokens)
    skill.py           Rulesync skill generation
    formats.py         Claude Code plugin builder
    cli.py             CLI (validate + generate)
    nerfctl/claude/    Grant management shell scripts
  default-manifests/   Default tool package manifests
  tests/               Test suite
```
