# Nerfed Commands -- High-Level Architecture

**Builds on:** [User-Based Security Model](../2026-03-08-user-based-security/)

## Overview

Nerfed commands add a controlled privilege layer on top of the user-based security model. Where that
model isolates agents from each other and from the admin layer, nerfed commands give agents a
narrow, auditable, time-boxed path to perform specific privileged operations.

The mechanism is SUID executables owned by a dedicated `nerf` user. Each executable performs one
scoped operation, checks RBAC rules before executing, and logs every invocation. No daemon, no
socket, no IPC -- just the filesystem and standard Unix SUID semantics.

## Security Layer Diagram

```text
+------------------------------------------------------------------+
|                        VM (trust boundary)                        |
|                                                                   |
|  admin (agentworks)                                               |
|    - sudo, full control                                           |
|    - manages nerf config (write)                                  |
|    - installs nerf tools                                          |
|    - manages agent lifecycle                                      |
|                                                                   |
|  nerf                                                             |
|    - no sudo, no login                                            |
|    - owns SUID executables                                        |
|    - owns config dir (read-only for nerf, no access for others)   |
|    - holds brokered credentials                                   |
|                                                                   |
|  ws1--coder, ws1--reviewer, ...                                    |
|    - no sudo, no login shell (except via coding tool)             |
|    - workspace access via group                                   |
|    - tools access via aw-tools group (read/execute)               |
|    - nerfed operations via SUID executables only                  |
+------------------------------------------------------------------+
```

## User and Group Topology (additions)

Building on the user-based security model topology:

```text
Users (new):
  nerf                (no sudo, no login, SUID identity)

Groups (new):
  nerf-exec           (execute access to nerf tool binaries)
```

### Nerf user

- Created during VM initialization alongside the admin user
- No sudo, no password, no login shell (`/usr/sbin/nologin`)
- Home directory: `/home/nerf/` (exists but unused)
- Not a member of any workspace or tools group
- Owns `/opt/agentworks/nerf/bin/` and `/opt/agentworks/nerf/etc/`

### Nerf-exec group

- Agent users are added to this group during agent provisioning
- Grants execute (but not read) access to nerfed tool binaries
- This prevents agents from reading the binary contents while still allowing SUID execution

## Directory Layout

```text
/opt/agentworks/nerf/
  bin/                              # 0710 nerf:nerf-exec
    nerf-git-push-origin            # 4711 nerf:nerf-exec (SUID)
    nerf-git-push-non-origin        # 4711 nerf:nerf-exec (SUID)
    nerf-wcid                       # 4711 nerf:nerf-exec (SUID)
    nerf-az-account-show            # 4711 nerf:nerf-exec (SUID)
    ...
  etc/                              # 0700 nerf:nerf (nerf-only)
    bigred.lock                     # if present, all tools deny (bigred)
    rbac.toml                       # RBAC rules
    credentials/                    # brokered credentials
      azure.json                    # service principal or managed identity
      github-token                  # GitHub PAT
      ...
```

### Permission rationale

- `bin/` is `0710 nerf:nerf-exec`: the group can enter and execute files, but cannot list the
  directory. This means agents can run a tool by name but cannot enumerate all available tools (they
  use `nerf-wcid` for that).
- Each binary is `4711 nerf:nerf-exec`: the SUID bit (4) causes execution as the nerf user. Owner
  can read/write/execute, group and others can execute.
- `etc/` is `0700 nerf:nerf`: only the nerf user can read. Since the SUID binaries execute as nerf,
  they can read RBAC rules and credentials. The calling agent user cannot.
- The admin user manages `etc/` contents via sudo (e.g.
  `sudo -u nerf vim /opt/agentworks/nerf/etc/rbac.toml`).

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

When a nerfed tool is invoked:

1. Tool reads the real UID of the calling process (`getuid()` before SUID takes effect, or the saved
   real UID)
2. Resolves the real UID to a username
3. Reads `rbac.toml`
4. Searches for a rule matching both the username and the tool name
5. If a matching rule has an `expires` field, checks against current UTC time
6. If no valid rule is found: log denial, exit non-zero
7. If a valid rule is found: proceed with the operation

### No globs

RBAC rules use exact matches for both user and tool. No wildcards, no patterns, no regex. This keeps
the security model simple and auditable. The admin explicitly grants each agent access to each tool.

## SUID Execution Flow

```text
Agent (ws1--coder) runs: nerf-git-push-origin

1. Kernel sets effective UID to nerf (SUID bit)
   Real UID remains ws1--coder
2. Tool checks for /opt/agentworks/nerf/etc/bigred.lock
   - If present: log BIGRED, print constructive message, exit 1
3. Tool reads real UID -> "ws1--coder"
4. Tool reads /opt/agentworks/nerf/etc/rbac.toml
5. Checks: does ws1--coder have access to nerf-git-push-origin?
   - If no: log DENIED, print constructive message, exit 1
   - If expired: log EXPIRED, print constructive message, exit 1
6. Tool performs the scoped git push operation
   - Uses SSH_AUTH_SOCK for git credentials (from user-based security model)
   - Or uses brokered credentials from etc/credentials/
7. Log "OK ws1--coder nerf-git-push-origin", exit with operation result
```

## Discovery (nerf-wcid)

`nerf-wcid` is itself a SUID nerfed tool. Every agent user should have access to it (the admin adds
a rule for each agent). When run:

1. Reads real UID, resolves to username
2. Checks RBAC for access to `nerf-wcid` itself
3. Reads all rules for the calling user
4. Outputs a list of authorized tools with expiration info

Example output:

```text
nerf-git-push-origin
nerf-git-push-non-origin  expires: 2026-03-09T00:00:00Z
nerf-az-account-show
nerf-wcid
```

## Credential Brokering

### Pattern

Nerfed tools that need external credentials read them from `/opt/agentworks/nerf/etc/credentials/`.
This directory is only readable by the nerf user, so credentials are inaccessible to agents. The
SUID binary reads the credential, authenticates, performs the operation, and returns the result.

### Azure

On Azure VMs, managed identity is preferred:

- No secrets to store or rotate
- The nerf tool calls the Azure Instance Metadata Service (IMDS) endpoint to obtain a token
- Works automatically for any Azure operation the VM's managed identity is authorized for

On non-Azure VMs (or when managed identity is insufficient):

- A service principal credential (`azure.json`) is stored in `etc/credentials/`
- Contains `tenant_id`, `client_id`, and `client_secret`
- Nerf tools use this to obtain Azure AD tokens

### GitHub

- A PAT or `gh auth` token stored in `etc/credentials/github-token`
- Nerf tools pass this as a bearer token for GitHub API calls

### SSH (git operations)

Git push/pull operations use the existing SSH agent socket from the user-based security model. The
calling agent user is already in the `agentworks-ssh` group and has `SSH_AUTH_SOCK` set. No
additional credential brokering is needed for git-over-SSH -- the nerfed tool's role is purely RBAC
enforcement.

## Audit Logging

All nerfed tool invocations are logged to syslog (or a dedicated log file at
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

## Tool Implementation

### Language choice

Nerfed tools should be compiled, statically linked binaries. SUID scripts are disabled by most Linux
kernels (the kernel ignores SUID on interpreted files). Reasonable options:

- **Go**: good stdlib, static linking by default, minimal attack surface
- **Rust**: same benefits, stricter memory safety
- **C**: works but higher risk of memory safety issues in security-critical code

Go is the likely default for simplicity. Each tool is a small, self-contained binary.

### Shared library

All nerfed tools share common logic:

- Check for `bigred.lock` (deny all if present)
- Read real UID and resolve to username
- Parse `rbac.toml`
- Evaluate RBAC rules (match user + tool, check expiration)
- Log the invocation
- Load credentials from `etc/credentials/`

This is a shared Go package (or similar), compiled into each binary. No shared `.so` files -- each
binary is fully self-contained.

### Constructive denial messages

When a nerfed tool denies an operation, it prints a constructive message to stderr that tells the
caller what happened and what to do about it. This is critical because the caller is often an AI
agent that will read the output and can act on clear instructions.

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

- **No shell expansion**: tools never pass arguments through a shell. All subprocess calls use exec
  directly with explicit argument arrays.
- **Input validation**: all arguments are validated before use. Tools that accept no arguments (most
  of them) reject any arguments.
- **Drop privileges**: after reading RBAC and credentials, tools drop the effective UID back to the
  real UID where possible before executing the underlying operation.
- **Minimal scope**: each tool does exactly one thing. No flags to change behavior, no configuration
  beyond what is in `etc/`.

## Impact on User-Based Security Model

The nerfed commands layer adds to the existing topology:

### VM initialization

- Create the `nerf` user and `nerf-exec` group
- Create `/opt/agentworks/nerf/bin/` and `/opt/agentworks/nerf/etc/`
- Set permissions as described above
- Install nerfed tool binaries

### Agent provisioning

- Add new agents to the `nerf-exec` group (in addition to existing groups)
- Add default RBAC rules (at minimum, `nerf-wcid` access)

### Agent deletion

- Remove RBAC rules for the deleted agent user

### Permissions summary (additions)

| Path             | Owner | Group     | Mode | Effect                           |
| ---------------- | ----- | --------- | ---- | -------------------------------- |
| Nerf bin dir     | nerf  | nerf-exec | 0710 | Agents execute, cannot list      |
| Nerf binaries    | nerf  | nerf-exec | 4711 | SUID to nerf, agents can execute |
| Nerf config      | nerf  | nerf      | 0700 | Nerf-only (SUID binaries read)   |
| Nerf credentials | nerf  | nerf      | 0700 | Nerf-only                        |

## Rulesync Skills

### Overview

Nerfed tools ship with rulesync skills grouped by domain. A single skill covers a family of related
tools (e.g. `nerf-git` covers `nerf-git-push-origin`, `nerf-git-push-non-origin`, etc.). This keeps
the number of skills manageable and provides cohesive documentation for related operations.

### Skill packaging

Skills are distributed alongside the tool binaries, organized by domain group:

```text
/opt/agentworks/nerf/
  skills/                             # 0755 nerf:nerf-exec
    nerf-git/
      SKILL.md                        # covers all nerf-git-* tools
    nerf-az/
      SKILL.md                        # covers all nerf-az-* tools
    nerf-wcid/
      SKILL.md                        # discovery tool
    ...
```

Skills are readable by agent users (via the `nerf-exec` group) so that rulesync can read and copy
them during generation.

### Automatic workspace configuration

When Agentworks provisions a workspace that uses rulesync, it configures the workspace's rulesync
setup to import the relevant skill groups based on the agent's authorized nerfed tools. The flow:

1. Operator grants RBAC access to nerfed tools for an agent
2. Agentworks reads the agent's RBAC rules to determine authorized tools
3. Agentworks maps authorized tools to their skill groups (e.g. `nerf-git-push-origin` maps to the
   `nerf-git` skill group)
4. Agentworks configures rulesync in the workspace to import the matching skill groups from
   `/opt/agentworks/nerf/skills/`
5. When rulesync generates output (e.g. `.claude/`, `.cursor/`), the skills are included
   automatically
6. The AI coding tool picks up the skills and knows how to use the tools

When RBAC rules change (tools granted or revoked), Agentworks updates the rulesync configuration and
regenerates. This keeps the agent's knowledge in sync with its actual permissions.

### Tool-to-skill mapping

The mapping from tool name to skill group uses the tool name prefix:

| Tool name prefix | Skill group |
| ---------------- | ----------- |
| `nerf-git-*`     | `nerf-git`  |
| `nerf-az-*`      | `nerf-az`   |
| `nerf-gh-*`      | `nerf-gh`   |
| `nerf-wcid`      | `nerf-wcid` |

Standalone tools (like `nerf-wcid`) map to their own skill. Tools that share a domain prefix map to
the same group skill. New tool families follow the same convention.

### Skill content

Each domain skill describes:

- The family of tools it covers and what each one does
- When to use each tool (the specific scenarios)
- How to invoke each tool (command, arguments if any)
- Expected output on success
- How to handle denials (constructive -- ask the operator)
- Relationship to other tools (e.g. "use `nerf-wcid` to check your permissions first")

### Non-rulesync workspaces

For workspaces that do not use rulesync, the skills directory is still available on the filesystem.
Operators can manually configure their AI tool of choice to read from
`/opt/agentworks/nerf/skills/`, or agents can read the skill files directly.

## Emergency Shutdown (bigred)

### Mechanism: lockfile

The bigred mechanism uses a lockfile at `/opt/agentworks/nerf/etc/bigred.lock`. When this file
exists, every nerfed tool checks for it before RBAC evaluation and denies unconditionally. This
design has several properties:

- **Atomic**: creating or deleting a single file is an atomic operation
- **Non-destructive**: RBAC rules, credentials, and agent users are untouched
- **Trivially verifiable**: `test -f bigred.lock` confirms the state
- **Resume is deletion**: removing the lockfile restores normal operations

The lockfile is owned by `nerf:nerf` with mode `0600`. The admin user creates and deletes it via
sudo.

The lockfile contains metadata for forensics:

```text
activated_at=2026-03-08T15:00:00Z
activated_by=agentworks-cli
reason=manual
```

### Bigred check ordering

The bigred lockfile check is the very first thing every nerfed tool does, before reading the real
UID, before parsing RBAC, before anything else. This ensures the shortest possible code path between
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
VM-level security and coding platform UX. The admin grants permissions via RBAC rules, and the
coding platform respects them automatically.
