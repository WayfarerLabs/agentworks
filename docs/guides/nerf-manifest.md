# Nerf Manifest Reference

A nerf manifest is a YAML file that declares tools within a package. Multiple manifests can
contribute to the same package. When they share a `package.name`, tools are merged with last-wins
semantics. The manifest is the source of truth for tool definitions, parameter specs, safety
guardrails, threat metadata, and AI skill documentation.

Each tool defines exactly **one execution mode** (`template`, `passthrough`, or `script`) plus
optional lifecycle hooks and shared configuration. The `nerf` CLI reads manifests and generates
self-contained bash scripts, rulesync skills, and Claude Code plugins.

Generated scripts target **bash** (not POSIX sh).

## Quick start

A minimal manifest with one tool:

```yaml
version: 1

package:
  name: git
  description: Git workflow tools
  skill_group: git

tools:
  git-log:
    description: Show recent commits
    threat:
      read: workspace
      write: none
    template:
      command: [git, log, --oneline, -20]
```

Validate and generate:

```bash
nerf validate manifest.yaml
nerf generate --target bin --outdir ./bin manifest.yaml
```

## Versioning

Every manifest declares its spec version as a top-level integer:

```yaml
version: 1
```

- The `version` field is required.
- The value is a single integer (major version only).
- The current version is **1**.
- The CLI validates against the declared version's schema.
- Backwards-incompatible changes require a version bump. Additive changes (new optional fields)
  do not.

## Terminology

These terms are used consistently throughout manifests, generated scripts, and generated
documentation.

### Token

A **token** is a single raw element of `$@`, one shell argument as received by the script. Before
parsing, every `$1`, `$2`, etc. is just a token.

Passthrough mode operates **exclusively at the token level**. It never classifies tokens; it scans
them against deny rules and forwards them as-is.

### Parameter types (template and script modes)

| Term | What it is | Tokens consumed | Syntax | Example |
|---|---|---|---|---|
| **switch** | Boolean flag. Present or absent, no value. | 1 | `--flag` or `-f` | `--verbose` |
| **option** | Named flag + value (two tokens: **option flag** and **option value**). | 2 | `--flag <value>` | `--remote origin` |
| **argument** | Positional value identified by position, not by a flag. | 1 | `<value>` | `origin` |

### Prefix and suffix tokens (passthrough mode)

**Prefix tokens** are static tokens inserted before the user's tokens. **Suffix tokens** are
inserted after. Both are hardcoded in the manifest.

```text
exec <command> <prefix...> "$@" <suffix...>
```

### Invocation order

Switches and options must come before arguments. The generated parser stops consuming flags at the
first non-flag token (or `--`).

```text
nerf-tool [switches] [options] [--] <arguments...>
```

A **variadic argument** collects all remaining positional values into a bash array. At most one
variadic argument is allowed per tool, and it must be the last argument.

## Package

Top-level metadata about the tool package.

```yaml
package:
  name: <string>            # Unique package identifier (e.g. "git", "nx")
  description: <string>     # Human-readable description of the package
  skill_group: <string>     # Skill directory name (usually matches name)
  skill_intro: <string>     # Optional multi-line intro for AI skill docs
```

All fields except `skill_intro` are required.

## Tool definition

```yaml
tools:
  <tool-name>:
    description: <string>           # Required. What the tool does.
    threat:                         # Required. Risk classification.
      read: <scope>                 #   none|workspace|machine|remote|admin
      write: <scope>                #   none|workspace|machine|remote|admin

    # Execution mode (exactly one required):
    template:    { ... }            # Build a command from a template
    passthrough: { ... }            # Forward tokens with a deny-list
    script: <string>                # Inline bash script

    # Parameters (optional, template and script only):
    switches: { ... }
    options:  { ... }
    arguments:  { ... }

    # Lifecycle (optional, all modes):
    pre: <string>                   # Inline bash run before main execution
    guards:                         # Pre-flight boolean checks
      - { ... }

    # Environment (optional):
    env: { ... }                    # Key-value pairs exported before execution
```

- Exactly one of `template`, `passthrough`, or `script` must be set.
- `switches`, `options`, and `arguments` are **not allowed** with `passthrough`.

## Threat model

Every tool declares a two-dimensional threat profile: what it **reads** and what it **writes**. This
metadata enables operators to grant permissions by risk scope rather than enumerating individual
tools.

```yaml
threat:
  read: <scope>
  write: <scope>
```

The threat profile is metadata only and does not change what the tool does. Enforcement is at the
permission layer (nerfctl grant commands, settings.json).

### Scopes (ordered, narrow to broad)

| Scope | Meaning |
|---|---|
| `none` | No access in this dimension. |
| `workspace` | Confined to the current workspace directory tree. |
| `machine` | Anywhere on the local filesystem. |
| `remote` | Network operations: APIs, remote repos, cloud services. |
| `admin` | Destructive, irreversible, or elevated operations. |

### Examples

| Tool | Read | Write | Rationale |
|---|---|---|---|
| `git log` | workspace | none | Reads repo history, writes nothing |
| `git add` | workspace | workspace | Reads and modifies the staging area |
| `git fetch` | remote | workspace | Downloads from remote, writes to local refs |
| `git push` | workspace | remote | Reads local refs, writes to remote |
| `find .` | workspace | none | Reads directory tree under workspace root |
| `cspell` | workspace | none | Reads files, reports issues |
| `prettier --write` | workspace | workspace | Reads and reformats files |

### Scope comparison

Scopes are ordered: `none < workspace < machine < remote < admin`. A tool is within a grant ceiling
when `tool.read <= ceiling.read AND tool.write <= ceiling.write`.

### Embedded metadata

Generated scripts include the threat profile as structured comments:

```bash
#!/usr/bin/env bash
# nerf-git-push-branch -- Push the current branch to a remote
# Generated from git manifest. Do not edit directly.
# nerf:threat:read=workspace
# nerf:threat:write=remote
```

This enables runtime tool discovery and threat classification without the manifest.

## Execution modes

### template

Build a command from an explicit template with `{{kind.name}}` placeholders. Best for wrapping a single
tool call where you want full control over exposed parameters.

```yaml
template:
  command: [<string>, ...]        # Command parts with {{kind.name}} placeholders
  npm_pkgrun: <bool>              # Use bunx/pnpx/npx resolver (default: false)
```

Rules:

- Every `{{kind.name}}` in `command` must be defined in `switches`, `options`, or `arguments`.
- Every parameter must be referenced by a `{{kind.name}}` in `command`.
- A variadic argument's `{{kind.name}}` must be the last element of `command`.
- The generated script ends with `exec`, replacing the process.

Example:

```yaml
git-commit:
  description: Create a git commit with a Conventional Commits message
  threat:
    read: workspace
    write: workspace
  template:
    command: [git, commit, -m, "{{arguments.message}}"]
  arguments:
    message:
      description: "Commit message: type[(scope)][!]: description"
      required: true
      pattern: "^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\\([a-zA-Z0-9._-]+\\))?!?: .+"
  guards:
    - script: "! git diff --cached --quiet"
      fail_message: No staged changes. Use git-add to stage changes first.
```

### passthrough

Forward all tokens to an underlying command, rejecting any that match a deny list. Best for exposing
a tool mostly as-is while removing dangerous capabilities.

Passthrough mode operates **exclusively at the token level**. There is no parsing, no classification
into switches/options/arguments. Every element of `$@` is scanned against deny rules and forwarded
as-is.

```yaml
passthrough:
  command: <string>               # Underlying command name
  deny: [<string>, ...]           # Glob patterns to reject (optional)
  prefix: [<string>, ...]         # Static tokens before user tokens (optional)
  suffix: [<string>, ...]         # Static tokens after user tokens (optional)
```

**Deny list matching:** each entry is a glob pattern matched against every token. Glob
metacharacters are `*`, `?`, `[`, `]`. A pattern without metacharacters is an exact match.

| Pattern | Matches | Does not match |
|---|---|---|
| `--delete` | `--delete` | `--delete-all` |
| `-ok*` | `-ok`, `-okdir` | `--ok` |

To match a literal metacharacter, escape with `\`. In YAML, use single quotes (`'\*'`) or double
the backslash (`"\\*"`).

Example:

```yaml
safe-find:
  description: Run find without exec capabilities
  threat:
    read: workspace
    write: none
  passthrough:
    command: find
    deny: [-exec, -execdir, -ok, -okdir, -delete]
    prefix: [.]
```

### script

Run an inline bash script. Best for tools that need custom logic beyond wrapping a single command.

```yaml
script: |
  <bash script body>
```

- Parameters are parsed and available as shell variables before the script runs.
- The script runs in the main shell (not a subshell). Its exit code becomes the tool's exit code.
- There is no `exec`. The script controls its own flow.

Example:

```yaml
deploy-check:
  description: Validate deployment readiness
  threat:
    read: remote
    write: remote
  script: |
    echo "Checking ${ENVIRONMENT}..."
    if [ "$DRY_RUN" = "true" ]; then
      echo "(dry run)" >&2
      exit 0
    fi
    terraform plan -detailed-exitcode
    rc=$?
    if [ $rc -eq 2 ]; then
      echo "Changes pending — review required" >&2
      exit 1
    fi
  arguments:
    environment:
      description: Target environment
      required: true
      allow: [staging, production]
  switches:
    dry_run:
      description: Skip actual checks
```

## Parameter specs

Parameters are only allowed with `template` and `script` modes. Names must not overlap across
`switches`, `options`, and `arguments` within a single tool.

All parameter values are stored as shell variables with the name uppercased and dashes replaced by
underscores (e.g. `my-param` becomes `MY_PARAM`).

### switches

A boolean flag, present or absent, with no value. Switches are always optional.

```yaml
switches:
  <name>:
    description: <string>         # Required
    flag: <string>                # Override flag (default: --<name> with _ replaced by -)
    short: <string>               # Single-char short form (e.g. -v)
    repeatable: <bool>            # Can be passed multiple times (default: false)
```

The shell variable is `"true"` when present, `""` when absent. For repeatable switches, the
variable is an integer count (0 when absent, incremented each time the switch is passed).

Rules:

- `flag` must match `-<name>` or `--<name>` pattern.
- `short` must match `-[a-zA-Z]`.

### options

A named flag that takes exactly one value.

```yaml
options:
  <name>:
    description: <string>         # Required
    flag: <string>                # Override flag (default: --<name> with _ replaced by -)
    short: <string>               # Single-char short form
    required: <bool>              # Default: false
    repeatable: <bool>            # Can be passed multiple times (default: false)
    pattern: <string>             # Regex the value must match (auto-anchored with ^...$)
    allow: [<string>, ...]        # Exhaustive list of allowed values
    deny:  [<string>, ...]        # Values to reject
```

Rules:

- `allow` and `deny` are mutually exclusive.
- `flag` must match `-<name>` or `--<name>` pattern.
- `short` must match `-[a-zA-Z]`.
- `pattern` is automatically anchored to a full match in the generated bash script.
- When `repeatable: true`, the option can be passed multiple times. The generated script
  accumulates flag-value pairs in an array so `"${VAR[@]}"` expands to `--flag val1 --flag val2`.

### arguments

A positional argument identified by position.

```yaml
arguments:
  <name>:
    description: <string>         # Required
    required: <bool>              # Default: false
    variadic: <bool>              # Collect remaining args into array (default: false)
    allow_flags: <bool>           # Allow flag-like tokens in variadic args (default: false)
    pattern: <string>             # Regex the value must match (auto-anchored)
    allow: [<string>, ...]        # Exhaustive list of allowed values
    deny:  [<string>, ...]        # Values to reject
```

Rules:

- `allow` and `deny` are mutually exclusive.
- At most one argument may be `variadic`, and it must be the last in `arguments`.
- In `template` mode, a variadic `{{kind.name}}` must be the last element in `template.command`.
- Variadic arguments become bash arrays; all others become scalar variables.
- By default, variadic arguments reject tokens starting with `-` to prevent flag injection. Set
  `allow_flags: true` when forwarding to a tool that expects its own flags (e.g. pytest, ruff).

## Lifecycle

### Execution order

```text
guards → pre → main (template exec | passthrough exec | script run)
```

### guards

Pre-flight boolean checks that run before the main execution.

```yaml
guards:
  - fail_message: <string>       # Required. Shown on failure.
    command: [<string>, ...]      # Run as subprocess, output suppressed
    # OR
    script: <string>             # Inline bash snippet
```

Exactly one of `command` or `script`. The check passes on exit code 0.
`{{kind.name}}` placeholders are substituted with parameter values.

### pre

An inline bash script that runs before the main execution mode. Use it for setup, variable
computation, and conditional abort.

```yaml
pre: |
  <bash script body>
```

The pre script is wrapped in a shell function (`_nerf_pre`). Key points:

- **Use `return` to abort, not `exit`.** `return 1` lets the wrapper report the failure. `exit`
  kills the entire script immediately, bypassing error reporting.
- **Print your own error messages.** The fallback message is generic. Write
  `echo "error: ..." >&2` before `return 1`.
- **Shell variables set in pre are visible to main.** Functions execute in the caller's scope.
- **`{{kind.name}}` placeholders work.** Parameters are parsed before pre runs.

Example:

```yaml
git-push-branch:
  description: Push the current branch to a remote
  threat:
    read: workspace
    write: remote
  template:
    command: [git, push, --follow-tags, "{{arguments.remote}}", HEAD]
  arguments:
    remote:
      description: Remote name
      required: true
      pattern: "^[a-z0-9_-]+$"
  pre: |
    BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null)
    if [ -z "$BRANCH" ]; then
      echo "error: cannot push from detached HEAD state" >&2
      return 1
    fi
    if [ "$BRANCH" = "main" ]; then
      echo "error: use git-push-main for the main branch" >&2
      return 1
    fi
```

## Environment

```yaml
env:
  VAR_NAME: "value"
```

Key-value pairs exported as environment variables before guards, pre, and main execution. Values are
static strings (no `{{kind.name}}` substitution). Keys must match `[A-Z_][A-Z0-9_]*`.

## Placeholder substitution

Placeholders use the form `{{kind.name}}` where `kind` is one of `switches`, `options`, or
`arguments`. They appear in `template.command`, `guard.command`, `guard.script`, and `pre` and are
replaced with shell variable references.

| Context | Required scalar | Optional scalar | Required variadic | Optional variadic | Switch | Repeatable option |
|---|---|---|---|---|---|---|
| template command | `"$VAR"` | `--flag $VAR` (conditional) | `"${VAR[@]}"` | `${VAR[@]+"${VAR[@]}"}` | `${VAR:+"--flag"}` | `"${VAR[@]}"` (flag-value pairs) |
| guard/pre script | `${VAR}` | `${VAR}` | `${VAR}` | `${VAR}` | `${VAR}` | `${VAR}` |

In script contexts (guards, pre, inline script body), the author is responsible for quoting.

## Error messages

Generated scripts produce structured errors for both humans and AI agents:

```text
error: <tool-name>: <what went wrong>
  <detail lines>
  hint: <what to do instead>
```

The first line always starts with `error: <tool-name>:` so the caller can identify which tool
failed. Examples:

```text
error: nerf-git-commit: missing required argument <message>
  hint: provide a value for <message>

error: nerf-git-commit: argument <message> does not match required pattern
  value:   "updated readme"
  pattern: ^(feat|fix|docs|...)...
  hint: value must match ^(feat|fix|docs|...)...

error: nerf-safe-find: token '-exec' is not allowed (matched deny pattern '-exec')
  denied patterns: -exec, -execdir, -ok*, --delete
  hint: remove '-exec' and retry
```

## Dry-run mode

Every generated tool supports `--nerf-dry-run`. When passed as the first argument, the tool runs
all validation, guards, pre-hooks, and deny scans as normal, but instead of executing the final
command it prints what would be run and exits.

```bash
$ nerf-git-fetch --nerf-dry-run origin
dry-run: git fetch origin --tags

$ nerf-find-cwd --nerf-dry-run -name '*.py'
dry-run: find '.' -name *.py

$ nerf-deploy-check --nerf-dry-run staging
dry-run: nerf-deploy-check would run inline script
```

If a guard or deny rule would reject the invocation, the error is reported as usual:

```bash
$ nerf-find-cwd --nerf-dry-run -exec echo {} \;
error: nerf-find-cwd: token '-exec' is not allowed (matched deny pattern '-exec')
```

`--nerf-dry-run` must be the first token because the parser stops consuming flags at the first
unrecognized argument.

## Generated documentation

### Maps-to line

Every template and passthrough tool shows a **Maps to** line revealing the underlying command:

- **Template:** `Maps to: git commit -m <message>`
- **Passthrough:** `Maps to: find . "$@"`
- **Script:** no Maps-to line.

### Help text (--help)

Generated scripts include a `usage()` function with separate Switches, Options, and Arguments
sections, constraints shown inline, and the Maps-to line.

### AI skill files (SKILL.md)

Generated skill files follow the same structure formatted as markdown for AI assistants.

## Validation summary

| Rule | Scope |
|---|---|
| `version` is required and must be an integer | manifest |
| `threat.read` and `threat.write` required | tool |
| Exactly one of `template`, `passthrough`, `script` | tool |
| `switches`/`options`/`arguments` not allowed with `passthrough` | tool |
| `{{kind.name}}` refs must exist in switches/options/arguments | template, guards, pre |
| All switches/options/arguments must be referenced in `{{kind.name}}` | template only |
| Variadic argument must be last in `arguments` | arguments |
| Variadic `{{kind.name}}` must be last element in `template.command` | template |
| `allow` and `deny` are mutually exclusive | options, arguments |
| Switch/option/argument names must not overlap | tool |
| `flag` matches `-<name>` or `--<name>` pattern | switches, options |
| `short` matches `-[a-zA-Z]` | switches, options |
| `pattern` is a valid regex | options, arguments |
| `env` keys match `[A-Z_][A-Z0-9_]*` | env |
| Guard has exactly one of `command` or `script` | guards |
