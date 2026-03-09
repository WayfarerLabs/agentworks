# User-Based Security Model -- High-Level Architecture

## Overview

This design uses standard Linux users, groups, and file permissions to isolate agent workspaces and
protect tools from modification. A systemd-managed SSH agent daemon shares git credentials across
all users without exposing key material.

## User and Group Topology

```text
Users:
  agentworks          (admin, uid 1000, sudo)
  <workspace>--<agent>    (agent, no sudo)
  ...

Groups:
  agentworks-ssh      (SSH agent socket access)
  ws-<workspace>      (workspace file access)
  aw-tools            (tools read/execute access)
```

### Admin user (`agentworks`)

- Created during Phase A of VM init (unchanged)
- Unrestricted sudo via `/etc/sudoers.d/agentworks`
- Owns `/opt/agentworks/tools/` (the tools directory)
- Runs the SSH agent systemd service
- Member of: `agentworks-ssh`

### Agent users (`<workspace>--<agent>`)

- Created when an agent is provisioned on a workspace
- Home directory: `/home/<workspace>--<agent>/`
- No sudo, no password
- Shell set to the configured default (e.g., zsh)
- Member of: `ws-<workspace>`, `agentworks-ssh`, `aw-tools`

### Workspace group (`ws-<workspace>`)

- Created when a workspace is created on the VM
- The workspace directory has ownership `agentworks:ws-<workspace>` with mode `2775` (setgid)
- Setgid ensures new files inherit the group
- All agents assigned to the workspace are members

### Tools group (`aw-tools`)

- The tools directory `/opt/agentworks/tools/` has ownership `agentworks:aw-tools` with mode `0750`
- Executables inside are `0755` (world-executable) or `0750` (group-only)
- Agent users are in `aw-tools`, giving them read/execute access
- Only the admin user can write to the tools directory

## Directory Layout

```text
/home/agentworks/                  # admin home
  .ssh/
    id_ed25519                     # VM keypair (600, admin-only)
    id_ed25519.pub
/home/agentworks/workspaces/       # workspace roots
  myproject/                       # 2775 agentworks:ws-myproject
    .git/
    src/
    ...
  another/                         # 2775 agentworks:ws-another
    ...

/opt/agentworks/
  tools/                           # 0750 agentworks:aw-tools
    mcp-servers/
    cli-tools/

/run/agentworks/
  ssh-agent.sock                   # 0660 agentworks:agentworks-ssh

/home/myproject--coder/            # agent home
  .config/                         # agent-specific config
```

## SSH Agent Daemon

### Purpose

Provides all users on the VM with the ability to perform git operations (clone, fetch, push) using
the VM's registered SSH key, without exposing the private key to agent users.

### Implementation

A systemd service runs `ssh-agent` as the admin user and loads the VM's key on startup.

```ini
# /etc/systemd/system/agentworks-ssh-agent.service
[Unit]
Description=Agentworks SSH Agent
After=network.target

[Service]
Type=forking
User=agentworks
Environment=SSH_AUTH_SOCK=/run/agentworks/ssh-agent.sock
ExecStartPre=/bin/mkdir -p /run/agentworks
ExecStart=/usr/bin/ssh-agent -a /run/agentworks/ssh-agent.sock
ExecStartPost=/usr/bin/ssh-add /home/agentworks/.ssh/id_ed25519
RuntimeDirectory=agentworks
RuntimeDirectoryMode=0750

[Install]
WantedBy=multi-user.target
```

Post-start, the socket permissions are set:

```bash
chgrp agentworks-ssh /run/agentworks/ssh-agent.sock
chmod 0660 /run/agentworks/ssh-agent.sock
```

### Client configuration

Agent users have `SSH_AUTH_SOCK` set in their environment (via `/etc/environment.d/` or profile
script):

```bash
export SSH_AUTH_SOCK=/run/agentworks/ssh-agent.sock
```

Git operations then use the agent socket transparently.

## Workspace Creation Flow

When a workspace is created on a VM:

1. Create the workspace group: `groupadd ws-<name>`
2. Create the workspace directory with setgid: `mkdir -p /home/agentworks/workspaces/<name>`
   `chown agentworks:ws-<name> ...` `chmod 2775 ...`
3. Clone the repo (if template specifies one)
4. Ensure cloned files have correct group: `chgrp -R ws-<name> ...`

## Agent Provisioning Flow

When an agent is created for a workspace:

1. Create the agent user: `useradd -m -s /bin/zsh <workspace>--<agent>`
2. Add to groups: `usermod -aG ws-<workspace>,agentworks-ssh,aw-tools <workspace>--<agent>`
3. Set `SSH_AUTH_SOCK` in the agent's environment
4. Record the agent user in the DB

## Permissions Summary

| Path             | Owner      | Group          | Mode | Effect                    |
| ---------------- | ---------- | -------------- | ---- | ------------------------- |
| Workspace dir    | agentworks | ws-NAME        | 2775 | Agents read/write, setgid |
| Tools dir        | agentworks | aw-tools       | 0750 | Agents read/execute       |
| SSH agent socket | agentworks | agentworks-ssh | 0660 | Agents use, not read key  |
| Admin SSH key    | agentworks | agentworks     | 0600 | Admin only                |
| Agent home       | agent-X    | agent-X        | 0750 | Agent-private             |

## Impact on Existing Code

### VM initialization (Phase A/B)

- Create `agentworks-ssh` and `aw-tools` groups
- Set up `/opt/agentworks/tools/` directory
- Install and enable the SSH agent systemd service
- Load the generated SSH key into the agent after Phase A generates the keypair

### Workspace creation (VM backend)

- Create the workspace group
- Set ownership and setgid on the workspace directory
- Ensure cloned repo files have correct group ownership

### New: agent management

- New CLI commands: `agent create`, `agent list`, `agent delete`
- New DB table for agents (name, workspace, Linux user)
- Agent user creation/deletion with group membership

### Existing admin operations

- `vm shell` continues to SSH as the admin user
- `workspace shell` continues to SSH as the admin user (admin is in all groups and has sudo)
- Tool installation targets `/opt/agentworks/tools/`

## Security Considerations

### What this model prevents

- Agents modifying their own tools
- Agents accessing other workspaces' files
- Agents killing other agents' processes
- Agents escalating to root

### What this model does not prevent

- Agents reading system-wide readable files (`/etc/passwd`, installed packages, etc.)
- Agents making arbitrary network requests
- Agents using the SSH agent to access any repo the VM key is registered with (by design, since git
  access is shared per-VM)
- Agents consuming resources (mitigated by optional cgroups/ulimits, not enforced by default)

### Trust boundary

The VM is the trust boundary. All agents on a VM share the same git credentials and network. If
repo-level or network-level isolation is needed, use separate VMs.

## Relationship to Nerfed Commands

This model provides the foundation for the [nerfed commands](../2026-03-08-nerfed-commands/) layer.
Where this model isolates agents from each other and from the user's account, nerfed commands add
controlled, auditable, time-boxed access to specific privileged operations (git push, cloud CLI
commands, etc.) via SUID executables owned by the user account. The nerfed commands layer adds a
`nerf-exec` group to the topology defined here and uses the user account as the SUID identity,
inheriting the user's existing credentials.
