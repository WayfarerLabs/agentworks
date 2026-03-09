# User-Based Security Model -- High-Level Architecture

## Overview

This design uses standard Linux users, groups, and file permissions to isolate agent workspaces and
protect tools from modification. The user account holds all credentials (SSH keys, az login, gh
auth); agents access them only through [nerfed commands](../2026-03-08-nerfed-commands/) via SUID.

## User and Group Topology

```text
Users:
  agentworks          (user account, uid 1000, sudo)
  <workspace>--<agent>    (agent, no sudo)
  ...

Groups:
  ws-<workspace>      (workspace file access)
  aw-tools            (tools read/execute access)
```

### User account (`agentworks`)

The operator's identity on the VM. This is the human's own account, not a special service account.
It has admin privileges (unrestricted sudo) because the operator needs full control over the VM.

- Created during Phase A of VM init (unchanged)
- Unrestricted sudo via `/etc/sudoers.d/agentworks`
- Owns `/opt/agentworks/tools/` (the tools directory)
- Runs the SSH agent systemd service
- Holds all authenticated sessions (az login, gh auth login, SSH keys, etc.)

### Agent users (`<workspace>--<agent>`)

- Created when an agent is provisioned on a workspace
- Home directory: `/home/<workspace>--<agent>/`
- No sudo, no password
- Shell: `/bin/bash` (no dotfiles, no customization)
- Member of: `ws-<workspace>`, `aw-tools`
- No direct access to credentials -- agents use nerfed commands (SUID) for privileged operations

### Workspace group (`ws-<workspace>`)

- Created when a workspace is created on the VM
- The workspace directory has ownership `agentworks:ws-<workspace>` with mode `2775` (setgid)
- Setgid ensures new files inherit the group
- All agents assigned to the workspace are members

### Tools group (`aw-tools`)

- The tools directory `/opt/agentworks/tools/` has ownership `agentworks:aw-tools` with mode `0750`
- Executables inside are `0755` (world-executable) or `0750` (group-only)
- Agent users are in `aw-tools`, giving them read/execute access
- Only the user account can write to the tools directory

## Directory Layout

```text
/home/agentworks/                  # user account home
  .ssh/
    id_ed25519                     # VM keypair (600, user account only)
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
  ssh-agent.sock                   # 0600 agentworks:agentworks

/home/myproject--coder/            # agent home
  .bashrc                          # agent prompt, no credentials
```

## SSH Agent Daemon

### Purpose

Provides the user account with SSH key access for git operations (clone, fetch, push). The socket is
owned by the user account with mode `0600` -- only the user account (and processes running as the
user account via SUID) can access it. Agents cannot access the socket directly.

### Implementation

A systemd service runs `ssh-agent` as the user account and loads the VM's key on startup.

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
ExecStartPost=/bin/chmod 0600 /run/agentworks/ssh-agent.sock
RuntimeDirectory=agentworks
RuntimeDirectoryMode=0700

[Install]
WantedBy=multi-user.target
```

### Credential access model

The user account has `SSH_AUTH_SOCK` set in its environment. Agent users have no access to the
socket. Git operations (and other credential-gated operations) are performed through
[nerfed commands](../2026-03-08-nerfed-commands/), which run as the user account via SUID and set
`SSH_AUTH_SOCK` in their constructed environment.

## Workspace Creation Flow

When a workspace is created on a VM:

1. Create the workspace group: `groupadd ws-<name>`
2. Create the workspace directory with setgid: `mkdir -p /home/agentworks/workspaces/<name>`
   `chown agentworks:ws-<name> ...` `chmod 2775 ...`
3. Clone the repo (if template specifies one)
4. Ensure cloned files have correct group: `chgrp -R ws-<name> ...`

## Agent Provisioning Flow

When an agent is created for a workspace:

1. Create the agent user: `useradd -m -s /bin/bash <workspace>--<agent>`
2. Add to groups: `usermod -aG ws-<workspace>,aw-tools <workspace>--<agent>`
3. Write minimal `.bashrc` (agent prompt, no credentials)
4. Record the agent user in the DB

## Permissions Summary

| Path             | Owner      | Group      | Mode | Effect                    |
| ---------------- | ---------- | ---------- | ---- | ------------------------- |
| Workspace dir    | agentworks | ws-NAME    | 2775 | Agents read/write, setgid |
| Tools dir        | agentworks | aw-tools   | 0750 | Agents read/execute       |
| SSH agent socket | agentworks | agentworks | 0600 | User account only         |
| SSH key          | agentworks | agentworks | 0600 | User account only         |
| Agent home       | agent-X    | agent-X    | 0750 | Agent-private             |

## Impact on Existing Code

### VM initialization (Phase A/B)

- Create `aw-tools` group
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

### Existing user account operations

- `vm shell` continues to SSH as the user account
- `workspace shell` continues to SSH as the user account (the user is in all groups and has sudo)
- Tool installation targets `/opt/agentworks/tools/`

## Security Considerations

### What this model prevents

- Agents modifying their own tools
- Agents accessing other workspaces' files
- Agents killing other agents' processes
- Agents escalating to root
- Agents accessing credentials directly (SSH keys, az/gh sessions)

### What this model does not prevent

- Agents reading system-wide readable files (`/etc/passwd`, installed packages, etc.)
- Agents making arbitrary network requests
- Agents consuming resources (mitigated by optional cgroups/ulimits, not enforced by default)

### Trust boundary

The VM is the trust boundary. If repo-level or network-level isolation is needed, use separate VMs.

## Relationship to Nerfed Commands

This model provides the foundation for the [nerfed commands](../2026-03-08-nerfed-commands/) layer.
Where this model isolates agents from each other and from the user's account, nerfed commands add
controlled, auditable, time-boxed access to specific privileged operations (git push, cloud CLI
commands, etc.) via SUID executables owned by the user account. The nerfed commands layer adds a
`nerf-exec` group to the topology defined here and uses the user account as the SUID identity,
inheriting the user's existing credentials.
