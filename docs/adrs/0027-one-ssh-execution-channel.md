# 27. One SSH Execution Channel; Platforms Keep Only a Root Channel

Date: 2026-11-18

## Status

Accepted

## Context

Agentworks runs work on a managed VM after provisioning: commands, scripts, file placement, terminal
attachment, port forwards and work that must outlive its connection. Earlier releases did this over
several channels. Core rendered commands onto the OpenSSH command line and copied files with `scp`,
and each platform also exposed a general native transport (`limactl shell`, `wsl.exe`, the Proxmox
guest agent, SSH to a cloud VM's public address) that core used as a second execution target, for
first contact, recovery and some ordinary operations.

That shape had standing costs:

- **Every operation inherited the tightest channel's limits.** A guest-agent request size, a Windows
  command-line length and each channel's quoting rules shaped operations that never ran there.
- **Request content was exposed.** Arguments and environment values sat on command lines on the
  workstation and the guest, and environment needed sshd and sudoers configuration on every VM
  ([ADR 0026](0026-environment-travels-inside-the-request.md)).
- **Trust followed the address, not the VM.** Host keys lived in the operator's `known_hosts`, keyed
  by tailnet address and accepted on first use, so a VM whose address changed after a rejoin looked
  like a new host, and any first connection could be trusted silently.
- **Operator configuration leaked in.** The operator's `~/.ssh/config` applied to Agentworks'
  connections, so a `RequestTTY`, `ControlMaster` or proxy setting could change how core's requests
  behaved.
- **Platforms differed in core.** A behavior one platform's channel lacked became a branch in core
  rather than a fix in that platform's provisioning.

## Decision

**One channel.** Every operation core performs on a managed VM after provisioning travels over the
installed OpenSSH client to the VM's tailnet address, through `agentworks.execution`. Core builds no
other `ssh` or `scp` command line. Files, detached work, terminal attachment and forwards are built
on the same request, not on `scp`, per-caller wrappers or channel-specific code.

**The request travels on stdin.** The client's command line carries only policy options, the
destination and a constant first stage. The client then writes, on its own stdin, a small Python
launcher, an envelope holding the argv or script, interpreter, working directory and environment,
and finally the caller's input. The launcher resets what the payload inherits, runs it, and brackets
its output with markers, so completion comes from the markers, never from ssh's exit status, and a
result that cannot be established says so. A terminal attach, whose stdin must be the operator's
terminal, uses a second connection that collects a request parked by the first.

**A fixed client policy.** VM connections run under `-F none`, offer only the configured identity,
run in batch mode, and require OpenSSH 8.5 or newer; on Windows only the native Win32-OpenSSH build
is used. A platform's own SSH to its host (Remote Lima's placement host) is that platform's private
access and may honor the operator's configuration.

**Trust bound to the VM.** Host keys live in an Agentworks store, one record per VM keyed by VM
name, verified strictly on every connection, by Agentworks and by the operator's own `ssh` through
the managed aliases. Only a VM Agentworks is creating has its key accepted on first use, at core's
first connection after provisioning. A cloud's provisioning connection and `vm recover`'s host-key
read also accept a key on first use, under a binding scoped to that one run and never persisted. An
existing VM's key enters the store only by an explicit import or by `vm recover` reading it through
the platform under the operator's confirmation.

**Platforms keep only a root channel.** A platform provisions with whatever native access it needs,
performs the VM's first Tailscale join, and returns the VM's tailnet address. After that it supplies
one capability core uses, `run_as_root`, which runs a core-owned script as root without depending on
Tailscale, for recovery, rekey and the logout at delete, plus an optional interactive native shell.
There is no general native transport; everything else a platform does natively stays private to it.

**One VM contract.** Every VM, on every platform, satisfies one contract that core checks over SSH
at create and reinit: a recognized Debian release, systemd, the admin's passwordless `sudo`, OpenSSH
on the tailnet, `python3` and the base tools the channel uses. A difference between platforms is
fixed in provisioning or initialization, not carried as a branch in core.

## Positives

- **One mechanism to learn and test.** Every caller, core or plugin, uses the same vocabulary, and
  the same conformance vectors run locally, against a loopback sshd and against a live VM.
- **No lowest common denominator.** No size limit, quoting rule or staging scheme exists to serve a
  native channel.
- **Request content stays private.** It appears in no process list on either machine and never
  touches disk.
- **Trust survives address changes** and never widens silently.
- **Recovery has a defined path.** When Tailscale or SSH breaks, `vm recover` repairs it through the
  platform's root channel instead of every operation carrying a fallback.

## Negatives

- **`python3` on every VM.** The launcher is a Python program; a VM without `python3` cannot run any
  request until `vm recover` installs it.
- **A newer client.** OpenSSH 8.5 is required, which Windows 10's inbox client (8.1) does not meet.
- **The operator's SSH configuration does not apply.** An `IdentityAgent` set only there is ignored,
  and an encrypted key must already be loaded in the agent named by `SSH_AUTH_SOCK`.
- **Existing trust needs a one-time import.** VMs created before this decision have no record until
  the operator imports one.
- **A broken sshd can be out of reach.** Where a platform's root channel itself goes through the
  guest's sshd (the clouds' public SSH, Lima), a broken sshd needs provider-native recovery or
  recreation.
- **Four account shells.** sshd runs the constant first stage under the account's login shell, which
  is verified only for `sh`, `dash`, `bash` and `zsh`, so an admin or agent login shell outside
  those is a configuration error.
- **A terminal attach costs two connections.**

## Alternatives considered

- **Keep the native channels as full execution targets.** Rejected: it is the shape described above,
  and building every operation for every channel's limits grew a large stack with no shippable
  increment.
- **Send the request through `SendEnv`.** Rejected: it needed an sshd `AcceptEnv` fragment and a
  sudo `env_keep` line on every VM, numbered parts under a size cap, and a degraded path for VMs
  without the fragment, and it passed secrets through the login shell's environment. Keeping
  `SendEnv` for terminal requests alone would have left a second delivery path for one mode.
- **Relay the terminal through `agw`.** Rejected: ssh takes window size and terminal modes from its
  own stdin, so `agw` would have to relay a pseudo-terminal on three workstation operating systems.
- **A reverse-forwarded socket for the terminal request.** Rejected: it adds a workstation listener
  and a forward that may not be ready when the command starts.
- **A long-lived control channel multiplexing requests.** Rejected: it would bring back the stale
  groups and PAM state that [ADR 0015](0015-abandon-ssh-controlmaster.md) removed connection sharing
  to avoid.
- **The launcher in Bash.** Rejected: exact stdin reads, a rebuilt environment, the handover socket
  and the file programs are simpler and safer in Python's standard library.

## Consequences

- [`cli/agentworks/execution/README.md`](../../cli/agentworks/execution/README.md) describes the
  channel, its outcomes, files, detached work, terminal handover, root scripts and host-key trust.
- [`cli/agentworks/capabilities/vm_platform/README.md`](../../cli/agentworks/capabilities/vm_platform/README.md)
  states what a platform must deliver and implement under vm-platform contract version 2.
- [ADR 0026](0026-environment-travels-inside-the-request.md) records how the environment travels,
  superseding ADRs 0014 and 0017. ADRs 0003, 0011, 0013, 0019,
  [0020](0020-close-ssh-stdin-instead-of-forcing-a-tty.md) and 0025 carry amendments for the
  mechanisms this decision replaced, and ADRs 0015 and 0016 note where `SetEnv` was replaced.
