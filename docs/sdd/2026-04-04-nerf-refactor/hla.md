# Nerf Tools v1 Manifest -- High-Level Architecture

## Overview

This document describes the architectural changes needed to implement the v1 manifest specification.
The core idea is unchanged: a Python CLI reads YAML manifests and generates self-contained bash
scripts, rulesync skills, and Claude Code plugins. What changes is the manifest data model (three
execution modes, 2D threat metadata, refined parameter types), the CLI surface (`validate` +
`generate --target`), and a new threat-based grant command.

There is no new runtime dependency. Generated scripts remain self-contained. The manifest is still a
build-time artifact only.

## Components

```text
nerf (Python CLI)                   standalone build and skill generation tool
  nerf validate                     parse and validate manifests (no output)
  nerf generate --target <name>     generate output for one or more targets
    targets: bin, skills, claude-plugin

nerfctl-grant-* (shell scripts)     Claude Code permission management
  grant-allow                       allow by name/pattern (unchanged)
  grant-deny                        deny by name/pattern (unchanged)
  grant-reset                       reset by name/pattern (unchanged)
  grant-by-threat                   allow/deny by threat ceiling (new)
  grant-list                        list permissions with threat annotations (enhanced)

find-tools (shell function)         discover nerf tools + parse embedded threat metadata
classify-by-threat (shell function) bucket tools into inside/outside a threat box
```

## Data Model

The Python data model in `manifest.py` is restructured around the three execution modes and the new
parameter terminology.

### Current model (v0)

```text
NerfManifest
  PackageMeta
  tools: dict[str, ToolSpec]
    ToolSpec
      command: tuple[str, ...]
      flags: dict[str, FlagSpec]      # boolean and valued flags mixed
      args: dict[str, ArgSpec]        # positional args
      guards: list[GuardSpec]
      env: dict[str, str]
```

### New model (v1)

```text
NerfManifest
  version: int                         # required, must be 1
  PackageMeta                          # unchanged
  tools: dict[str, ToolSpec]
    ToolSpec
      description: str
      threat: ThreatSpec               # new: read + write scopes
        read: ThreatLevel              # enum: none|workspace|machine|remote|admin
        write: ThreatLevel

      # Execution mode (exactly one):
      template: TemplateSpec | None
        command: list[str]             # with {{param}} placeholders
        npm_pkgrun: bool
      passthrough: PassthroughSpec | None
        command: str
        deny: list[str]               # glob patterns
        prefix: list[str]
        suffix: list[str]
      script: str | None               # inline bash

      # Parameters (template + script only):
      switches: dict[str, SwitchSpec]  # new: replaces boolean flags
        description, flag, short
      options: dict[str, OptionSpec]   # new: replaces valued flags
        description, flag, short, required, pattern, allow, deny
      arguments: dict[str, ArgSpec]    # refined: no flag field
        description, required, variadic, pattern, allow, deny

      # Lifecycle:
      pre: str | None                  # new: pre-hook script
      guards: list[GuardSpec]          # unchanged structure
      env: dict[str, str]              # unchanged
```

### ThreatLevel enum

```python
class ThreatLevel(Enum):
    NONE = "none"
    WORKSPACE = "workspace"
    MACHINE = "machine"
    REMOTE = "remote"
    ADMIN = "admin"

    def __le__(self, other):
        order = [self.NONE, self.WORKSPACE, self.MACHINE, self.REMOTE, self.ADMIN]
        return order.index(self) <= order.index(other)
```

### Validation

Validation is split into two phases:

1. **Schema validation** -- structural checks that can be done per-field: required fields present,
   types correct, enum values valid, mutual exclusions (exactly one mode, allow vs deny).

2. **Cross-reference validation** -- checks that span multiple fields: `{{param}}` placeholders
   match defined parameters, variadic argument is last, all parameters are referenced in template
   mode, `switches`/`options`/`arguments` not present in passthrough mode.

Both phases run during `nerf validate` and before any `nerf generate` output. Errors include the
manifest file path, tool name, and field path for unambiguous diagnosis.

## Script Generation

### Builder restructuring

The current `builder.py` generates scripts for a single execution model (what is now called
"template"). The v1 builder supports three code paths:

```text
builder.py
  build_script(tool: ToolSpec) -> str
    _emit_header(tool)              # shebang, description, threat metadata
    _emit_env(tool)                 # export env vars
    _emit_usage(tool)               # usage() function
    _emit_parser(tool)              # argument parsing (template + script only)
    _emit_guards(tool)              # guard checks
    _emit_pre(tool)                 # pre-hook function + call
    _emit_template(tool)            # exec with substituted command
    _emit_passthrough(tool)         # deny scan + exec with prefix/suffix
    _emit_script(tool)              # inline script body
```

Only one of `_emit_template`, `_emit_passthrough`, or `_emit_script` is called per tool.

### Script structure by mode

**Template mode:**

```bash
#!/usr/bin/env bash
# nerf-<tool> -- <description>
# Generated from <package> manifest. Do not edit directly.
# nerf:threat:read=<scope>
# nerf:threat:write=<scope>
set -euo pipefail

# env exports
# usage() function
# argument parsing (switches, options, positional args)
# validation (required, pattern, allow, deny)
# guards
# _nerf_pre() function + call
exec <command with substituted params>
```

**Passthrough mode:**

```bash
#!/usr/bin/env bash
# nerf-<tool> -- <description>
# Generated from <package> manifest. Do not edit directly.
# nerf:threat:read=<scope>
# nerf:threat:write=<scope>
set -euo pipefail

# env exports
# usage() function (lists denied patterns)
# guards
# _nerf_pre() function + call
# deny scan loop
exec <command> <prefix...> "$@" <suffix...>
```

**Script mode:**

```bash
#!/usr/bin/env bash
# nerf-<tool> -- <description>
# Generated from <package> manifest. Do not edit directly.
# nerf:threat:read=<scope>
# nerf:threat:write=<scope>
set -euo pipefail

# env exports
# usage() function
# argument parsing (switches, options, positional args)
# validation (required, pattern, allow, deny)
# guards
# _nerf_pre() function + call
<inline script body>
```

### Threat metadata in headers

Every generated script includes structured threat metadata as comments:

```bash
# nerf:threat:read=workspace
# nerf:threat:write=remote
```

These are the source of truth for runtime discovery. The grant system parses these tags -- it does
not need access to the manifest.

### Pre-hook generation

The pre script is wrapped in a function to allow `return` for abort:

```bash
_nerf_pre() {
  <pre script body with {{param}} substituted>
}

_nerf_pre_rc=0
_nerf_pre || _nerf_pre_rc=$?
if [ $_nerf_pre_rc -ne 0 ]; then
  echo "error: <tool-name>: pre-hook failed (exit code $_nerf_pre_rc)" >&2
  exit $_nerf_pre_rc
fi
```

Shell variables set in `_nerf_pre` are visible to the main execution because the function runs in
the caller's scope.

### Placeholder substitution

The substitution table from the manifest spec is implemented in the builder:

| Context | Required scalar | Optional scalar | Required variadic | Optional variadic | Switch |
|---|---|---|---|---|---|
| template command | `"$VAR"` | `${VAR:+"$VAR"}` | `"${VAR[@]}"` | `${VAR[@]+"${VAR[@]}"}` | `${VAR:+"--flag"}` |
| guard/pre script | `${VAR}` | `${VAR}` | `${VAR}` | `${VAR}` | `${VAR}` |

The builder selects the substitution strategy based on parameter kind and context.

### Error message generation

All generated error messages follow the structured format:

```text
error: <tool-name>: <what went wrong>
  <detail lines>
  hint: <what to do instead>
```

The builder embeds the tool name, constraint details, and hint text into each validation check.
Error messages are static strings in the generated script -- no runtime lookup.

## CLI Restructuring

### Current CLI

```text
nerf build [--outdir] [--no-default] [--keep-existing] [--install-nerfctl] [--prefix] [manifests...]
nerf skill [--outdir] [--no-default] [--keep-existing] [--prefix] [manifests...]
```

### New CLI

```text
nerf validate [manifests...]
nerf generate --target <name> [--target <name> ...] [options] [manifests...]
```

#### `nerf validate`

Parses and validates manifests. Reports all errors (not just the first). Exits non-zero on any
failure. No output files. Useful in CI and pre-commit hooks.

#### `nerf generate`

Generates output for one or more named targets. Each target type has its own output logic and
default output directory.

| Target | Output | Default outdir |
|---|---|---|
| `bin` | Executable shell scripts | `./bin/` |
| `skills` | Rulesync SKILL.md files | `./skills/` |
| `claude-plugin` | Plugin manifest + skills + scripts | `./claude-plugin/` |

Common options:

- `--outdir <dir>`: override default output directory
- `--no-default`: skip built-in manifest catalog
- `--keep-existing`: preserve files not regenerated by this run
- `--prefix <string>`: tool name prefix (default: `nerf-`)
- `[manifests...]`: additional manifest files merged after catalog

The `--install-nerfctl` flag is removed. The `claude-plugin` target includes nerfctl scripts and
skills as part of its standard output.

### CLI module changes

`cli.py` changes from two Typer commands to two new ones. The internal implementation routes through
the same manifest loading and merging pipeline, then dispatches to the appropriate generator based on
the target name.

```text
cli.py
  validate_command(manifests)
    load + merge + validate (report errors, exit code)
  generate_command(targets, manifests, options)
    load + merge + validate
    for target in targets:
      match target:
        "bin"           -> builder.build_scripts(...)
        "skills"        -> skill.build_skills(...)
        "claude-plugin" -> formats.build_claude_plugin(...)
```

## Skill and Plugin Generation

### Skill changes

`skill.py` adds support for:

- Three execution modes in documentation (template shows "Maps to" with placeholders, passthrough
  shows "Maps to" with `"$@"`, script has no "Maps to")
- Separate parameter sections: Switches, Options, Arguments (instead of mixed Flags/Arguments)
- Threat metadata display (optional, for operator reference)

### Plugin changes

`formats.py` changes to match the new skill format and adds threat metadata to generated script
headers. The plugin structure is otherwise unchanged:

```text
output_dir/
  .claude-plugin/
    plugin.json
    marketplace.json
  skills/
    nerftools/SKILL.md            (overview)
    <prefix><group>/
      SKILL.md                    (skill with tool docs)
      scripts/<tool>              (executable scripts)
    nerfctl-grant-allow/          (unchanged)
    nerfctl-grant-deny/           (unchanged)
    nerfctl-grant-reset/          (unchanged)
    nerfctl-grant-by-threat/      (new)
    nerfctl-grant-list/           (enhanced)
```

## Grant System

### Existing grant scripts (unchanged)

`grant-allow`, `grant-deny`, and `grant-reset` continue to work as-is. They operate on tool names
and glob patterns, manipulating `Bash(<path>)` entries in `settings.json`. No changes needed for the
v1 manifest -- these scripts don't care about the manifest format. They only care about the file
paths of generated scripts.

### New: find-tools and classify-by-threat

Two reusable shell functions that form the core of threat-based grant operations. They are
framework-agnostic -- they work with any directory of nerf scripts.

#### `find-tools`

Given a root directory, discover all nerf tools and their embedded threat metadata.

```text
Input:  root directory, optional name filter glob
Output: list of (script_path, tool_name, read_level, write_level)
```

Implementation:

1. Find executable files under the root (supports flat `bin/` and nested `skills/*/scripts/`)
2. Grep each file for `# nerf:threat:read=` and `# nerf:threat:write=` comment lines
3. Parse the key=value pairs
4. Apply name filter if provided
5. Output structured results (one line per tool, tab-separated)

#### `classify-by-threat`

Given a list of tools with threat metadata and a ceiling, classify each as inside or outside.

```text
Input:  tool list (from find-tools), read ceiling, write ceiling
Output: list of (script_path, tool_name, classification: inside|outside)
```

A tool is "inside" when `tool.read <= ceiling.read AND tool.write <= ceiling.write`.

### New: grant-by-threat

A new nerfctl grant command that uses `find-tools` and `classify-by-threat` to manage permissions
by threat profile.

```text
nerfctl-grant-by-threat <plugin-root> --read <level> --write <level>
  [--filter <glob>] [--outside deny|reset] [--settings-scope user|local]
```

Implementation:

1. Run `find-tools` on the plugin root (with `--filter` if provided)
2. Run `classify-by-threat` with the specified ceiling
3. For "inside" tools: add to allow, remove from deny
4. For "outside" tools: add to deny (default) or remove from both (if `--outside reset`)
5. Print changes with `(was: allowed)` / `(was: denied)` annotations when overriding

The `find-tools` and `classify-by-threat` logic can be embedded as functions within the
`grant-by-threat` script, or sourced from a shared file. This is an implementation decision -- the
important thing is that the logic is well-separated and testable. The current nerfctl scripts are
self-contained (no sourcing), and we can keep that pattern if the shared functions are small enough
to inline.

### Enhanced: grant-list

The existing `grant-list` is enhanced to show threat metadata alongside each permission entry:

```text
user (~/.claude/settings.json):
  Allowed:
    nerf-git-log        read:workspace  write:none
    nerf-git-add        read:workspace  write:workspace
  Denied:
    nerf-git-push-branch  read:workspace  write:remote
```

This uses `find-tools` to look up threat metadata for each listed tool. Tools without embedded
metadata (e.g. removed or non-nerf entries) are listed without annotation.

## Manifest Migration

There is no automated migration from unversioned (v0) manifests to v1. The v0 format is not
supported by the v1 CLI. The built-in manifest catalog is rewritten in v1 format as part of the
implementation.

### Migration mapping

| v0 field | v1 equivalent |
|---|---|
| `command` | `template.command` |
| `flags` with `boolean: true` | `switches` |
| `flags` without `boolean` | `options` |
| `args` | `arguments` |
| `guards` | `guards` (unchanged structure) |
| `env` | `env` (unchanged) |
| (none) | `threat` (new, must be added to every tool) |
| (none) | `version: 1` (new, required) |
| (none) | `pre` (new, optional) |

Tools that currently use `command` with flags and args become `template` mode tools. No existing
tools use passthrough or script mode -- those are new capabilities.

## Directory Layout

No changes to the installed layout on VMs:

```text
/opt/agentworks/nerf/
  bin/                  generated executables (on agent PATH)
  skills/               generated skills (rulesync source)
```

The source layout adds the SDD documents:

```text
nerftools/
  nerftools/            Python package
    __init__.py
    cli.py              restructured: validate + generate
    manifest.py         new data model (v1)
    builder.py          three code generation paths
    skill.py            updated doc format
    formats.py          updated plugin builder
    nerfctl/
      claude/
        grant-allow.sh      (unchanged)
        grant-deny.sh       (unchanged)
        grant-reset.sh      (unchanged)
        grant-by-threat.sh  (new)
        grant-list.sh       (enhanced)
  manifests/            rewritten in v1 format
    git/manifest.yaml
    ...
  tests/                updated for v1
```

## Design Decisions

### No v0 compatibility layer

The v0 manifest format has no `version` field and a different data model. Rather than building a
compatibility shim, we require migration. The built-in manifests are rewritten and there are no
known external consumers. The cost of a shim (complexity, testing surface) exceeds the migration
cost.

### Inlined find-tools and classify-by-threat

The `find-tools` and `classify-by-threat` logic is inlined into the grant scripts rather than
sourced from a shared library. This preserves the self-contained property of nerfctl scripts and
avoids introducing a sourcing dependency. The logic is small enough (grep + parse + compare) that
duplication is manageable. If the logic grows substantially, it can be extracted into a sourced
helper later.

### Passthrough deny via bash case/glob

Passthrough deny matching uses bash `case` statements with glob patterns rather than regex. This is
intentional: `case` glob matching is built into bash (no external dependency), supports the common
patterns needed (exact match, prefix, suffix, wildcards), and is easy to audit. Regex support
(`deny_regex`) is reserved for future use.

### Pre-hook as function, not subshell

The pre script runs as a function in the main shell scope, not in a subshell. This means:

- `return` works for abort (vs `exit` which would kill the whole script)
- Variables set in pre are visible to the main execution
- The pre script has access to parsed parameters

The trade-off is that a buggy pre script could corrupt the main shell state. This is acceptable
because the pre script is authored by the manifest writer (an operator), not by the agent.

### Last-write-wins for grants

All grant commands write to the same flat allow/deny lists. There is no precedence system and no
layered evaluation. This matches Claude Code's actual settings.json model and avoids building an
abstraction that the underlying system doesn't support. Operators are expected to run grant commands
in a deliberate order.
