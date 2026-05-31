# Agent tmux sockets -- high-level architecture

## Current model

```text
Console pane PTY
  -> tmux attach -t <session>          (admin user, connects to admin's tmux server)
    -> tmux server (admin user)
      -> pane PTY
        -> sudo su --login <agent>     (use_pty allocates inner PTY -- resize breaks here)
          -> agent shell (inner PTY, stuck at 80x24)
```

## New model

```text
Console pane PTY
  -> tmux -S <socket> attach -t <session>    (admin user, socket has group permissions)
    -> tmux server (agent user, owns socket)
      -> pane PTY
        -> agent shell (directly on pane PTY -- resize works)
```

## Socket layout

Directories are keyed on the **Linux username** (e.g., `agt--alice`). The VM layer operates entirely
in terms of Linux users -- it has no database and no concept of "agent name". The CLI layer on the
operator's workstation resolves agent name to Linux username via the database before issuing any VM
commands. This is consistent with how the rest of the VM-side infrastructure works (workspace
groups, home directories, file ownership, process isolation).

```text
/run/agentworks/agent-tmux-sockets/      root:tmux-agent-access 2771
  agt--alice/                            agt--alice:tmux-agent-access 2770
    workspace--task.sock                 agt--alice:tmux-agent-access 0770*
  agt--bob/                              agt--bob:tmux-agent-access 2770
    workspace--task.sock                 agt--bob:tmux-agent-access 0770*
```

\*tmux creates sockets at 0700; must be chmod'd to 0770 after session creation.

### Directory permissions

- **Root directory** (`/run/agentworks/agent-tmux-sockets/`): Owned by `root`, group
  `tmux-agent-access`, mode `2771`. SGID ensures subdirectories inherit the group. The `other`
  execute bit allows agent users (who are not in the group) to traverse into their own subdirectory.
- **Per-agent directory**: Owned by the agent's Linux user, group `tmux-agent-access` (inherited via
  SGID), mode `2770`. The agent can create sockets here. The admin can access via group membership.
  Other agents cannot access (no owner match, not in group).

### Why the group works

Only the admin user is added to `tmux-agent-access`. Agent users are not members. Each agent's
subdirectory is owned by that agent's Linux user, so the agent has owner access. The SGID bit
propagates the group to new files (sockets), and the admin has group access. Other agents have
neither owner nor group access to each other's directories.

The agent user needs to be able to create files in their own directory. Since they own the directory
(mode `2770` = `rwxrws---`), they have owner `rwx`. This is sufficient.

### tmux server-access ACL

In addition to filesystem permissions on the socket, tmux 3.3+ has a server-level ACL. By default,
only the socket owner (agent user) can connect. tmux's `server-access` command is user-level only
(no group support), so after session creation we enumerate the members of the `tmux-agent-access`
group and grant each one access:

```bash
for user in $(getent group tmux-agent-access | cut -d: -f4 | tr ',' ' '); do
  sudo -u <agent> tmux -S <socket> server-access -a "$user"
done
```

Today this will just be the admin user, but it is robust to future changes (e.g., additional
operators or monitoring users added to the group).

This is a belt-and-suspenders approach: filesystem group permissions AND tmux server-access.

## Socket path derivation

Given a Linux username and a session name (which is `<workspace>--<task>`):

```text
/run/agentworks/agent-tmux-sockets/<linux_user>/<session>.sock
```

This is deterministic from the Linux username and session name. The CLI layer resolves agent name to
Linux username from the database before calling any VM-side function.

## Changes by component

### Infrastructure setup (idempotent)

| Trigger        | Action                                                                                                                                                                   |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| VM init/reinit | Create group `tmux-agent-access`, add admin user. Create root socket directory with permissions. Loop over all existing agents and ensure per-agent subdirectory exists. |
| Agent create   | Create per-agent socket subdirectory.                                                                                                                                    |
| Agent reinit   | Ensure per-agent socket subdirectory exists.                                                                                                                             |

### Task session lifecycle

| Operation        | Current (agent mode)                                                    | New (agent mode)                                                                                                                             |
| ---------------- | ----------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Create session   | `tmux new-session -d -s <session> ... "sudo su --login <agent> -c ..."` | `sudo -u <agent> tmux -S <socket> new-session -d -s <session> ...` then `chmod g+rwx <socket>` then `server-access -a` for each group member |
| Check exists     | `tmux has-session -t <session>`                                         | `tmux -S <socket> has-session -t <session>`                                                                                                  |
| Kill session     | `tmux kill-session -t <session>`                                        | `tmux -S <socket> kill-session -t <session>`                                                                                                 |
| Send keys        | `tmux send-keys -t <session> ...`                                       | `tmux -S <socket> send-keys -t <session> ...`                                                                                                |
| Capture output   | `tmux capture-pane -t <session> ...`                                    | `tmux -S <socket> capture-pane -t <session> ...`                                                                                             |
| Attach (console) | `unset TMUX; tmux attach -t <session>`                                  | `unset TMUX; tmux -S <socket> attach -t <session>`                                                                                           |

Admin-mode tasks are unchanged (no `-S` flag, no socket).

### Function signature changes

Functions in `tmux.py` that currently take `(workspace_name, task_name, run_command)` will gain an
optional `socket_path: str | None = None` parameter. When provided, `-S <socket_path>` is prepended
to the tmux command. A helper `agent_socket_path(linux_user, workspace_name, task_name)` derives the
path.

The console wrapper (shell script string) and tmuxinator config will embed the socket path directly.

### Config for the agent's tmux server

The restricted task config (`/opt/agentworks/tmux-task.conf`) continues to be used via
`-f <config>`. Since it's a system-wide file readable by all users, the agent's tmux server can load
it.

The task config starts with `if-shell "test -f ~/.tmux.conf" "source-file ~/.tmux.conf"`. In the
current model, `~` resolves to the admin user's home, so inner and outer sessions share the same
base config (prefix key, mouse settings, etc.). In the new model, `~` resolves to the agent user's
home. If the agent user has no `~/.tmux.conf`, tmux defaults apply (prefix `Ctrl-b`). If the agent
user has a different config, the inner session's prefix may differ from the outer session's.

This is a known limitation with minor impact:

- **Console usage**: the outer tmux intercepts the prefix key. The inner session's prefix is
  irrelevant since keystrokes never reach it directly.
- **Direct attach** (`agentworks task attach`): the operator would need to use the inner session's
  prefix to detach. If the agent user has no custom config, this is the default `Ctrl-b d`. If the
  agent user has a different prefix, the operator needs to know it.
- **Accidental inner detach from console**: if the operator sends the inner session's prefix + `d`
  through the console, the inner session detaches. The console wrapper immediately re-attaches
  (`while ... do tmux attach; sleep 0.5; done`), so this causes a brief flicker, not a breakage.

In practice, agent users are automated and unlikely to have custom tmux configs. This is not worth
adding complexity to address.

### What about the agent's shell command?

Currently, admin-mode sessions optionally run a command via `$SHELL -lic "cd <path> && <command>"`.
Agent-mode sessions wrap this in `sudo su --login`.

In the new model, the session is created as the agent user
(`sudo -u <agent> tmux -S <socket> new-session ...`). The `new-session` command itself can specify
the shell command. Since `tmux new-session` runs as the agent user, the shell inherits the agent's
environment. We still want `$SHELL -lic` to source rc files, but we no longer need
`sudo su --login`.

Open question: does `sudo -u <agent> tmux new-session` give us a proper login environment? The
`tmux new-session` starts a shell in the pane, but it is not a login shell by default. We may need
the `-f` config to include something, or use `exec $SHELL -l` as the pane command. Alternatively,
`sudo --login -u <agent> tmux ...` gives sudo a login context, but the tmux pane shell may still not
be a login shell. This needs testing during implementation.
