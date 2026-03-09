# Nerfed Commands -- High-Level Architecture

**Builds on:** [User-Based Security Model](../2026-03-08-user-based-security/)

## Overview

Nerfed commands add a controlled privilege layer on top of the user-based security model. Where that
model isolates agents from each other and from the user's account, nerfed commands give agents a
narrow, auditable, time-boxed path to perform specific privileged operations.

The core mechanism is `nerfrun` -- a single compiled SUID binary owned by the user account
(`agentworks`). Tools are defined declaratively in manifest files. When an agent runs a nerfed tool,
`nerfrun` checks RBAC, then either executes a command template from the manifest (for wrapper tools)
or delegates to a custom script (for more complex tools). No daemon, no socket, no IPC -- just the
filesystem and standard Unix SUID semantics.

Because `nerfrun` is SUID to the user account, it inherits the user's existing credentials
(`az login`, `gh auth login`, SSH keys, etc.) naturally. There is no separate service user or
credential brokering -- the user authenticates once as they normally would, and agents access those
credentials through the SUID gate.

## Security Layer Diagram

```text
+------------------------------------------------------------------+
|                        VM (trust boundary)                        |
|                                                                   |
|  user account (agentworks)                                        |
|    - sudo, full control                                           |
|    - owns nerfrun and nerf config                                 |
|    - holds all credentials (az, gh, ssh, etc.)                    |
|    - manages agent lifecycle                                      |
|                                                                   |
|  ws1--coder, ws1--reviewer, ...                                   |
|    - no sudo, no login shell (except via coding tool)             |
|    - workspace access via group                                   |
|    - tools access via aw-tools group (read/execute)               |
|    - nerfed operations via nerfrun symlinks only                  |
+------------------------------------------------------------------+
```

## User and Group Topology (additions)

Building on the user-based security model topology:

```text
Groups (new):
  nerf-exec           (execute access to nerfrun and its symlinks)
```

No new users are added. The user account (`agentworks`) owns all nerf infrastructure. The dedicated
`nerf` user from the earlier design has been eliminated -- see
[Design Decision: No Dedicated Nerf User](#design-decision-no-dedicated-nerf-user) below.

### Nerf-exec group

- Agent users are added to this group during agent provisioning
- Grants execute (but not read) access to `nerfrun` and its symlinks
- This prevents agents from reading the binary contents while still allowing SUID execution

## Directory Layout

```text
/opt/agentworks/nerf/
  bin/                                # 0710 agentworks:nerf-exec
    nerfrun                           # 4710 agentworks:nerf-exec (SUID, the only compiled binary)
    nerf-git-push-origin -> nerfrun   # symlink
    nerf-git-push-non-origin -> nerfrun
    nerf-az-account-show -> nerfrun
    nerf-wcid -> nerfrun
    ...
  etc/                                # 0700 agentworks:agentworks
    bigred.lock                       # if present, all tools deny (bigred)
    rbac.toml                         # RBAC rules
  packages/                           # 0700 agentworks:agentworks
    nerf-git/                         # a nerf package
      manifest.toml                   # tool definitions, descriptions, skills metadata
      skills/
        SKILL.md                      # rulesync skill for this package
    nerf-az/
      manifest.toml
      scripts/
        nerf-az-resource-list.sh      # custom script for a complex tool
      skills/
        SKILL.md
    nerf-wcid/
      manifest.toml
      skills/
        SKILL.md
```

### Permission rationale

- `bin/` is `0710 agentworks:nerf-exec`: the group can enter and execute files, but cannot list the
  directory. Agents can run a tool by name but cannot enumerate all available tools (they use
  `nerf-wcid` for that).
- `nerfrun` is `4710 agentworks:nerf-exec`: the SUID bit (4) causes execution as the user account.
  Owner can read/write/execute, group can execute only, others have no access. All tool-name
  symlinks point to this single binary.
- `etc/` is `0700 agentworks:agentworks`: only the user account can read. Since `nerfrun` executes
  as the user account, it can read RBAC rules. The calling agent user cannot.
- `packages/` is `0700 agentworks:agentworks`: only the user account (and therefore `nerfrun`) can
  read manifests and custom scripts. Agent users access package content indirectly through
  `nerfrun`.
- The user manages `etc/` and `packages/` contents directly -- no sudo needed since the user
  account owns them.

## Nerf Packages and Manifests

### Package concept

A nerf package is a directory that bundles everything needed for a family of related nerfed tools:
tool definitions, descriptions, optional custom scripts, and rulesync skills. Installing a package
means copying it into `/opt/agentworks/nerf/packages/` and creating symlinks in `bin/` for each
tool it defines.

### Manifest format

Each package contains a `manifest.toml` that declaratively defines its tools.

#### Wrapper tools

Most tools are simple wrappers around an existing command. The manifest defines the command template
and parameter validation. No code is needed -- `nerfrun` executes the command directly.

```toml
[package]
name = "nerf-git"
skill_group = "nerf-git"

[tools.nerf-git-push-origin]
description = "Push the current branch to the origin remote"
command = ["git", "push", "origin", "HEAD"]
run_as_user = true

[tools.nerf-git-push-non-origin]
description = "Push the current branch to a non-origin remote"
command = ["git", "push", "{remote}", "HEAD"]
run_as_user = true

[tools.nerf-git-push-non-origin.params.remote]
required = true
description = "Remote name to push to"
deny = ["origin"]
pattern = "^[a-z0-9_-]+$"
```

#### Custom tools

When the logic is more complex than "run this command with these args" (e.g. a tool that needs to
parse API responses, combine multiple calls, or format output), a package includes a custom script
and the manifest points to it.

```toml
[tools.nerf-az-resource-list]
description = "List Azure resources in the configured subscription"
script = "scripts/nerf-az-resource-list.sh"
run_as_user = true
env = { AZURE_DEFAULTS_GROUP = "my-resource-group" }
```

Custom scripts can be written in any language (bash, Python, etc.). They do not need to be compiled
because `nerfrun` is the SUID entry point -- by the time it execs the script, the effective UID is
already set. The scripts themselves are not SUID and are not directly executable by agents (they live
in `packages/` which is `0700`).

#### The `run_as_user` flag

Each tool declares whether it should run as the user account or drop privileges back to the calling
agent user before executing.

- `run_as_user = true`: the command/script runs as the user account. Required for tools that need
  the user's credentials (`az`, `gh`, git push, etc.). This is the common case.
- `run_as_user = false`: `nerfrun` drops the effective UID back to the real UID (the agent user)
  before executing. Useful for tools that only need RBAC gating but should not run with the user's
  full privileges (e.g. a tool that formats workspace output or runs a linter).

#### Parameter validation

Parameters in command templates use `{name}` substitution. Each parameter has a validation spec:

- **`required`**: whether the parameter must be provided
- **`description`**: human-readable description (used by `nerf-wcid`)
- **`pattern`**: regex the value must match (e.g. `^[a-z0-9_-]+$`)
- **`allow`**: explicit allow-list of valid values
- **`deny`**: explicit deny-list of forbidden values (e.g. `["origin"]`)
- **`default`**: default value if not provided (only valid when `required = false`)

Parameters are always passed as discrete exec arguments -- never shell-interpolated. `nerfrun`
validates all parameters before constructing the exec argument array. If validation fails, the tool
exits with a constructive error message.

#### Environment variables

Tools can declare environment variables via the `env` map. These are set before executing the
command or script. This is useful for tools that need specific configuration (e.g.
`AZURE_CONFIG_DIR`, `AZURE_DEFAULTS_GROUP`).

### Built-in tools

`nerf-wcid` is a built-in tool implemented directly in the `nerfrun` binary rather than via a
manifest wrapper. It reads all installed manifests and cross-references with RBAC to produce the
discovery output. It still has a manifest entry (for description and skill metadata) but does not
use the `command` or `script` fields.

### Package installation

Installing a nerf package:

1. Copy the package directory to `/opt/agentworks/nerf/packages/<name>/`
2. For each tool defined in the manifest, create a symlink in `bin/`:
   `ln -s nerfrun /opt/agentworks/nerf/bin/<tool-name>`
3. Validate the manifest (required fields, parameter specs, script paths)

The Agentworks CLI provides `agentworks nerf install <package-path>` to automate this.

## RBAC Configuration

### Format

```toml
# /opt/agentworks/nerf/etc/rbac.toml

[[rules]]
user = "myproject--coder"
tool = "nerf-git-push-origin"

[[rules]]
user = "myproject--coder"
tool = "nerf-git-push-non-origin"
expires = "2026-03-09T00:00:00Z"

[[rules]]
user = "myproject--reviewer"
tool = "nerf-wcid"

[[rules]]
user = "myproject--reviewer"
tool = "nerf-az-account-show"
```

### Evaluation

When `nerfrun` is invoked (via a tool-name symlink):

1. Reads `argv[0]` to determine the tool name
2. Checks for `bigred.lock` -- if present, deny unconditionally
3. Reads the real UID of the calling process and resolves to a username
4. Reads `rbac.toml` and searches for a matching rule (user + tool name)
5. If a matching rule has an `expires` field, checks against current UTC time
6. If no valid rule is found: log denial, exit non-zero
7. If a valid rule is found: load the manifest, validate parameters, execute

### No globs

RBAC rules use exact matches for both user and tool. No wildcards, no patterns, no regex. This keeps
the security model simple and auditable. The operator explicitly grants each agent access to each
tool.

## SUID Execution Flow

```text
Agent (ws1--coder) runs: nerf-git-push-origin

1. Kernel executes nerfrun (via symlink) with effective UID = agentworks
   Real UID remains ws1--coder
2. nerfrun reads argv[0] -> "nerf-git-push-origin"
3. nerfrun checks for /opt/agentworks/nerf/etc/bigred.lock
   - If present: log BIGRED, print constructive message, exit 1
4. nerfrun reads real UID -> "ws1--coder"
5. nerfrun reads /opt/agentworks/nerf/etc/rbac.toml
6. Checks: does ws1--coder have access to nerf-git-push-origin?
   - If no: log DENIED, print constructive message, exit 1
   - If expired: log EXPIRED, print constructive message, exit 1
7. nerfrun loads manifest for nerf-git package
8. Resolves command template: ["git", "push", "origin", "HEAD"]
9. Checks run_as_user flag:
   - true: keep effective UID as agentworks
   - false: drop effective UID to ws1--coder
10. exec() the resolved command (no shell, direct exec)
11. Log "OK ws1--coder nerf-git-push-origin", exit with command result
```

## Discovery (nerf-wcid)

`nerf-wcid` is a built-in `nerfrun` command (not a manifest wrapper). Every agent user should have
RBAC access to it. When run:

1. Reads real UID, resolves to username
2. Checks RBAC for access to `nerf-wcid` itself
3. Reads all installed manifests to get tool names and descriptions
4. Cross-references with RBAC rules for the calling user
5. Outputs authorized tools with descriptions and expiration info

Example output:

```text
nerf-git-push-origin         Push the current branch to the origin remote
nerf-git-push-non-origin     Push the current branch to a non-origin remote
                             expires: 2026-03-09T00:00:00Z
nerf-az-account-show         Show the current Azure account
nerf-wcid                    List available nerfed tools
```

## Credential Inheritance

### Pattern

`nerfrun` executes as the user account via SUID, so tools with `run_as_user = true` inherit the
user's existing credentials directly. There is no credential brokering, no separate credential
store, and no service principal management. The user authenticates once (as they already do for
interactive work), and nerfed tools piggyback on that authentication.

### Azure

The user's `az login` session is stored in `~/.azure/` in the user account's home directory. Since
`nerfrun` executes as the user account, `az` CLI calls within a nerf tool see this session
automatically.

On Azure VMs, managed identity is also available and works without any login step.

### GitHub

The user's `gh auth login` session is stored in `~/.config/gh/` in the user account's home
directory. `nerf-gh-*` tools see this session automatically.

### SSH (git operations)

Git push/pull operations use the existing SSH agent socket from the user-based security model. The
calling agent user is already in the `agentworks-ssh` group and has `SSH_AUTH_SOCK` set. No
additional credential handling is needed for git-over-SSH -- the nerfed tool's role is purely RBAC
enforcement.

## Audit Logging

All `nerfrun` invocations are logged to syslog (or a dedicated log file at
`/var/log/agentworks/nerf.log`). Each entry includes:

- Timestamp (UTC)
- Calling user (real UID resolved to username)
- Tool name
- Outcome: `OK`, `DENIED`, `EXPIRED`, `BIGRED`, or `ERROR`
- For errors: a brief description

Example:

```text
2026-03-08T14:32:01Z OK ws1--coder nerf-git-push-origin
2026-03-08T14:32:15Z DENIED ws1--reviewer nerf-git-push-origin
2026-03-08T14:33:00Z EXPIRED ws1--coder nerf-az-account-show
2026-03-08T15:01:00Z BIGRED ws1--coder nerf-git-push-origin
```

## nerfrun Implementation

### The only compiled binary

`nerfrun` is a single compiled, statically linked binary. It is the only artifact that requires
compilation in the entire nerfed commands system. Everything else -- tool definitions, custom
scripts, skills, RBAC rules -- is declarative content.

SUID scripts are disabled by most Linux kernels (the kernel ignores SUID on interpreted files), so
the entry point must be compiled. Reasonable language options:

- **Go**: good stdlib, static linking by default, minimal attack surface
- **Rust**: same benefits, stricter memory safety

Go is the likely default for simplicity.

### Multicall binary pattern

`nerfrun` uses the multicall binary pattern (like busybox). Each nerfed tool is a symlink to
`nerfrun`. When invoked, `nerfrun` reads `argv[0]` to determine which tool was requested, then
loads the corresponding manifest entry. When invoked as `nerfrun` directly (no symlink), it prints
usage information.

### Core logic

The `nerfrun` binary contains:

- Bigred lockfile check
- Real UID resolution
- RBAC evaluation (parse `rbac.toml`, match user + tool, check expiration)
- Manifest loading and validation
- Parameter parsing and validation
- Privilege management (`run_as_user` flag handling)
- Audit logging
- `nerf-wcid` implementation (built-in)

### Constructive denial messages

When `nerfrun` denies an operation, it prints a constructive message to stderr that tells the caller
what happened and what to do about it. This is critical because the caller is often an AI agent that
will read the output and can act on clear instructions.

Example stderr output for each denial type:

```text
DENIED: ws1--coder is not authorized to run nerf-git-push-origin.
Ask your operator to grant access. They can run:
  agentworks nerf grant ws1--coder nerf-git-push-origin
```

```text
EXPIRED: ws1--coder had access to nerf-git-push-origin but it
expired at 2026-03-09T00:00:00Z.
Ask your operator to renew access. They can run:
  agentworks nerf grant ws1--coder nerf-git-push-origin
```

```text
BIGRED: All privileged operations are suspended on this VM.
Your operator has activated the emergency shutdown. No nerfed
tools will work until they resume normal operations.
```

The message always includes:

- The specific denial reason (not authorized, expired, or bigred)
- The calling user and tool name
- An actionable next step (who to ask, what command to run)

This ensures that AI agents can relay the denial to their human operator with enough context to
resolve it, rather than just reporting "permission denied."

### Security hardening

- **No shell expansion**: `nerfrun` never passes arguments through a shell. All command execution
  uses exec directly with explicit argument arrays constructed from the manifest template and
  validated parameters.
- **Parameter validation**: all parameters are validated against their manifest spec (pattern, allow,
  deny) before substitution into the command template. Invalid parameters are rejected with a
  constructive error message.
- **Privilege management**: `nerfrun` respects the `run_as_user` flag. When `false`, it drops the
  effective UID back to the real UID before executing. When `true`, the command runs as the user
  account.
- **Manifest integrity**: manifests and custom scripts live in `packages/` which is `0700` to the
  user account. Agents cannot modify them. `nerfrun` reads them as the user account via SUID.
- **Minimal attack surface**: one compiled binary, statically linked, no dynamic loading. Custom
  scripts are exec'd (not sourced or interpreted by `nerfrun`).

## Design Decision: No Dedicated Nerf User

An earlier design introduced a dedicated `nerf` Linux user as the SUID identity for nerfed tools.
That user would hold brokered credentials (service principal files, PATs, etc.) and the SUID
binaries would run as `nerf` to access them.

This design eliminates the `nerf` user entirely. The rationale:

- **Credential duplication is the core problem.** The user account already holds all credentials
  (`az login`, `gh auth login`, SSH keys). A separate nerf user requires duplicating or forwarding
  those credentials, adding operational complexity (service principal management, token rotation,
  separate auth flows) for no functional benefit.
- **The user account IS the user's identity.** The `agentworks` account is not a special admin
  service account -- it is the human operator's identity on the VM. Treating it as the SUID target
  is natural: "when an agent runs a nerfed tool, it acts as the user, gated by RBAC."
- **The security trade-off is minimal.** If an agent exploited a bug in `nerfrun`, they would land
  as the user account (which has sudo) rather than as an unprivileged nerf user. But `nerfrun` is a
  single compiled, statically linked binary. The RBAC and bigred checks execute before any real
  work. The attack surface is tiny, and the operational simplification far outweighs the theoretical
  privilege escalation risk.

## Impact on User-Based Security Model

The nerfed commands layer adds to the existing topology:

### VM initialization

- Create the `nerf-exec` group
- Create `/opt/agentworks/nerf/bin/`, `/opt/agentworks/nerf/etc/`,
  `/opt/agentworks/nerf/packages/`
- Set permissions as described above
- Install `nerfrun` binary and nerf packages

### Agent provisioning

- Add new agents to the `nerf-exec` group (in addition to existing groups)
- Add default RBAC rules (at minimum, `nerf-wcid` access)

### Agent deletion

- Remove RBAC rules for the deleted agent user

### Permissions summary (additions)

| Path          | Owner      | Group      | Mode | Effect                                |
| ------------- | ---------- | ---------- | ---- | ------------------------------------- |
| Nerf bin dir  | agentworks | nerf-exec  | 0710 | Agents execute, cannot list           |
| nerfrun       | agentworks | nerf-exec  | 4710 | SUID to user account, agents execute  |
| Tool symlinks | agentworks | nerf-exec  | -    | Point to nerfrun                      |
| Nerf config   | agentworks | agentworks | 0700 | User account only (nerfrun reads too) |
| Packages      | agentworks | agentworks | 0700 | User account only (nerfrun reads too) |

## Rulesync Skills

### Overview

Nerfed tools ship with rulesync skills as part of their nerf packages. A single skill covers a
family of related tools (e.g. the `nerf-git` package skill covers `nerf-git-push-origin`,
`nerf-git-push-non-origin`, etc.). This keeps the number of skills manageable and provides cohesive
documentation for related operations.

### Skill packaging

Skills live inside their nerf package directory:

```text
/opt/agentworks/nerf/packages/
  nerf-git/
    manifest.toml
    skills/
      SKILL.md                        # covers all nerf-git-* tools
  nerf-az/
    manifest.toml
    skills/
      SKILL.md                        # covers all nerf-az-* tools
  nerf-wcid/
    manifest.toml
    skills/
      SKILL.md                        # discovery tool
```

Skills are not directly readable by agent users (packages are `0700`). Rulesync reads them via the
user account during workspace provisioning and copies the content into the workspace's rulesync
output.

### Automatic workspace configuration

When Agentworks provisions a workspace that uses rulesync, it configures the workspace's rulesync
setup to import the relevant skills based on the agent's authorized nerfed tools. The flow:

1. Operator grants RBAC access to nerfed tools for an agent
2. Agentworks reads the agent's RBAC rules to determine authorized tools
3. Agentworks maps authorized tools to their packages (via the manifest `skill_group` or package
   name)
4. Agentworks copies the matching skills from `packages/<name>/skills/` into the workspace's
   rulesync configuration
5. When rulesync generates output (e.g. `.claude/`, `.cursor/`), the skills are included
   automatically
6. The AI coding tool picks up the skills and knows how to use the tools

When RBAC rules change (tools granted or revoked), Agentworks updates the rulesync configuration and
regenerates. This keeps the agent's knowledge in sync with its actual permissions.

### Tool-to-package mapping

The mapping from tool name to package uses the manifest. Each package declares which tools it
provides. The Agentworks CLI reads all manifests to build the mapping:

| Tool name                | Package    |
| ------------------------ | ---------- |
| `nerf-git-push-origin`   | `nerf-git` |
| `nerf-git-push-non-main` | `nerf-git` |
| `nerf-az-account-show`   | `nerf-az`  |
| `nerf-wcid`              | `nerf-wcid`|

### Skill content

Each package skill describes:

- The family of tools it covers and what each one does
- When to use each tool (the specific scenarios)
- How to invoke each tool (command, arguments if any)
- Expected output on success
- How to handle denials (constructive -- ask the operator)
- Relationship to other tools (e.g. "use `nerf-wcid` to check your permissions first")

### Non-rulesync workspaces

For workspaces that do not use rulesync, the operator can manually extract skills from packages and
configure their AI tool of choice, or agents can request the information via `nerf-wcid`.

## Emergency Shutdown (bigred)

### Mechanism: lockfile

The bigred mechanism uses a lockfile at `/opt/agentworks/nerf/etc/bigred.lock`. When this file
exists, `nerfrun` checks for it before RBAC evaluation and denies unconditionally. This design has
several properties:

- **Atomic**: creating or deleting a single file is an atomic operation
- **Non-destructive**: RBAC rules, credentials, and agent users are untouched
- **Trivially verifiable**: `test -f bigred.lock` confirms the state
- **Resume is deletion**: removing the lockfile restores normal operations

The lockfile is owned by `agentworks:agentworks` with mode `0600`.

The lockfile contains metadata for forensics:

```text
activated_at=2026-03-08T15:00:00Z
activated_by=agentworks-cli
reason=manual
```

### Bigred check ordering

The bigred lockfile check is the very first thing `nerfrun` does, before reading the real UID,
before parsing RBAC, before anything else. This ensures the shortest possible code path between
invocation and denial.

### Known limitation: in-flight operations

A nerfed tool that has already passed the bigred check and is mid-operation will not be interrupted
by a subsequent bigred activation. The window is small (the check happens at the start of
execution), but it exists. This is an accepted trade-off -- the alternative (filesystem watches,
signal handling) adds significant complexity for minimal benefit.

### CLI: `agentworks nerf bigred`

#### Activation

```text
agentworks nerf bigred [--vm <name>] [--yes --yes --yes]
```

Interactive mode (default):

```text
$ agentworks nerf bigred
This will disable ALL privileged agent operations on ALL VMs.
RBAC rules and credentials will be preserved for postmortem.

Are you sure you want to proceed? [y/N] y

This action takes effect immediately. Agents will not be able to
perform any nerfed operations until you resume.

Are you really sure? [y/N] y

Final confirmation. This will affect 3 VMs: dev-1, dev-2, staging-1.

Type YES to confirm: YES

Applying bigred to dev-1... done
Applying bigred to dev-2... done
Applying bigred to staging-1 (starting VM)... done (VM stopped again)

Bigred active on 3 VMs.
```

Scripted mode skips all prompts:

```text
agentworks nerf bigred --yes --yes --yes
```

#### VM states during activation

| VM state            | Action                                         |
| ------------------- | ---------------------------------------------- |
| Running             | SSH in, create lockfile, confirm               |
| Stopped/deallocated | Start VM, create lockfile, confirm, stop VM    |
| Unreachable         | Mark `bigred-unreachable` in DB, block all ops |

VMs marked `bigred-unreachable` require manual operator intervention. All Agentworks operations on
that VM (shell, workspace commands, agent commands) are blocked until the operator resolves the
situation.

#### Resume

```text
agentworks nerf bigred --resume [--vm <name>]
```

Requires a single confirmation prompt:

```text
$ agentworks nerf bigred --resume
This will restore normal nerfed tool operations on 2 VMs: dev-1, dev-2.
Note: 1 VM (staging-1) is marked bigred-unreachable and must be
resolved manually.

Are you sure you want to resume? [y/N] y

Resuming dev-1... done
Resuming dev-2... done

Normal operations restored on 2 VMs.
1 VM remains in bigred-unreachable state.
```

Resume is not available for `bigred-unreachable` VMs. The operator must manually inspect the VM,
resolve the issue, and clear the state.

### Database tracking

The Agentworks database tracks bigred state per VM:

| Column              | Type     | Description                              |
| ------------------- | -------- | ---------------------------------------- |
| `bigred_state`      | text     | `none`, `active`, or `unreachable`       |
| `bigred_at`         | datetime | When bigred was activated                |
| `bigred_resumed_at` | datetime | When bigred was resumed (null if active) |

These columns are added to the existing `vms` table.

### Operator reminders

When any VM has a non-`none` bigred state, the CLI surfaces warnings:

- **`agentworks vm list`**: a status indicator next to affected VMs
- **`agentworks vm status <name>`**: full bigred details (state, timestamp)
- **Top-level commands**: a warning line printed before normal output when any VMs are in bigred
  state, e.g.: `WARNING: 2 VMs in bigred state. Run 'agentworks nerf bigred --resume'.`

### Operations blocked during bigred-unreachable

When a VM is in the `bigred-unreachable` state, the following operations are blocked at the CLI
level (before any SSH or API call):

- `agentworks vm shell`
- `agentworks workspace shell`
- `agentworks workspace create/delete`
- `agentworks agent create/delete`

The operator must first resolve the unreachable state. This ensures that no one accidentally
interacts with a VM that could not be confirmed as locked down.

## Future: Auto-Approval in Coding Platforms

The `nerf-wcid` output provides the exact information a coding platform needs to auto-approve tool
use. A Claude Code hook (or equivalent) could:

1. On tool invocation, run `nerf-wcid` to get the agent's current permissions
2. If the requested tool is in the list and not expired, auto-approve
3. Otherwise, prompt the human operator

This turns nerfed tool access into a declarative permission system that bridges the gap between
VM-level security and coding platform UX. The operator grants permissions via RBAC rules, and the
coding platform respects them automatically.
