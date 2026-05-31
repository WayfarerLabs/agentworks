# Nerf Tools -- Functional Requirements

## Problem Statement

Agents need to perform sensitive operations -- pushing code, calling cloud APIs, querying external
services -- but direct access to the underlying tools is too broad. An agent with unrestricted `git`
can force-push to main. An agent with `az` can do anything the authenticated identity allows.

The nerf tools layer provides **defanged versions of underlying tools**: each nerf tool performs one
scoped operation, with fixed arguments and validated parameters. The tool cannot be misused because
it simply cannot do more than what it is designed to do.

Enforcement is at the application layer: AI coding frameworks (Claude Code, Cursor, and others)
control which tools an agent is permitted to call. The nerf tools layer provides the right
abstraction boundary -- one tool per scoped operation -- so that framework-level permissions are
meaningful and auditable. A permission to call `nerf-git-push-origin` is precise. A permission to
call `git` is not.

This design does not rely on OS-level privilege escalation (SUID). The tools run as the agent user.
Credential access is handled by whatever the agent already has access to (git credential helpers,
environment variables, etc.). Scoped credential injection -- where specific credentials are provided
per tool at provisioning time -- is a future capability described in the Future section.

## Personas

### Operator

The human who owns the VM. Provisions workspaces, defines nerf packages for the tools agents need,
installs built tools onto the VM, and configures which nerf tools the AI coding framework will allow
agents to invoke.

### Package author

Defines a nerf package: a manifest that describes a family of related scoped tools. Often the same
person as the operator, but could also be a team or community member sharing a package. Writes TOML,
not code.

### AI agent

Operates inside a workspace. Has access to exactly the nerf tools the operator has permitted. Calls
them like any other CLI tool. Cannot invoke the underlying tool directly (because the underlying
tool is either not on the agent's PATH, or the framework permission model blocks it, or both).

### AI coding framework

The platform driving the agent (Claude Code, Cursor, etc.). Controls which tools the agent can
invoke via its permission model. Nerf tool names -- one per scoped operation -- provide the right
granularity for these permissions.

## Requirements

### R1: Manifest-based tool definitions

Each nerf package is defined by a YAML manifest that fully describes a family of related scoped
tools. The manifest is the single source of truth for:

- What each tool does (description)
- What underlying command it invokes
- What arguments and flags are fixed vs. parameterized
- Parameter validation rules (pattern, allow-list, deny-list)
- Rulesync skill metadata

The manifest requires no code. A package author writes YAML and gets a family of tools with built-in
validation and documentation.

### R2: Build system

The Agentworks CLI reads a manifest and generates standalone executable shell scripts -- one per
tool -- from it. The build process:

1. Parses and validates the manifest
2. For each tool definition, generates a shell script that:
   - Parses and validates the allowed arguments per the manifest spec
   - Constructs the underlying command from the validated inputs
   - Executes the command (no shell interpolation -- exec with explicit args)
3. Writes scripts to a specified output directory

The generated scripts are self-contained and human-readable. They do not require the manifest to be
present at runtime -- only at build time. They can be inspected, committed to version control, and
deployed like any other script.

### R3: Claude Code permissions management

The Agentworks CLI provides commands to manage nerf tool permissions in the AI coding framework's
configuration. For Claude Code, this means managing `settings.json` entries that allow specific nerf
tool invocations.

Operators can:

- Grant permission for an agent to call a specific nerf tool
- Revoke that permission
- List current permissions for a workspace

Permissions are written to the appropriate `settings.json` (global, project-level, or
workspace-level) per the operator's intent.

This creates a complete workflow: define tools in a manifest, build the scripts, grant permissions
via the CLI, and the agent can use exactly those tools.

### R4: Rulesync skills

Each nerf package manifest contains enough information (tool descriptions, parameter descriptions,
usage examples) to generate a rulesync skill file. The Agentworks CLI generates these skills as part
of the build step or on demand.

The generated skill covers the full package: what each tool does, when to use it, how to invoke it,
what arguments it accepts, and what to expect on success or error. The agent's AI coding tool picks
up the skill via rulesync and knows how to use the tools without further documentation.

## Future

### Scoped credential injection

A future mode where the operator specifies, per tool, which credentials should be injected at
workspace provisioning time. Examples:

- A deploy key scoped to one repository, injected for `nerf-git-push-origin`
- A GitHub token with specific permissions, injected for `nerf-gh-*` tools
- An Azure token scoped to one resource group, injected for `nerf-az-*` tools

With credential injection, nerf tools become useful even in environments where the application
permission layer is not available or not configured -- the injected credentials are themselves
scoped, so the tool can only do what the credentials allow. This is a complementary enforcement
mechanism, not a replacement for application-layer permissions.

### Discovery (nerf-wcid)

A tool that lists which nerf tools are available and permitted for the calling agent. Useful for
agents that need to self-discover capabilities. Likely tied to permission management and injection
metadata once those exist.

### Emergency shutdown (bigred)

A mechanism to immediately disable all nerf tool operations across one or more VMs. Design is
deferred until the credential injection model is defined, since the mechanism depends on how
credentials are managed.

### Audit trail

Structured logging of nerf tool invocations. Current enforcement via the application layer provides
some audit capability (framework logs). A dedicated audit mechanism may be added alongside
credential injection.

## Out of Scope

- **OS-level privilege enforcement (SUID)**: this may be introduced as a separate mechanism for
  different use cases, but is not part of the nerf tools model.
- **Network-level controls**: orthogonal, handled separately if at all.
- **Container isolation**: the nerf tools layer operates at the script/executable level.
- **Dynamic tool installation by agents**: only the operator can install or update nerf packages.
