# Nerf Manifest Specification (v1)

A nerf manifest is a YAML file that declares a family of scoped tool wrappers.
It is the single source of truth for tool definitions, parameter specs, safety
guardrails, and AI skill metadata.

Each tool defines exactly **one execution mode** — `template`, `passthrough`, or
`script` — plus optional lifecycle hooks and shared configuration.

Generated scripts target **bash** (not POSIX sh).

---

## Versioning

Every manifest declares its spec version as a top-level integer:

```yaml
version: 1

package:
  name: git
  ...
```

**Rules:**

- The `version` field is required.
- The value is a single integer (major version only). There are no minor or
  patch versions — the spec is either compatible or it isn't.
- The current version is **1**. This is the first versioned manifest format.
  Prior unversioned manifests are not supported — they must be migrated.
- The CLI validates against the declared version's schema.
- Backwards-incompatible changes require a version bump. Additive changes (new
  optional fields) do not.

---

## Terminology

The terminology below is strict throughout this spec, in generated scripts,
and in generated help text. Use these terms precisely.

### Token

A **token** is a single raw element of `$@` — one shell argument as received
by the script. Before parsing, every `$1`, `$2`, etc. is just a token. Parsing
classifies tokens into the categories below.

Passthrough mode operates **exclusively at the token level**. It never
classifies tokens — it scans them against deny rules and forwards them as-is.

### Parameter types (template and script modes)

| Term | What it is | Tokens consumed | Syntax | Example |
|---|---|---|---|---|
| **switch** | A boolean flag. Present or absent, no value. | 1 (the switch itself) | `--flag` or `-f` | `--verbose`, `-v` |
| **option** | A named flag that takes exactly one value, expressed as two tokens: the **option flag** and the **option value**. | 2 (option flag + option value) | `--flag <value>` or `-f <value>` | `--remote origin` |
| **argument** | A positional value identified by position, not by a flag. | 1 | `<value>` | `origin`, `src/` |

The parts of an option:

- **option flag** — the name token (e.g. `--remote`, `-r`)
- **option value** — the data token that follows (e.g. `origin`)

### Prefix and suffix tokens (passthrough mode)

In passthrough mode, **prefix tokens** are static tokens inserted before the
user's tokens and **suffix tokens** are static tokens inserted after. These
are hardcoded in the manifest and always present in the final command.

```text
exec <command> <prefix tokens...> "$@" <suffix tokens...>
```

### Invocation order

**Switches and options must come before arguments.** The generated parser stops
consuming flags at the first non-flag token (or `--`).

```text
nerf-tool [switches] [options] [--] <arguments...>
```

A **variadic argument** collects all remaining positional values into a bash
array. At most one variadic argument is allowed per tool, and it must be the
**last argument** in the tool definition **and** the last `{{param}}` in the
template command. This is validated.

---

## Package

Top-level metadata about the tool family.

```yaml
package:
  name: <string>            # Unique package identifier (e.g. "git", "nx")
  description: <string>     # Human-readable description of the family
  skill_group: <string>     # Skill directory name (usually matches name)
  skill_intro: <string>     # Optional multi-line intro for AI skill docs
```

All fields except `skill_intro` are required.

---

## Tool definition

```yaml
tools:
  <tool-name>:
    description: <string>           # Required. What the tool does.
    threat:                         # Required. Risk classification.
      read: <scope>                 #   Read scope (none|workspace|machine|remote|admin)
      write: <scope>                #   Write scope (none|workspace|machine|remote|admin)

    # Execution mode (exactly one required):
    template:    { ... }            # Build a specific invocation from a template
    passthrough: { ... }            # Forward args with a deny-list
    script: <string>                # Inline bash script

    # Parameters (optional, allowed with template and script):
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

**Validation:**

- Exactly one of `template`, `passthrough`, or `script` must be set.
- `switches`, `options`, and `arguments` are allowed with `template` and
  `script` modes.  They are **not allowed** with `passthrough` (passthrough
  treats all input as opaque tokens to scan and forward).

---

## Threat model

Every tool declares its threat profile as two independent dimensions: what it
**reads** and what it **writes**. The threat profile is metadata — it does not
change what the tool does. Enforcement is at the permission layer (nerfctl,
settings.json, etc.).

```yaml
threat:
  read: <scope>
  write: <scope>
```

### Scopes (ordered, narrow to broad)

| Scope | Meaning |
|---|---|
| `none` | No access. The tool does not read or write in this dimension. |
| `workspace` | Confined to the current workspace directory tree. |
| `machine` | Anywhere on the local filesystem, outside the workspace. |
| `remote` | Network operations — APIs, remote repos, cloud services. |
| `admin` | Destructive, irreversible, or elevated operations (e.g. permission changes, system config). |

### Examples

| Tool | Read | Write | Rationale |
|---|---|---|---|
| `git log` | workspace | none | Reads repo history, writes nothing |
| `git add` | workspace | workspace | Reads and modifies the staging area |
| `git commit` | workspace | workspace | Reads staged changes, writes a commit |
| `git fetch` | remote | workspace | Downloads from remote, writes to local refs |
| `git push` | workspace | remote | Reads local refs, writes to remote |
| `find .` | workspace | none | Reads directory tree under workspace root |
| `find /` | machine | none | Reads anywhere on the filesystem |
| `nx run` | workspace | workspace | Reads source, writes build output |
| `tg plan` | remote | none | Reads remote state, proposes changes |
| `tg apply` | remote | remote | Reads and writes remote infrastructure |
| `az boards wi show` | remote | none | Reads work items from Azure |
| `az repos pr create` | remote | remote | Reads repo, creates remote PR |
| `cspell` | workspace | none | Reads files, reports issues |
| `prettier --write` | workspace | workspace | Reads and reformats files |

### Scope ordering and comparison

Scopes are ordered from narrow to broad:

```text
none < workspace < machine < remote < admin
```

A tool's threat profile can be compared against a ceiling using `<=` on both
dimensions independently. A tool is "within" a given boundary when:

```text
tool.read  <= ceiling.read  AND  tool.write <= ceiling.write
```

**Examples of boundary checks:**

| Ceiling | Inside | Outside |
|---|---|---|
| read:workspace, write:workspace | git log, git add, git commit, cspell, prettier | git push, git fetch, find /, tg plan |
| read:machine, write:workspace | All of the above + find / | git push, git fetch, tg plan |
| read:remote, write:workspace | All of the above + git fetch, tg plan, az boards wi show | git push, tg apply, az repos pr create |
| read:remote, write:remote | Everything except admin | admin-level tools |

See the FRD (R5) for how threat profiles are used in grant commands.

### Embedded metadata in generated scripts

Generated scripts include the threat profile as structured comments in the
header, immediately after the description and source lines:

```bash
#!/usr/bin/env bash
# nerf-git-push-branch -- Push the current branch to a remote
# Generated from git manifest. Do not edit directly.
# nerf:threat:read=workspace
# nerf:threat:write=remote
```

The `# nerf:threat:` prefix is a structured tag. Each tag is a separate
comment line with a `key=value` pair. This metadata enables runtime tool
discovery and threat classification without requiring the manifest to be
present.

**Discovery:** scan for executable files under a root directory, grep for
`# nerf:threat:` lines, parse key=value pairs. This works with any directory
nesting (flat bin/, nested skills/*/scripts/, etc.).

### Rules

- The `threat` field is required on every tool definition.
- Both `read` and `write` are required within `threat`.
- Values must be one of: `none`, `workspace`, `machine`, `remote`, `admin`.
- Generated scripts must include `# nerf:threat:read=<scope>` and
  `# nerf:threat:write=<scope>` comment lines.

---

## Execution modes

### template

Build a command invocation from an explicit template with `{{param}}`
placeholders. Best for wrapping a single tool call where you want full control
over which parameters are exposed.

```yaml
template:
  command: [<string>, ...]        # Command parts with {{param}} placeholders
  npm_pkgrun: <bool>              # Use bunx/pnpx/npx resolver (default: false)
```

**Rules:**

- Every `{{param}}` in `command` must be defined in `switches`, `options`, or
  `arguments`.
- Every entry in `switches`, `options`, and `arguments` must be referenced by a
  `{{param}}` in `command`.
- If a variadic argument exists, its `{{param}}` placeholder must be the **last
  element** of the `command` list. This ensures the array expands at the tail.
- The generated script ends with `exec`, replacing the process.

**Example:**

```yaml
git-commit:
  description: >-
    Create a git commit with a Conventional Commits message.
  template:
    command: [git, commit, -m, "{{message}}"]
  arguments:
    message:
      description: "Commit message: type[(scope)][!]: description"
      required: true
      pattern: "^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\\([a-zA-Z0-9._-]+\\))?!?: .+"
  guards:
    - script: "! git diff --cached --quiet"
      fail_message: No staged changes. Use git-add to stage changes first.
```

**Example with variadic argument (must be last):**

```yaml
git-add:
  description: Stage files or directories for commit
  template:
    command: [git, add, "{{files}}"]
  arguments:
    files:
      description: Files or directories to stage
      required: true
      variadic: true
```

---

### passthrough

Forward all tokens to an underlying command, rejecting any that match a deny
list. Best for exposing a tool mostly as-is while removing dangerous
capabilities.

Passthrough mode operates **exclusively at the token level**. There is no
parsing, no classification into switches/options/arguments — every element of
`$@` is an opaque token scanned against deny rules and forwarded as-is. This
is intentionally simple.

```yaml
passthrough:
  command: <string>               # Underlying command name
  deny: [<string>, ...]           # Glob patterns to reject (optional)
  # deny_regex: [<string>, ...]   # Reserved for future regex support
  prefix: [<string>, ...]         # Static tokens before user tokens (optional)
  suffix: [<string>, ...]         # Static tokens after user tokens (optional)
```

**Deny list matching:**

Each entry in `deny` is a **glob pattern** matched against every token in `$@`.
Glob metacharacters are `*`, `?`, `[`, and `]`. A pattern without
metacharacters is an exact match.

| Pattern | Matches | Does not match |
|---|---|---|
| `--delete` | `--delete` | `--delete-all` |
| `-ok*` | `-ok`, `-okdir` | `--ok` |
| `--format=[jJ]*` | `--format=json`, `--format=JSON` | `--format=csv` |

To match a literal metacharacter, escape it with `\` (e.g. `\*`, `\?`,
`\[`). In YAML, backslashes in unquoted or double-quoted strings need
doubling (`"\\*"`), or use single quotes (`'\*'`).

**Rules:**

- `switches`, `options`, and `arguments` are **not allowed** in passthrough
  mode. No parameter parsing occurs.
- The generated script ends with `exec`, replacing the process.
- `pre` and `guards` are still available (they operate on raw `$@`).

**Error messages:**

Runtime errors must be clear enough for both humans and AI agents to
understand what went wrong and how to fix it. Denied token errors include:

1. The tool name
2. The rejected token
3. The deny pattern that matched
4. The full list of deny patterns (so the caller knows what else to avoid)

Format:

```text
error: <tool-name>: token '<rejected>' is not allowed (matched deny pattern '<pattern>')
  denied patterns: <pattern1>, <pattern2>, ...
  hint: remove '<rejected>' and retry
```

**Generated script logic:**

```bash
_NERF_DENY_PATTERNS=(-exec -execdir '-ok*' --delete)

for _tok in "$@"; do
  for _pat in "${_NERF_DENY_PATTERNS[@]}"; do
    case "$_tok" in
      $_pat)
        echo "error: <tool-name>: token '$_tok' is not allowed (matched deny pattern '$_pat')" >&2
        echo "  denied patterns: ${_NERF_DENY_PATTERNS[*]}" >&2
        echo "  hint: remove '$_tok' and retry" >&2
        exit 1
        ;;
    esac
  done
done
exec <command> <prefix tokens...> "$@" <suffix tokens...>
```

**Example:**

```yaml
safe-find:
  description: Run find without exec capabilities
  passthrough:
    command: find
    deny: [-exec, -execdir, -ok, -okdir, -delete]
    prefix: [.]
```

---

### script

Run an inline bash script. Best for tools that need custom logic beyond
wrapping a single command invocation.

```yaml
script: |
  <bash script body>
```

**Rules:**

- `switches`, `options`, and `arguments` are parsed and validated before the
  script runs. They are available as shell variables.
- The script runs in the main shell (not a subshell). Its exit code becomes the
  tool's exit code.
- There is no `exec` — the script is responsible for its own control flow.

**Example:**

```yaml
deploy-check:
  description: Validate deployment readiness
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

---

## Parameter specs

Parameters are only allowed with `template` and `script` modes. Names must not
overlap across `switches`, `options`, and `arguments` within a single tool.

All parameter values are stored as shell variables with the name uppercased and
dashes replaced by underscores (e.g. `my-param` → `MY_PARAM`).

### switches

A switch is a boolean flag — present or absent, no value. Switches are always
optional.

```yaml
switches:
  <name>:
    description: <string>         # Required
    flag: <string>                # Override flag string (default: --<name> with _ → -)
    short: <string>               # Single-char short form (e.g. -v)
```

The shell variable is set to `"true"` when the switch is present, `""` when
absent.

**Rules:**

- `short` must match `-[a-zA-Z]`.
- No validation fields (`pattern`, `allow`, `deny`) — switches have no value.

### options

An option is a named flag that takes exactly one value.

```yaml
options:
  <name>:
    description: <string>         # Required
    flag: <string>                # Override flag string (default: --<name> with _ → -)
    short: <string>               # Single-char short form (e.g. -r)
    required: <bool>              # Default: false
    pattern: <string>             # Regex the value must match
    allow: [<string>, ...]        # Exhaustive list of allowed values
    deny:  [<string>, ...]        # Values to reject
```

**Rules:**

- `allow` and `deny` are mutually exclusive.
- `short` must match `-[a-zA-Z]`.

### arguments

A positional argument identified by position in the invocation.

```yaml
arguments:
  <name>:
    description: <string>         # Required
    required: <bool>              # Default: false
    variadic: <bool>              # Collect remaining args into array (default: false)
    pattern: <string>             # Regex the value must match
    allow: [<string>, ...]        # Exhaustive list of allowed values
    deny:  [<string>, ...]        # Values to reject
```

**Rules:**

- `allow` and `deny` are mutually exclusive.
- At most one argument may be `variadic`, and it must be the **last** in the
  `arguments` dict.
- In `template` mode, a variadic argument's `{{param}}` must be the **last
  element** in `template.command`.
- Variadic arguments become bash arrays; all others become scalar variables.

---

## Lifecycle

### Execution order

```text
guards → pre → main (template exec | passthrough exec | script run)
```

### guards

Pre-flight boolean checks.

```yaml
guards:
  - fail_message: <string>       # Required. Shown on failure.
    command: [<string>, ...]      # Run as subprocess, output suppressed
    # OR
    script: <string>             # Inline bash snippet
```

Exactly one of `command` or `script`. The check passes on exit code 0.
`{{param}}` placeholders are substituted with parameter values. Guard
placeholders may only reference parameters defined in the tool's `switches`,
`options`, or `arguments`.

### pre

An inline bash script that runs **before** the main execution mode. Use it for
setup, variable computation, and conditional abort.

```yaml
pre: |
  <bash script body>
```

**Execution model:**

The pre script is wrapped in a shell function and called from the main shell:

```bash
_nerf_pre() {
  <pre script body>
}

_nerf_pre_rc=0
_nerf_pre || _nerf_pre_rc=$?
if [ $_nerf_pre_rc -ne 0 ]; then
  echo "error: pre-hook failed (exit code $_nerf_pre_rc)" >&2
  exit $_nerf_pre_rc
fi
```

**How to write pre scripts:**

- **Use `return` to abort, not `exit`.** Pre runs as a function in the main
  shell — `return 1` lets the wrapper report the failure. `exit` would kill the
  entire script immediately, bypassing error reporting.
- **Print your own error messages.** The fallback message
  (`"pre-hook failed (exit code N)"`) is generic. For a good user experience,
  write `echo "error: ..." >&2` before `return 1`.
- **Shell variables set in pre are visible to main.** Functions execute in the
  caller's scope. Both regular assignments (`FOO=bar`) and exports work — no
  `export` is required for variables that only need to be visible within the
  same script.
- **`{{param}}` placeholders work.** Parameters are parsed before pre runs, so
  placeholders are substituted the same as in guards.

**Example:**

```yaml
git-push-branch:
  description: Push the current branch to a remote
  template:
    command: [git, push, --follow-tags, "{{remote}}", HEAD]
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

---

## Environment

```yaml
env:
  VAR_NAME: "value"
```

Key-value pairs exported as environment variables before guards, pre, and main
execution. Values are static strings (no `{{param}}` substitution).

---

## Placeholder substitution

`{{param}}` placeholders in `template.command`, `guard.command`, `guard.script`,
and `pre` are replaced with shell variable references.

The substitution strategy depends on the parameter kind and context:

| Context | Required scalar | Optional scalar | Required variadic | Optional variadic | Switch |
|---|---|---|---|---|---|
| template command | `"$VAR"` | `${VAR:+"$VAR"}` | `"${VAR[@]}"` | `${VAR[@]+"${VAR[@]}"}` | `${VAR:+"--flag"}` |
| guard/pre script | `${VAR}` | `${VAR}` | `${VAR}` | `${VAR}` | `${VAR}` |

In script contexts (guards, pre, inline script body), the author is responsible
for quoting.

---

## Error messages

Generated scripts are used by AI agents as well as humans. Every runtime error
must be **structured, specific, and actionable** so that an agent can parse the
failure and recover without guessing.

### Format

All errors are written to stderr and follow a consistent structure:

```text
error: <tool-name>: <what went wrong>
  <detail line(s) — constraints, allowed values, etc.>
  hint: <what to do instead>
```

The first line is always `error: <tool-name>: ...` so the caller can
identify which tool failed.

### Error types and examples

**Missing required argument:**

```text
error: nerf-git-commit: missing required argument <message>
  usage: nerf-git-commit <message>
  hint: provide a Conventional Commits message (e.g. 'fix: correct typo')
```

**Missing required option:**

```text
error: nerf-nx-run: missing required option --project
  usage: nerf-nx-run --project <project> <target>
  hint: provide --project <name>
```

**Value does not match pattern:**

```text
error: nerf-git-commit: argument <message> does not match required pattern
  value:   'updated readme'
  pattern: ^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([a-zA-Z0-9._-]+\))?!?: .+
  hint: message must start with a type prefix (e.g. 'docs: update readme')
```

**Value not in allow list:**

```text
error: nerf-deploy-check: argument <environment> is not an allowed value
  value:   'dev'
  allowed: staging, production
  hint: use one of the allowed values
```

**Value in deny list:**

```text
error: nerf-git-tag: argument <tag> is not allowed
  value:  'latest'
  denied: latest, nightly
  hint: use a versioned tag name (e.g. 'v1.2.3')
```

**Denied token (passthrough mode):**

```text
error: nerf-safe-find: token '-exec' is not allowed (matched deny pattern '-exec')
  denied patterns: -exec, -execdir, -ok*, --delete
  hint: remove '-exec' and retry
```

**Guard failure:**

```text
error: nerf-git-commit: pre-flight check failed
  No staged changes. Use git-add to stage changes first.
```

**Unknown switch or option flag:**

```text
error: nerf-git-commit: unknown option '--amend'
  hint: run 'nerf-git-commit --help' for usage
```

---

## Generated documentation

Both the `--help` output of generated scripts and the AI skill files include
structured documentation derived from the manifest. This section specifies what
that documentation looks like.

### "Maps to" line

Every template and passthrough tool shows a **"Maps to"** line that reveals the
underlying command. This is critical for agents and humans to understand what
the nerf wrapper actually does.

**Template mode** — placeholders are shown as `<name>` tokens, static parts
shown verbatim:

```text
Maps to: git commit -m <message>
Maps to: git add <files...>
Maps to: git push --follow-tags <remote> HEAD
Maps to: <runner> cspell@8.19.4 <args...>
```

For `npm_pkgrun: true`, the runner is shown as `<runner>` since it resolves at
runtime (bunx, pnpx, or npx).

**Passthrough mode** — prefix tokens, then `"$@"` for the forwarded user
tokens, then suffix tokens:

```text
Maps to: find . "$@"
Maps to: kubectl "$@" --context=prod
Maps to: curl "$@"
```

The `"$@"` makes it visually obvious where the user's input goes and that
everything is forwarded. Prefix and suffix tokens are shown as literals.

**Script mode** — no "Maps to" line. Scripts are custom logic with no single
underlying command.

### Help text (--help)

Generated scripts include a `usage()` function printed on `--help` or argument
errors. Format:

```text
Usage: nerf-git-commit [--verbose|-v] --remote <remote> <message>

Switches:
  --verbose, -v       Enable verbose output

Options:
  --remote, -r <remote> (required)
      Remote name (e.g. origin)
      Must match: ^[a-z0-9_-]+$

Arguments:
  <message> (required)
      Commit message: type[(scope)][!]: description
      Must match: ^(feat|fix|docs|...)...

Maps to: git commit -m <message>

Create a git commit with a Conventional Commits message.
```

For passthrough mode:

```text
Usage: nerf-safe-find [tokens...]

Forwards all tokens to the underlying command.

Denied patterns: -exec, -execdir, -ok, -okdir, -delete

Maps to: find . "$@"

Run find without exec capabilities.
```

Key points:

- Switches, options, and arguments are in separate labeled sections.
- Constraints (pattern, allow, deny) are shown inline under each parameter.
- "Maps to" is always present for template and passthrough.
- The tool description is shown last.

### AI skill files (SKILL.md)

Generated skill files follow the same structure as help text but formatted as
markdown for AI assistants. Each tool gets a section:

```markdown
## nerf-git-commit

Create a git commit with a Conventional Commits message.

**Usage:** `<nerf-bin>/nerf-git-commit <message>`

**Maps to:** `git commit -m <message>`

**Arguments:**

- `<message>` (required): Commit message: type[(scope)][!]: description.
  Must match `^(feat|fix|docs|...)...`
```

For passthrough:

```markdown
## nerf-safe-find

Run find without exec capabilities.

**Usage:** `<nerf-bin>/nerf-safe-find [tokens...]`

**Maps to:** `find . "$@"`

**Denied patterns:** `-exec`, `-execdir`, `-ok`, `-okdir`, `-delete`
```

For script mode (no "Maps to"):

```markdown
## nerf-deploy-check

Validate deployment readiness.

**Usage:** `<nerf-bin>/nerf-deploy-check [--dry-run] <environment>`

**Switches:**

- `--dry-run`: Skip actual checks

**Arguments:**

- `<environment>` (required): Target environment. One of `staging`, `production`
```

---

## Complete example

```yaml
version: 1

package:
  name: git
  description: Git workflow tools for safe, scoped git operations
  skill_group: git
  skill_intro: |
    These tools wrap git operations with safety guardrails. Always stage changes
    with git-add before committing. Commit messages must follow the
    Conventional Commits specification.

tools:
  git-add:
    description: Stage files or directories for commit
    threat:
      read: workspace
      write: workspace
    template:
      command: [git, add, "{{files}}"]
    arguments:
      files:
        description: Files or directories to stage
        required: true
        variadic: true

  git-commit:
    description: >-
      Create a git commit with a Conventional Commits message.
      Format: type[(scope)][!]: description.
    threat:
      read: workspace
      write: workspace
    template:
      command: [git, commit, -m, "{{message}}"]
    arguments:
      message:
        description: "Commit message: type[(scope)][!]: description"
        required: true
        pattern: "^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\\([a-zA-Z0-9._-]+\\))?!?: .+"
    guards:
      - script: "! git diff --cached --quiet"
        fail_message: No staged changes. Use git-add to stage changes first.
      - script: |
          ! echo "{{message}}" | grep -qi 'co-authored-by:'
        fail_message: Commit message must not contain Co-Authored-By trailers.

  git-push-branch:
    description: Push the current branch to a remote (no force push)
    threat:
      read: workspace
      write: remote
    template:
      command: [git, push, --follow-tags, "{{remote}}", HEAD]
    arguments:
      remote:
        description: Remote name (e.g. origin)
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

  git-log:
    description: Show a short one-line log of recent commits
    threat:
      read: workspace
      write: none
    template:
      command: [git, log, --oneline, --no-decorate, -20]
```

**Passthrough example:**

```yaml
version: 1

package:
  name: find
  description: Safe file search without code execution
  skill_group: find

tools:
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

**Script example:**

```yaml
version: 1

package:
  name: deploy
  description: Deployment workflow tools
  skill_group: deploy

tools:
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

---

## Validation summary

| Rule | Scope |
|---|---|
| `version` is required and must be an integer | manifest |
| `threat.read` and `threat.write` required, each one of `none`, `workspace`, `machine`, `remote`, `admin` | tool |
| Exactly one of `template`, `passthrough`, `script` | tool |
| `switches`/`options`/`arguments` not allowed with `passthrough` | tool |
| `{{param}}` refs must exist in switches/options/arguments | template, guards, pre |
| All switches/options/arguments must be referenced in `{{param}}` | template only |
| Variadic argument must be last in `arguments` | arguments |
| Variadic `{{param}}` must be last element in `template.command` | template |
| `allow` and `deny` are mutually exclusive | options, arguments |
| Switch/option/argument names must not overlap | tool |
| `short` matches `-[a-zA-Z]` | switches, options |
| `pattern` is a valid regex | options, arguments |
| Guard has exactly one of `command` or `script` | guards |
| `deny` entries are valid glob patterns | passthrough |
