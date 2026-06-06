# Direct user SSH access -- functional requirements

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

## Background

Agentworks opens shells on VMs through SSH. Two consistent patterns exist today, plus a third that
sits awkwardly in between:

1. **Admin shells** -- SSH as the admin user. Used for provisioning, workspace operations, all
   `target.run(...)` plumbing, and the `vm shell` interactive entry point.
2. **Agent shells** -- SSH as admin, then `sudo --login -u <agent>` to drop into the agent's Linux
   identity. Used for agent-mode session creation (`sudo --login -u agent tmux ...`) and the
   `agent shell` interactive entry point.
3. **Tmux attaches** -- SSH as admin (with PTY), connect to an existing tmux server. Socket
   permissions and the tmux `server-access` ACL grant admin reach into agent-owned tmux servers.
   Established by the 2026-04-10 agent-tmux-sockets SDD.

Pattern 2 (admin + sudo for agent operations) was the path of least resistance when agents were
introduced, because it sidestepped per-user SSH credential management. It has accumulated downstream
costs:

- **sudo strips env.** `sudo --login` resets the environment to a clean state by default. Anything
  set in the calling shell's env (which the upcoming env-and-secrets SDD relies on for propagation
  to tmux servers) is gone before the agent's tmux ever starts. Working around this requires sudoers
  tweaks or per-invocation `--preserve-env`, both of which add VM-side config surface area.
- **Two distinct code paths for "do work as user X".** Admin operations go directly; agent
  operations route through sudo. Every shell-opening site has to know which path applies.
- **The three-mode framing doesn't naturally surface.** The actual access modes -- run a script,
  open an interactive shell, spawn a tmux session -- get conflated with the user-identity decision
  because of the sudo detour.

Separately, default Linux exposes process information broadly: `/proc/<pid>/cmdline` is mode 0444
and `/proc` is mounted with `hidepid=0`, meaning any user on the VM can read any other user's
command line. SSH preludes carrying secret material would be visible to any local uid.

This SDD codifies a cleaner access model and tightens the related kernel-level visibility, paving
the way for the env-and-secrets work and any future per-user remote operations.

## Terminology

- **Access mode**: how agentworks reaches a shell on the VM. Three modes (see R2).
- **Subject**: the Linux user the shell runs as. Currently either the admin user or an agent user.
- **Direct user SSH**: SSH'ing to the VM as the subject directly, rather than as admin and sudo'ing.
  The change this SDD codifies.
- **Authorized keys reconciliation**: the existing mechanism (`_reconcile_authorized_keys` in
  `vms/initializer.py`) that writes a user's `~/.ssh/authorized_keys` from operator config.
  Currently called only for the admin user; this SDD extends its use to agent users.
- **`hidepid`**: kernel `/proc` mount option controlling cross-user visibility of process
  information. Default is 0 (everyone sees everything). This SDD adopts `hidepid=1` for VMs.

## Requirements

### R1: Direct user SSH for agent operations

Operations whose subject is an agent open SSH directly as the agent's Linux user, rather than
SSH'ing as admin and `sudo --login -u <agent>`. Concretely:

- Agent-mode session creation (`sessions/tmux.create_session`, agent path) replaces
  `ssh admin@vm "sudo --login -u <agent> tmux ..."` with `ssh <agent>@vm "tmux ..."`.
- `agent shell` replaces `ssh admin@vm + sudo su --login agent` with `ssh -t <agent>@vm`.
- Any other "perform work as the agent" site follows the same pattern.

Operations whose subject is admin continue to SSH as admin. The choice of which user to SSH as is
explicit at the call site, derived from the operation's subject.

The existing socket / group / `server-access` ACL infrastructure for cross-user tmux attach
(established by the 2026-04-10 agent-tmux-sockets SDD) is preserved unchanged. Admin retains its
existing ability to attach to agent-owned tmux servers via group permissions. This SDD changes _who
opens the SSH connection for what kind of operation_, not the on-VM socket model.

### R2: Three access modes as the codified pattern

Every shell agentworks opens falls into one of three modes. Each mode has a single canonical
transport pattern.

| Mode                               | Subject options | SSH form           | Stdin               |
| ---------------------------------- | --------------- | ------------------ | ------------------- |
| 1. Running a script (provisioning) | admin           | `ssh admin@vm`     | Script piped in     |
| 2. Interactive shell               | admin or agent  | `ssh -t <user>@vm` | Operator's terminal |
| 3. Tmux session creation           | admin or agent  | `ssh <user>@vm`    | Script piped in     |

Mode 1 is admin-only by definition -- provisioning requires admin rights and operates before any
agent exists. Modes 2 and 3 take a subject parameter and SSH as that subject. The `sudo --login`
detour disappears entirely.

Tmux attaches (`session attach`, `console attach`, etc.) are not in this table because they don't
open a new shell; they connect to an existing tmux server via admin's existing socket access. Their
SSH form is unchanged.

### R3: Authorized keys lifecycle for agent users

Agent users must have the operator's SSH public key(s) in their `~/.ssh/authorized_keys` so that
direct SSH as the agent works. This is achieved by extending the existing reconciliation mechanism:

- **Agent create**: after the agent's Linux user exists, write the operator's authorized_keys
  (primary + extras) to `<agent_home>/.ssh/authorized_keys` using the same logic that runs for admin
  today.
- **Agent reinit**: re-run reconciliation. Picks up additions/removals to
  `operator.extra_ssh_public_keys` and reflects them.
- **Agent delete**: no special handling needed. `userdel --remove` (the existing path) takes the
  home directory with it.
- **Operator key rotation**: operator updates config; `agent reinit` syncs each affected agent. Same
  pattern as VM reinit for admin authorized_keys today.

Authorized_keys files for agents share the same managed-header convention as admin's, including the
warning that manual edits are overwritten.

### R4: VM hardening at provisioning

VMs are provisioned with the following hardening, applied at `vm create` and re-applied on
`vm reinit`:

#### R4a: `/proc` mounted with `hidepid=1`

`/etc/fstab` is updated (or the equivalent systemd mount unit is written) so that `/proc` is mounted
with `hidepid=1`. Mode 1 prevents non-owners from reading files inside `/proc/<pid>/` (notably
`cmdline`, `environ`, `status`), while leaving the directory entry itself visible. Mode 2 is
rejected for now because the additional benefit (hiding pid existence) is not part of the threat
model and risks breaking tools that walk `/proc`.

The agentworks pid-existence checks (`test -d /proc/<pid>`) are confirmed to work cross-uid under
mode 1 (see R5).

#### R4b: Sysctl audit pass

The following sysctls are set explicitly at VM init, defaulting to the safer value if not already
applied:

- `kernel.dmesg_restrict=1` -- restricts `dmesg` to `CAP_SYS_ADMIN`.
- `kernel.kptr_restrict=1` -- hides kernel pointers from `/proc` and similar.
- `kernel.yama.ptrace_scope=1` -- restricts ptrace to descendant processes (Debian default is 0
  historically; this raises the baseline).
- `fs.protected_hardlinks=1`, `fs.protected_symlinks=1`, `fs.protected_fifos=2`,
  `fs.protected_regular=2` -- symlink/hardlink/fifo/regular-file protection against common attack
  patterns.
- `kernel.unprivileged_bpf_disabled=1` -- disables BPF for non-privileged users. Agentworks does not
  require unprivileged BPF.

These are sysctls, applied via `/etc/sysctl.d/99-agentworks.conf`, picked up at boot and on reload.

### R5: Empirical verification of pid-check under hidepid=1

Before any other implementation work, verify on a real provisioned VM that
`test -d /proc/<other-uid-pid>` returns success for admin against agent processes under `hidepid=1`.
The four call sites (`sessions/manager.py:_pid_alive`, `sessions/manager.py` batch status compound,
`sessions/tmux.py force_kill_tmux_server` two checks) all rely on this. Kernel semantics indicate it
should work, but procfs has historical edge cases worth confirming empirically. If the check fails,
route those call sites through sudo before proceeding.

This verification is captured as the first item in the plan.

### R6: Existing sessions continue to work

Sessions created before this SDD lands (under the old admin+sudo agent-session pattern) continue to
function without intervention:

- Their tmux servers already run as the agent uid (the previous `sudo --login` did that work at
  create time). The running state on the VM is identical to what the new code would produce.
- `session attach` connects via the existing socket and group permissions -- unchanged.
- `session list` reads liveness via `test -d /proc/<pid>` -- unchanged.
- The only behavioral difference is that the old sessions lack the env vars the new model would
  inject. This is no regression: they didn't have those env vars before either.
- When such a session is restarted, the new code path takes over and it enters the new world
  naturally.

No data migration, no compat shim, no operator action required.

## Non-goals

- **Removing the agent-tmux-sockets infrastructure.** The group-shared sockets + `server-access` ACL
  model from the 2026-04-10 SDD continues to provide admin's access path for batch read operations
  and attaches. Direct-user-SSH is the _write_ path for agent operations; cross-user socket access
  remains the _read_ path.
- **Sunsetting `vm console` / `workspace console`.** These legacy single-console entry points are
  worth eventually replacing with the named-console infrastructure, but that's a separate cleanup.
  Out of scope here.
- **Forwarded-socket env transport.** The env-and-secrets SDD's v1 wire is argv-on-SSH-command-line
  (mitigated by R4a's `hidepid=1`). The eventual move to a forwarded-socket env transport is its own
  follow-on SDD.
- **Per-agent SSH key separation.** Agents share the operator's keys, not their own. If a future use
  case requires per-agent SSH credentials with finer-grained ACLs, that is its own design.
- **Tightening sudoers.** Admin retains `NOPASSWD:ALL`. The motivation for the env-related sudoers
  tweak we discussed (preserving env across `sudo --login`) disappears under direct-user-SSH, so no
  sudoers changes are needed.
- **Migration of long-lived sessions to the new code path.** Existing sessions are not retroactively
  recreated. They migrate naturally on next restart (R6).
- **`hidepid=2` + `gid=proc`.** Mode 2 with a process-visibility group is a viable future hardening
  if the threat model expands to include pid-existence leaks. Not in scope here.
