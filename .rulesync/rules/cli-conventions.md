---
description: "CLI command shape and naming conventions"
globs: ["**/cli.py", "**/completions/**/*.py", "**/agentworks/**/manager.py"]
---

# CLI Conventions

The Agentworks CLI follows a small, opinionated set of shape rules. They are listed here so new
commands compose naturally with what already exists rather than inventing their own dialect.

## Verb pattern: `<resource> <verb>` or `<resource> <verb>-<object>`

Two shapes, depending on whether a second object is involved.

**Operations on the resource itself** use a single verb: `<resource> <verb>`. Examples:

- `agent create`
- `agent delete`
- `agent describe`
- `vm reinit`
- `workspace list`
- `console attach`

**Operations involving a second object** make the object explicit in the verb:
`<resource> <verb>-<object>`. Examples:

- `console add-session` (verb `add` operates on `session` inside the console)
- `console remove-session`
- `console add-shell`
- `console restore-session`
- `agent grant-workspace`
- `agent revoke-workspace`

Do not introduce a multi-word subcommand group (e.g. `agent workspace-grants`) just to host a small
family of related verbs. The flat `<resource> <verb>-<object>` form is more discoverable, has
shorter command depth, and matches the rest of the surface. If a future command pair needs the same
object (`agent suspend-workspace` / `agent resume-workspace`), the verb-object form scales
naturally.

## Positional vs option

- **Positional arguments** for required things: names, IDs, lists of things being operated on.
- **Options** (`--flag` / `--key value`) for modifiers, mode switches, and optional config.
- **Variadic positionals** for lists. `agent grant-workspace my-agent ws1 ws2 ws3`, not a single
  comma-separated string. This gives operators shell-completion past the first item and avoids
  quoting hazards.

## Bulk flags

Bulk operations use `--all` as a single, consistent flag name, never the more verbose form. So
`agent grant-workspace my-agent --all`, not `--all-workspaces` or `--every-workspace`. The
surrounding command provides the context for what "all" applies to.

## Service layer is the authority

CLI command bodies should be thin: argv to kwargs, then call the service-layer function on the
relevant manager. Validation, error shaping, and business logic live in the manager. If the CLI
finds itself re-implementing a check the manager already does (e.g. "refuse empty input"), let the
manager raise and propagate. The contract: service-layer functions raise typed `AgentworksError`
subclasses from `agentworks.errors`, organized by _kind_ of error (`NotFoundError`,
`AlreadyExistsError`, `ValidationError`, `StateError`, `ConnectivityError`, `ExternalError`,
`ConfigError`, `UserAbort`); the entity dimension (vm, workspace, agent, session, console) is
carried as the `entity_kind` / `entity_name` attributes on the exception, not as the type. The CLI
catches and renders them; no `typer.echo`, `print`, or `typer.Exit` from manager modules. See the
`agentworks-reviewer` rubric for the full check.

## When in doubt

Look at the closest existing command in the same resource group and match its shape. Consistency
across the surface is the strongest signal an operator gets that they're typing a real command.
