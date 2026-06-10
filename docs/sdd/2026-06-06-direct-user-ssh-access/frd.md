# Direct target-user SSH access: FRD

**Status:** Draft **Repo:** `agentworks` **Path:** `cli/agentworks/`

## Background

Agentworks reaches every VM through SSH; every interaction with a VM is ultimately a shell that
agentworks opens over SSH. Each opened shell has two characteristics that vary independently:

- **Target user**: the Linux uid the shell runs as. Today either the admin user or one of the agent
  users.
- **Purpose**: what the shell is being opened for. Three current purposes: running a provisioning
  script, opening an interactive shell directly, or creating a tmux session.

These dimensions define a 2x3 matrix of six combinations. How agentworks reaches each combination
today:

|                         | admin user                             | agent user                                                       |
| ----------------------- | -------------------------------------- | ---------------------------------------------------------------- |
| **Provisioning script** | `ssh admin@vm <script>`                | (unused today; no provisioning currently runs at the agent uid)  |
| **Direct interactive**  | `ssh -t admin@vm` (e.g. `vm shell`)    | `ssh admin@vm + sudo su --login <agent>` (`agent shell`)         |
| **Tmux session create** | `ssh admin@vm tmux new-session -d ...` | `ssh admin@vm + sudo --login -u <agent> tmux new-session -d ...` |

Tmux _attach_ operations are not in this matrix; they connect to an existing tmux server via admin's
existing socket access (established by the 2026-04-10 agent-tmux-sockets SDD) and stay unchanged by
this work.

The admin-user column is straightforward: SSH as admin and do the work (provisioning or
interactive). The agent-user column is not. Currently, it routes through admin SSH plus a
`sudo --login -u <agent>` step. That detour carries real downstream costs:

- **`sudo --login` strips env.** The default sudoers policy resets the environment to a near-empty
  baseline. Anything the calling shell sets is gone before the agent's process starts. Workarounds
  (per-invocation `--preserve-env`, sudoers `env_keep` extensions) all add VM-side config surface
  area.
- **Two distinct code paths.** Admin operations go directly; agent operations route through sudo.
  Every shell-opening site has to know which path applies, and any new "do work as the agent" site
  has to remember the detour.
- **Mixed dimensions in code.** The user-identity decision (admin vs. agent) is conflated with the
  shell-purpose decision (script / interactive / tmux) because the sudo detour applies to one axis
  but appears at every shell-opening site.

The agent-user column was the path of least resistance when agents were introduced. It sidestepped
per-user SSH credential management. The costs above have accumulated since then.

This SDD changes the agent-user column to **direct target-user SSH**: `ssh <agent>@vm` instead of
`ssh admin@vm + sudo --login -u <agent>`. After this work, every shell agentworks opens has the same
shape regardless of target user.

The cleanup is independently valuable for the reasons above. It is also a precondition for the
in-flight env-and-secrets SDD, which assumes the cleaner access model.

## Process visibility today

Default Linux exposes process information broadly: `/proc/<pid>/cmdline` is mode 0444 and `/proc` is
mounted with `hidepid=0` (verified on an existing agentworks VM), meaning any user on the VM can
read any other user's command line, environment, and other per-pid metadata. Importantly, this means
that any secrets entered at the command line are readable by any user on the VM.

This is independent of the access cleanup but worth fixing in the same SDD: the threat-model surface
is the same (controlling who can see what on the VM), the mitigation fits naturally into VM
provisioning, and downstream SDDs that pass material over the SSH transport will benefit from the
tighter baseline.

## Terminology

- **Target user**: the Linux uid a shell runs as. Either the admin user or an agent user.
- **Purpose**: what the shell is being opened for. One of `provisioning`, `direct interactive`, or
  `tmux session create` (see R2 for the canonical enumeration).
- **Direct target-user SSH**: SSHing to the VM as the target user directly, rather than as admin and
  `sudo`ing. The change this SDD codifies.
- **Authorized keys reconciliation**: the existing mechanism (`_reconcile_authorized_keys` in
  `vms/initializer.py`) that writes a user's `~/.ssh/authorized_keys` from operator config.
  Currently called only for the admin user; this SDD extends its use to agent users.
- **`hidepid`**: kernel `/proc` mount option controlling cross-user visibility of process
  information. Default is 0 (everyone sees everything). This SDD adopts `hidepid=1`.

## Requirements

### R1: Direct target-user SSH for agent operations

Operations whose subject is the agent (the work is being done _as_ the agent's Linux uid) open SSH
directly as the agent's Linux user, over Tailscale, rather than SSH'ing as admin and
`sudo --login -u <agent>`. Concretely:

- Agent-mode session creation (`sessions/tmux.create_session`, agent path) replaces
  `ssh admin@vm "sudo --login -u <agent> tmux ..."` with `ssh <agent>@vm "tmux ..."`.
- Agent-mode session restart follows the same pattern: the rebuilt tmux server is spawned over a
  direct agent SSH session.
- `agent shell` replaces `ssh admin@vm + sudo su --login <agent>` with `ssh -t <agent>@vm`.
- Any future site whose subject is the agent follows the same pattern.

The choice of target user is explicit at each call site and derived from the operation's subject.
All new agent SSH connections, like all existing admin SSH connections, go over Tailscale.

#### Out of scope: admin operations that read or maintain agent state

Many operations are admin operations even though they read or affect agent-owned tmux servers. These
continue to SSH as admin and continue to use the existing socket / group permissions /
`server-access` ACL path established by the 2026-04-10 agent-tmux-sockets SDD. They are explicitly
out of scope for the direct-target-user-SSH flip:

- **`session list`** and other batch reads. A single `tmux has-session` compound across all sessions
  on a VM in one SSH call (admin) is materially faster than fanning out one SSH per (VM, agent).
  Keep as-is.
- **`session attach`** and any other tmux-attach surface (`console attach`, etc.): admin attaches to
  an existing tmux server through group permissions on the socket. Unchanged.
- **`force_kill_tmux_server`** against an agent's tmux pid: admin via sudo. Unchanged.
- **All console operations** (`console create`, `console add-session`, `console add-shell`,
  `console attach`, `console restore-session`, and the rest). The console is admin-owned and is the
  only mechanism in agentworks for mixing multi-agent and admin windows in a single tmux server.
  `console add-session` references an agent-owned tmux session, but the console- management action
  itself is performed as admin against admin's tmux server.
- **All provisioning operations** and `vm shell` / `workspace shell`. Already admin today;
  unchanged.

The infrastructure that supports admin's cross-uid reach (group-shared sockets,
`_grant_server_access`, the tmux `server-access` ACL) is preserved unchanged and remains
load-bearing for the operations above. This SDD flips the _write_ path for agent operations; the
existing infrastructure remains the _read_, _attach_, and _maintenance_ path for admin operations
that touch agent state.

### R2: Three access modes as the codified pattern

Every shell agentworks opens falls into one of three modes. Each mode has a single canonical
transport pattern.

| Mode                               | Target user options | SSH form           | Stdin               |
| ---------------------------------- | ------------------- | ------------------ | ------------------- |
| 1. Running a script (provisioning) | admin (today)       | `ssh admin@vm`     | Script piped in     |
| 2. Interactive shell               | admin or agent      | `ssh -t <user>@vm` | Operator's terminal |
| 3. Tmux session creation           | admin or agent      | `ssh <user>@vm`    | Script piped in     |

Mode 1 is admin-only as of today; no provisioning work currently runs at an agent uid. Nothing in
the model precludes agent-level provisioning later. When a use case for it emerges, mode 1 picks up
an agent option without changing the mode's transport pattern.

Modes 2 and 3 take a target-user parameter and SSH as that user. The `sudo --login` detour
disappears entirely.

Tmux attaches (`session attach`, `console attach`, etc.) are not in this table because they don't
open a new shell; they connect to an existing tmux server via admin's existing socket access. Their
SSH form is unchanged.

### R3: Authorized keys lifecycle for agent users

Agent users must accept the operator's SSH public key(s) so that direct SSH as the agent works. This
is achieved by extending the existing reconciliation mechanism to agent users:

- **Agent create**: after the agent's Linux user exists, write `<agent_home>/.ssh/authorized_keys`
  with the operator's primary key (`operator.ssh_public_key`) plus every entry in
  `operator.extra_ssh_public_keys`. Same set of keys that admin's `authorized_keys` receives at VM
  init; reuses the same `_reconcile_authorized_keys` helper.
- **Agent reinit**: re-run the reconciliation. Picks up additions or removals to
  `operator.extra_ssh_public_keys` and applies them.
- **Agent delete**: no special handling. `userdel --remove` (the existing path) removes the home
  directory and its authorized_keys file along with it.
- **Operator key rotation**: operator updates config and runs `agent reinit` to sync each affected
  agent. The sync is manual, not automatic. This matches the pattern operators already follow for
  admin via `vm reinit`.

Authorized_keys files for agents share admin's managed-header convention, including the warning that
manual edits are overwritten.

### R4: VM hardening at provisioning

VMs are provisioned with the following hardening, applied at `vm create` and re-applied on
`vm reinit`. The hardening composes with the access cleanup: direct target-user SSH narrows the uid
that sees secret material on the wire, and `hidepid=1` then closes the cross-uid argv leak that
would otherwise expose that material to any user on the VM. Together they move the baseline from
"permissive process visibility under admin+sudo" to "tight per-uid isolation under direct
target-user SSH."

`vm reinit` re-application is idempotent: the second run is a no-op unless the hardening file
contents have changed. HLA spells out the recipe.

#### R4a: `/proc` mounted with `hidepid=1`

`/etc/fstab` is updated (or the equivalent systemd mount unit is written) so that `/proc` is mounted
with `hidepid=1`. Mode 1 prevents non-owners from reading files inside `/proc/<pid>/` (notably
`cmdline`, `environ`, `status`), while leaving the directory entry itself visible. Mode 2 is
rejected for now because the additional benefit (hiding pid existence) is not part of the threat
model and risks breaking tools that walk `/proc`.

The agentworks pid-existence checks (`test -d /proc/<pid>`) are expected to work cross-uid under
mode 1 (see R5).

#### R4b: Sysctl audit pass

The following sysctls are set explicitly at VM init, defaulting to the safer value if not already
applied:

- `kernel.dmesg_restrict=1` restricts `dmesg` to `CAP_SYS_ADMIN`.
- `kernel.kptr_restrict=1` hides kernel pointers from `/proc` and similar.
- `kernel.yama.ptrace_scope=1` restricts ptrace to descendant processes (Debian default is 0
  historically; this raises the baseline).
- `fs.protected_hardlinks=1`, `fs.protected_symlinks=1`, `fs.protected_fifos=2`,
  `fs.protected_regular=2` provide symlink/hardlink/fifo/regular-file protection against common
  attack patterns.
- `kernel.unprivileged_bpf_disabled=1` disables BPF for non-privileged users. Agentworks does not
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

Sessions created before this SDD lands (under the admin+sudo agent-session pattern) continue to
function without intervention:

- Their tmux servers already run at the agent uid (the previous `sudo --login -u <agent>` step at
  create time placed them there). The running state on the VM is identical to what the new code
  would produce.
- `session attach` connects via the existing socket and group permissions (unchanged).
- `session list` reads liveness via `test -d /proc/<pid>` (unchanged).
- When such a session is restarted, the new code path takes over and the rebuilt session enters the
  new world naturally.

No data migration, no compat shim, no operator action required.

Future SDDs that build on this one (e.g. env-and-secrets) may require restart for old sessions to
pick up _their_ new behavior. That is a concern of those SDDs, not this one.

### R7: Operator-side SSH config entries for agents

Agentworks already maintains operator-side SSH config entries for each VM (typically
`Host awvm--<vm>` under the operator's `~/.ssh/config.d/` directory, derived from
`operator.ssh_host_prefix`). This SDD extends that to per-agent entries so the operator can
`ssh awvm--<vm>--<agent>` to land directly in the agent's shell, and so tooling such as VS Code
Remote-SSH can target agents explicitly.

Lifecycle (mirrors admin entries):

- **Agent create**: write a `Host awvm--<vm>--<agent>` entry. HostName derives from the VM's
  Tailscale host (same as the admin entry); `User` is the agent's Linux user; `IdentityFile`
  inherited from the operator config.
- **Agent reinit**: refresh the entry. Picks up any changes to host or identity paths.
- **Agent delete**: remove the entry.

This is a single declarative process: the same code path satisfies first-time create and subsequent
reinit. The host-prefix shape (`awvm--<vm>--<agent>`) is reserved here so that future naming choices
for per-agent entries remain compatible.

## Non-goals

- **Env variable propagation of any kind.** This SDD is the prerequisite for the env-and-secrets SDD
  and is ultimately motivated by it, but it does not specify any env-related behavior itself.
  Establishing standard `AGENTWORKS_*` identity vars, exposing user-defined env, propagating secrets
  over the SSH transport: all owned by env-and-secrets, not here.
- **Admin operations that touch agent state.** Admin's existing read / attach / maintenance paths
  into agent-owned tmux servers (via the 2026-04-10 SDD's group permissions and `server-access` ACL)
  remain unchanged. This SDD flips only the _write_ path for agent operations. See R1 for the full
  list of admin operations that stay on the existing mechanism.
- **Removing the agent-tmux-sockets infrastructure.** Per the above, the group-shared sockets +
  `server-access` ACL model from the 2026-04-10 SDD continues to provide admin's access path for
  batch read operations and attaches and remains load-bearing.
- **Sunsetting `vm console` / `workspace console`.** These legacy single-console entry points are
  worth eventually replacing with the named-console infrastructure, but that is a separate cleanup.
  Out of scope here.
- **Per-agent SSH key separation.** Agents share the operator's keys, not their own. If a future use
  case requires per-agent SSH credentials with finer-grained ACLs, that is its own design.
- **Tightening sudoers.** Admin retains `NOPASSWD:ALL`. The motivation for any env-related sudoers
  tweak disappears under direct target-user SSH.
- **Migration of long-lived sessions to the new code path.** Existing sessions are not retroactively
  recreated. They migrate naturally on next restart (R6).
- **`hidepid=2` + `gid=proc`.** Mode 2 with a process-visibility group is a viable future hardening
  if the threat model expands to include pid-existence leaks. Not in scope here.
