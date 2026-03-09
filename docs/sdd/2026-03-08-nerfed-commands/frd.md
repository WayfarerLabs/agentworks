# Nerfed Commands -- Functional Requirements

**Builds on:** [User-Based Security Model](../2026-03-08-user-based-security/)

## Problem Statement

The [user-based security model](../2026-03-08-user-based-security/) establishes Linux user isolation
for agents: each agent runs as its own unprivileged user, workspaces are group-isolated, and tools
are read-only. That model prevents agents from tampering with tools and accessing other workspaces.

However, agents still need to perform certain privileged or sensitive operations -- pushing code,
accessing cloud APIs, reading calendars -- but giving them direct access to the underlying tools
(git, az cli, etc.) is too broad. An agent with unrestricted git push can force-push to main. An
agent with az cli access can do anything the authenticated identity allows.

The nerfed commands layer sits on top of the user-based security model and provides:

- Specific, scoped operations (not entire tools)
- Per-agent permissions with time-boxing
- Credential inheritance (tools run as the user account, which already holds credentials)
- Full auditability (who ran what, when)
- Tool-agnostic (works with any AI coding tool)
- No daemon required (SUID + config files)

## Personas

### Operator

The human who owns the VM. Logs in as the user account (`agentworks`), manages agent permissions,
configures which agents can run which nerfed tools and for how long, and installs and updates nerfed
tools. This is the same identity used for all non-agent work on the VM -- it is the user's account,
not a special service account.

### AI agent

Operates inside a workspace as an unprivileged Linux user. Needs to perform scoped operations (push
to a specific remote, query a calendar, run a scoped az command) without direct access to the
underlying credentials or tools.

### AI coding tool

The platform driving the agent (Claude Code, Cursor, etc.). In the future, nerfed tool access will
be used to auto-approve tool use in the coding platform so the agent does not need to prompt the
human for every operation.

## Requirements

### R1: Nerfed tool executables

Each nerfed tool performs a single scoped operation. From the agent's perspective, each tool is a
standalone executable in a system-wide directory. Under the hood, all tools are implemented via
`nerfrun` -- a single SUID binary that reads declarative manifest files to determine what each tool
does. See the HLA for the nerfrun architecture and manifest format.

Examples:

- `nerf-git-push-non-origin` -- push to a non-origin remote
- `nerf-git-push-origin` -- push to the origin remote
- `nerf-az-account-show` -- run `az account show`
- `nerf-calendar-today` -- query today's calendar events

When an agent invokes a nerfed tool, `nerfrun`:

1. Records the real (calling) user identity
2. Assumes the user account identity (via SUID)
3. Checks RBAC rules to verify the caller is authorized
4. Validates parameters against the manifest spec
5. Performs the scoped operation using the user account's existing credentials
6. Returns the result to the caller

### R2: SUID identity -- the user account

Nerfed tools are SUID to the user account (`agentworks`) rather than a dedicated service user. The
user account is the human operator's identity on the VM -- it holds all authenticated sessions
(`az login`, `gh auth login`, SSH keys, etc.) and has unrestricted sudo. When a nerfed tool executes
via SUID, it inherits these credentials naturally.

This eliminates the need for credential brokering, service principal management, or duplicated
authentication state. The user logs in once (as they already do), and nerfed tools piggyback on that
identity. There is no separate "nerf user" -- the SUID target is the same account the human uses
interactively.

### R3: RBAC rules

A configuration file (readable only by the user account) that maps agent users to the specific
nerfed tools they are allowed to run. Rules support:

- **Agent user**: the Linux username of the agent (no globs)
- **Tool**: the specific nerfed tool name (no globs)
- **Expiration**: an optional UTC timestamp after which the rule is no longer valid. This enables
  time-boxed access (e.g. "ws1--coder can push until 2026-03-09T00:00:00Z").

If no matching rule exists, the tool refuses to execute. Expired rules are treated as nonexistent.

Denial messages must be constructive: they should tell the agent (and the human reading the output)
what happened and what to do about it. For example, a denied tool should print something like
"Permission denied. Ask your operator to grant access to nerf-git-push-origin." rather than a bare
"access denied". This helps AI agents self-diagnose and request the right permission from their
human operator.

### R4: Discovery (nerf-wcid)

A `nerf-wcid` ("what can I do") tool that any agent user can run to see which nerfed tools they are
currently authorized to use. Output includes tool names and expiration times (if any). This serves
two purposes:

- Agents (and their driving AI tools) can discover available capabilities
- Future: coding platforms can use this to auto-approve tool use

### R5: Credential inheritance

Nerfed tools inherit the user account's existing credentials via SUID. When a tool runs as the user
account, it has access to:

- **Azure**: the user's `az login` session (token cache in the user's home directory). `nerf-az-*`
  tools call `az` directly and it just works.
- **GitHub**: the user's `gh auth login` session. `nerf-gh-*` tools call `gh` directly.
- **SSH (git operations)**: the SSH agent socket from the user-based security model. The agent user
  already has `SSH_AUTH_SOCK` set, and the nerfed tool's role is purely RBAC enforcement.
- **Calendars, APIs, etc.**: any credentials the user has configured in their account.

This is a significant simplification over a credential brokering model. There are no service
principal files to manage, no token caches to synchronize, no separate credential stores. The user
authenticates once as they normally would, and agents inherit that authentication through the SUID
mechanism, gated by RBAC.

On Azure VMs, managed identity is also available and works automatically since the SUID process runs
as a normal user on the VM.

### R6: Configuration management

The user account owns and manages all nerf configuration directly:

- Creates and updates RBAC rules
- Installs and updates nerf packages (the installable unit for nerfed tools -- each package bundles
  manifests, optional scripts, and rulesync skills for a family of related tools)
- Manages the nerf config directory (no sudo needed -- it is the user's own files)

Agent users have no access to the nerf config directory or package contents. They interact with the
system solely through the nerfed tool executables.

### R7: Audit trail

All nerfed tool executions are logged with: calling user, tool name, timestamp, and outcome
(success/failure/denied). This leverages the per-agent Linux user model -- every execution is
attributable to a specific agent.

### R8: Rulesync skills for AI agents

Nerfed tools ship with rulesync skills that teach AI coding assistants how to use them. Skills are
grouped by domain (e.g. a single `nerf-git` skill covers all nerfed git tools) rather than one skill
per tool.

When Agentworks provisions a workspace that uses both rulesync and nerfed tools, it automatically
configures the workspace's rulesync setup to include the relevant skill groups based on the agent's
authorized tools. This means an agent's AI coding tool automatically knows about the nerfed tools it
can use, without manual configuration.

This creates a complete chain:

1. Operator grants RBAC access to nerfed tools
2. Agentworks determines which skill groups are relevant and includes them in the workspace's
   rulesync configuration
3. The AI coding tool learns about the tools via rulesync-generated config
4. The agent uses the tools with full context on behavior and error handling
5. Future: auto-approval bridges the last gap (no human prompt needed)

### R9: Emergency shutdown (bigred)

A "big red button" that immediately disables all nerfed tool operations across one or more VMs. This
is the operator's panic button for security incidents, runaway agents, or any situation where
privileged agent operations must stop immediately.

Key properties:

- **Non-destructive**: does not modify or delete RBAC rules, credentials, or agent users. The
  existing configuration is preserved for postmortem analysis.
- **Immediate effect**: once applied to a VM, all subsequent nerfed tool invocations are denied
  unconditionally, regardless of RBAC rules.
- **Resumable**: the operator can restore normal operations after the incident is resolved. Resume
  requires explicit confirmation.
- **Scoped**: can target all VMs, a specific VM, or a set of VMs.
- **Tracked**: bigred state is recorded in the Agentworks database. The CLI surfaces bigred status
  in relevant commands and reminds the operator when VMs are in the bigred state.

#### Activation flow

`agentworks nerf bigred [--vm <name>] [--yes --yes --yes]`

Interactive mode presents three sequential confirmation prompts with escalating "are you sure?"
language. The operator must press enter on each to proceed. The `--yes` flag can be passed three
times to skip all prompts for scripted emergency response.

For each targeted VM:

1. If the VM is running: apply the bigred lockout and confirm success.
2. If the VM is stopped/deallocated: start the VM, apply the bigred lockout, confirm success, then
   stop the VM again.
3. If the VM cannot be reached or the lockout cannot be confirmed: mark the VM as
   `bigred-unreachable` in the database and block all Agentworks operations on that VM until an
   operator manually resolves it.

#### Resume flow

`agentworks nerf bigred --resume [--vm <name>]`

Requires a single confirmation prompt. Restores normal nerfed tool operations by removing the
lockout. For VMs marked `bigred-unreachable`, resume is not available -- the operator must manually
inspect and resolve.

#### Operator reminders

The CLI surfaces bigred status in:

- `agentworks vm list` -- shows bigred state next to affected VMs
- `agentworks vm status` -- includes bigred details
- Top-level commands -- a warning line when any VMs are in bigred state

## Future

### Auto-approval in coding platforms

Nerfed tool access can drive auto-approval in AI coding platforms. For example, Claude Code hooks
could call `nerf-wcid` to determine which tools the agent is allowed to run and auto-approve
matching tool use requests. This eliminates the human-in-the-loop bottleneck for pre-authorized
operations.

### Tool-specific configuration

Beyond RBAC, nerfed tools may have their own configuration (e.g. which calendars to expose, which
Azure subscriptions to allow). This configuration lives in the nerf config directory alongside RBAC
rules.

## Out of Scope

- **Network-level controls**: restricting agent network access is orthogonal and handled separately
  (if at all).
- **Container isolation**: nerfed commands work at the Linux user level, not the container level.
- **Dynamic tool installation**: agents cannot install new nerfed tools. Only the operator can.
