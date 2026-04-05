# Nerf Tools v1 Manifest -- Functional Requirements

## Problem Statement

The original nerf tools system works well for its original scope -- wrapping single commands with
explicit parameter validation. But three gaps have emerged:

1. **Limited execution models.** The original design only supports wrapping a single command with
   explicitly-defined parameters. There is no way to pass arguments through to an underlying tool
   with a deny-list (e.g. `find` without `-exec`), and no way to define a tool with custom
   multi-step logic.

2. **Flat CLI interface.** The current CLI has separate `build` and `skill` commands that each
   produce one output type. The `--install-nerfctl` flag is bolted onto `build`. As output targets
   grow (bin scripts, skills, claude plugin), the interface needs restructuring around `validate`
   and `generate` with pluggable targets.

3. **No versioning or risk metadata.** Manifests have no version field, so there is no way to evolve
   the spec while preserving backwards compatibility. Tools have no risk classification, so
   permissions must be granted per-tool rather than by category.

4. **Loose definition of args vs flags vs ...** The current manifest format does not clearly
   distinguish between positional arguments, named flags, and other types of parameters. This opens
   the door to ambiguity in how tokens are interpreted and potential security problems.

## Goals

- Support three execution modes: **template** (explicit params + command template), **passthrough**
  (deny-list + forward), and **script** (inline bash).
- Provide a clean CLI with `validate` and `generate` commands, where `generate` supports named
  targets.
- Version manifests so the spec can evolve without breaking existing tools.
- Attach a threat profile to each tool so operators can grant permissions by risk scope rather than
  enumerating every tool.

## Requirements

### R1: Parameter terminology

The new manifest replaces `flags` and `args` with three precise parameter types:

| New term     | Old equivalent               | What it is             |
| ------------ | ---------------------------- | ---------------------- |
| **switch**   | `flags` with `boolean: true` | Boolean flag, no value |
| **option**   | `flags` without `boolean`    | Named flag + value     |
| **argument** | `args`                       | Positional value       |

A **token** is a single raw element of `$@` — the unclassified unit before parsing. A **switch**
consumes one token. An **option** consumes two tokens: the **option flag** and the **option value**.
An **argument** consumes one token.

The invocation order is enforced: switches and options must come before arguments. The generated
parser stops consuming flags at the first non-flag token (or `--`).

```text
nerf-tool [switches] [options] [--] <arguments...>
```

In passthrough mode, tokens are never classified — they are scanned against deny rules and forwarded
as-is. **Prefix tokens** and **suffix tokens** are static tokens hardcoded before and after the
user's tokens.

### R2: Three execution modes

Each tool defines exactly one execution mode:

- **template** -- Build a command from an explicit template with `{{param}}` placeholders. An
  explicit allow list of parameters (switches, options, arguments) are parsed, validated, and
  substituted. The final argument may be variadic, allowing the template to accept an arbitrary
  number of trailing positional arguments from the user if appropriate for the underlying tool. This
  mode is generally preferred when the underlying command has a well-defined set of parameters that
  can be safely enumerated and substituted, as it allows for precise control over what the user can
  pass and reduces the risk of injection or misuse.

- **passthrough** -- Forward all tokens to an underlying command after scanning them against a deny
  list of glob patterns. Importantly, this mode operates entirely on a token basis. No attempt is
  made to classify tokens as switches, options, or arguments is made. Every token is evaluated
  against the deny list individually, and if it passes, it is forwarded to the underlying command
  exactly as provided by the user. Prefix and suffix tokens allow for static tokens to be prepended
  or appended to the user's token stream, enabling the tool to inject fixed arguments before or
  after the user-supplied input without classifying or modifying the user's tokens. This mode is
  provided for situations where an underlying tool has many parameters and only a few options or
  switches need to be restricted (e.g. `exec` and similar for `find`).

- **script** -- Run an inline bash script. Parameters are parsed and available as shell variables,
  but the script body is fully custom. As the most complex, this mode should only be used when the
  underlying command does not fit into the other modes.

While it might be possible to combine aspects of template and passthrough modes into a hybrid mode,
we keep them separate and simple to avoid footguns and other security issues. The goal is that each
of these modes should be easy to reason about within its particular model.

All modes support guards (pre-flight checks) and pre hooks (setup logic that runs before main
execution).

### R3: Pre hooks

Tools can define a `pre` script that runs before the main execution. Pre hooks are wrapped in a
shell function so that:

- `return 1` aborts with a fallback error message
- Shell variables set in pre are visible to the main execution
- `{{param}}` placeholders are substituted

This replaces some uses of guards where richer setup logic is needed. The execution order is:

```text
guards → pre → main (template exec | passthrough exec | script run)
```

### R4: CLI restructuring

The CLI is restructured around two top-level commands:

#### `nerf validate`

Parses and validates one or more manifests. Reports errors. Exits non-zero on any validation
failure. No output files are written.

```text
nerf validate [manifest...]
```

#### `nerf generate`

Generates output from manifests for one or more named targets. Each target produces a specific
output type in a specific directory.

```text
nerf generate --target <name> [--target <name> ...] [options] [manifest...]
```

Targets:

| Target          | What it produces                               | Current equivalent |
| --------------- | ---------------------------------------------- | ------------------ |
| `bin`           | Executable shell scripts, one per tool         | `nerf build`       |
| `skills`        | Rulesync SKILL.md files, one per package       | `nerf skill`       |
| `claude-plugin` | Claude Code plugin (skills, scripts, manifest) | `formats.py`       |

Each target has its own `--outdir` default. Common options (`--prefix`, `--no-default`,
`--keep-existing`) apply across targets.

The `--install-nerfctl` flag is removed from `build` and becomes part of the `claude-plugin`
target's behavior.

### R5: Manifest versioning

Every manifest declares a major version. As we have no need for support of the original design,
we're starting now with version 1.

```yaml
version: 1
```

The version is a single integer at the top level of the manifest file. It determines which fields
are valid and how the manifest is parsed.

**Rules:**

- The `version` field is required.
- The CLI validates against the declared version's schema.
- The current version is **1**. This is the first versioned manifest format. Prior unversioned
  manifests are not supported — they must be migrated.
- When the spec changes in backwards-incompatible ways, the version is bumped. Additive changes (new
  optional fields) do not require a version bump.

### R6: Threat model

Each tool declares a two-dimensional threat profile: what it **reads** and what it **writes**. This
enables operators to grant permissions by risk scope rather than enumerating individual tools.

```yaml
tools:
  git-log:
    threat:
      read: workspace
      write: none
    ...
  git-add:
    threat:
      read: workspace
      write: workspace
    ...
  git-push-branch:
    threat:
      read: workspace
      write: remote
    ...
```

#### Read vs write

The read axis captures what a tool can observe or consume: files in the workspace, the broader
filesystem, or remote resources. The write axis captures what a tool can modify or affect: nothing,
the workspace, the local machine, remote systems, or administrative-level operations. By separating
these two dimensions, we can reason about the risk of a tool more precisely than with a single
linear scale.

**Threat levels (ordered, narrow to broad):**

| Level       | Meaning                                                 |
| ----------- | ------------------------------------------------------- |
| `none`      | No access in this dimension                             |
| `workspace` | Confined to the current workspace directory tree        |
| `machine`   | Anywhere on the local filesystem                        |
| `remote`    | Network operations — APIs, remote repos, cloud services |
| `admin`     | Destructive, irreversible, or elevated operations       |

The two-axis model captures distinctions that a single axis cannot. For example, `git fetch` (read:
remote, write: workspace) and `git push` (read: workspace, write: remote) have very different risk
profiles but would both be "remote" on a single scale. Similarly, `find .` (read: workspace) and
`find /` (read: machine) are meaningfully different.

### R7: Threat-based grants

Operators can grant permissions by threat profile rather than enumerating individual tools. This is
implemented as a new grant command that works alongside the existing name/pattern-based commands.

**Grant-by-threat semantics:**

In addition to the existing pattern-based allow and deny grants, we define a way to manage grants by
threat profile.

```text
grant-by-threat --read <allowed_threat_level> --write <allowed_threat_level>
```

A tool is permitted when `tool.read <= grant.read AND tool.write <= grant.write`.

Per-tool grants remain available and can be applied on top of (after) threat-based grants. A tool
can be individually denied even if its threat levels are within the grant ceiling.

**Rules:**

- The `threat` field is required on every tool, with both `read` and `write`.
- Allowed threat levels must be one of: `none`, `workspace`, `machine`, `remote`, `admin`.
- The threat profile is metadata only -- it does not change what the tool does. Enforcement is at
  the permission layer (nerfctl, settings.json, etc.).

#### Embedded threat metadata

Generated scripts include structured threat metadata in their header comments:

```bash
#!/usr/bin/env bash
# nerf-git-push-branch -- Push the current branch to a remote
# Generated from git manifest. Do not edit directly.
# nerf:threat:read=workspace
# nerf:threat:write=remote
```

This metadata is the source of truth for threat profiles at runtime. It enables tool discovery and
classification without requiring manifests to be present.

The `# nerf:threat:` prefix is a structured tag. Discovery scans for executable files under a root
directory, greps for these tags, and parses the key=value pairs. This works with any directory
nesting (flat bin/, nested plugin skills/\*/scripts/, etc.).

#### Capabilities

The grant system must support two kinds of operations:

1. **Tool discovery** — given a root directory, find all nerf tools and their threat metadata by
   scanning for embedded `# nerf:threat:` tags in generated scripts. This must work across any
   directory nesting (flat bin/, nested plugin skills/\*/scripts/, etc.).

2. **Threat classification** — given a set of tools and a threat ceiling (read + write levels),
   classify each tool as inside or outside the box. A tool is "inside" when
   `tool.read <= ceiling.read AND tool.write <= ceiling.write`. Tools not matching a filter pattern
   are untouched.

The tool discovery and classification logic should be framework-agnostic and reusable. The
framework-specific part is only how results are applied (e.g. writing `Bash(<path>)` entries to
Claude Code's settings.json).

#### Grant commands

Five grant commands, each with one clear job. The first three are name/pattern based (unchanged from
current). The fourth is new and threat-based.

**`nerfctl-grant-allow`** `<plugin-root> <pattern> [--settings-scope user|local]`

Allow specific tools by name or glob pattern. Unchanged from current behavior.

**`nerfctl-grant-deny`** `<plugin-root> <pattern> [--settings-scope user|local]`

Deny specific tools by name or glob pattern. Unchanged from current behavior.

**`nerfctl-grant-reset`** `<plugin-root> <pattern> [--settings-scope user|local]`

Remove specific tools from both allow and deny. Unchanged from current behavior.

**`nerfctl-grant-by-threat`** `<plugin-root> --read <level> --write <level>`
`[--filter <glob>] [--outside deny|reset] [--settings-scope user|local]`

Allow all tools within the defined threat box, deny or reset everything outside.

```bash
# Allow tools with read ≤ workspace AND write ≤ workspace. Deny everything else.
nerfctl-grant-by-threat <plugin-root> --read workspace --write workspace

# Same, but only for git tools. Non-git tools untouched.
nerfctl-grant-by-threat <plugin-root> --read workspace --write workspace \
  --filter 'nerf-git-*'

# Allow up to remote read, workspace write. Reset (don't deny) outside tools.
nerfctl-grant-by-threat <plugin-root> --read remote --write workspace \
  --outside reset
```

Parameters:

- `--read <level>` (required): read ceiling.
- `--write <level>` (required): write ceiling.
- `--filter <glob>` (default: `*`): restricts which tools are affected. Tools not matching the
  filter are untouched.
- `--outside deny|reset` (default: `deny`): what to do with tools outside the threat box. `deny`
  adds them to the deny list. `reset` removes them from both allow and deny (back to
  ask-every-time).
- `--settings-scope user|local` (default: `user`): which settings file to modify.

**`nerfctl-grant-list`** `[--settings-scope user|local]`

List current permissions with threat metadata alongside each tool:

```text
user (~/.claude/settings.json):
  Allowed:
    nerf-git-log        read:workspace  write:none
    nerf-git-add        read:workspace  write:workspace
    nerf-git-commit     read:workspace  write:workspace
  Denied:
    nerf-git-push-branch  read:workspace  write:remote
```

If a tool's script is not found or has no embedded threat metadata, it is listed without
annotation.

#### Operational model

All grant commands write to the same flat allow/deny lists in settings.json. There is no layered
evaluation — **last write wins**. The operator controls ordering.

The intended workflow is a convention:

```bash
# 1. Set baseline with threat levels
nerfctl-grant-by-threat <root> --read workspace --write workspace

# 2. Override specific tools after the baseline
nerfctl-grant-allow <root> nerf-git-push-branch   # allow one that was denied
nerfctl-grant-deny <root> nerf-git-tag             # deny one that was allowed
```

Re-running step 1 overwrites the overrides from step 2. The operator knows this.

To provide visibility, `grant-by-threat` prints what it changes, including when it overrides an
existing entry:

```text
  Allowed: nerf-git-log        read:workspace  write:none
  Allowed: nerf-git-add        read:workspace  write:workspace
  Denied:  nerf-git-push-branch  read:workspace  write:remote  (was: allowed)
```

The `(was: allowed)` annotation gives the operator a chance to notice when a threat-based grant is
about to clobber an explicit per-tool grant. This is information, not enforcement — the write still
happens.

### R8: Agent-friendly error messages

All runtime errors (validation failures, denied tokens, guard failures) follow a structured format:

```text
error: <tool-name>: <what went wrong>
  <detail lines>
  hint: <what to do instead>
```

Errors must be specific enough for an AI agent to parse the failure, understand the constraint, and
retry correctly without human intervention.

## Future

### Scoped credential injection

Per-tool credential injection at provisioning time. This is unchanged from the original nerf tools
future roadmap and is orthogonal to the manifest changes.

### `deny_regex` for passthrough

Regex-based deny patterns for passthrough mode. The `deny_regex` field is reserved in the spec but
not implemented. Glob patterns (`deny`) cover current needs.

## Out of Scope

- **OS-level privilege enforcement** -- unchanged from original design.
- **Runtime manifest dependency** -- generated scripts remain self-contained.
- **Passthrough with wrapper parameters** -- passthrough mode does not support explicit
  switches/options/arguments. This avoids complexity in the scan-and-forward model.
