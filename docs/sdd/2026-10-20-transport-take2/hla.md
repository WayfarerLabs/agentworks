# Transport Take 2: High-Level Architecture

- Status: Draft for review. Nothing here is implemented.
- Date: 2026-10-31
- Requirements: [frd.md](./frd.md)
- Plan: [plan.md](./plan.md)
- Source baseline: `eee56117c` (`main` after PR #851)
- Input: the saga lead's salvage list for this effort, commit `7f2b706eb`, which is not yet on
  `main`. Facts credited to it below were measured in its sources, the predecessors' live reports.

## Summary

One package, `agentworks.execution`, owns everything core does on a managed VM after provisioning.
It reaches the VM only through the installed OpenSSH client at the VM's tailnet address. The client
runs a constant guest launcher, a short Bash program, and passes the launcher the whole request in
one encoded envelope in the client's own environment: argv or script, environment, working
directory, interpreter and elevation. Buffered and streaming commands, terminal attachment, files,
port forwarding and detached work are all built on that one mechanism. Host keys live in an
Agentworks-owned store bound to the VM.

Platforms stop exposing a general native transport. Each one keeps its native access private and
supplies a single recovery capability that runs a core-owned script as root. Provisioning reports
the tailnet address. Core's first connection to a new VM enrolls its host key and checks the VM
contract.

```text
 capability stages              core managers (vms, sessions, workspaces, agents, backup)
        |                                         |
  RunContext.admin_access()                execution.vm_access(vm, config, identity)
  RunContext.agent_access()                       |
        +-------------------+---------------------+
                            |
                      VMAccess (passive)
        run / stream / attach / forward / detached / files
                            |
         request envelope in the client's environment (SendEnv)
                            |
     ssh client process: resolved executable, VM policy, VM-bound trust
                            |
            installed OpenSSH  ==tailnet==>  sshd  ->  guest launcher (bash)

 platform plugins: private native access + run_recovery(script, stdin) + optional native shell
 shared SSH machinery for platforms: execution.ssh_process (host policy or VM policy)
```

## Design principles

- **One mechanism.** Files, detached work and terminals are built from the same guest request, not
  from scp, per-caller wrappers or channel-specific code. A need that cannot be built from it goes
  back to the lead as a design question.
- **No lowest common denominator.** No size limit, quoting rule or staging scheme exists to serve a
  native channel. The FRD names that as the cause of the predecessor's growth.
- **Build for callers that exist.** Each operation below names its current consumers and lands in
  the slice that migrates them. General jobs and cancellation stay deferred (R6).
- **Passive until run.** Constructing access objects resolves nothing and connects to nothing (R9).

## Components

| Module                                   | Responsibility                                                                                                                                    | Adopted from                                                      |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| `execution/client.py`                    | Resolve the OpenSSH executable once to an absolute path, read its version and build, refuse unsupported builds before dispatch                    | `carriers/ssh/client.py` version probe                            |
| `execution/ssh_process.py`               | Build a client argv from a policy (VM or host) and run it: stdin source, stdout and stderr pump, local deadline. The only place that spawns `ssh` | `carriers/ssh/connection.py`, `_io.py`                            |
| `execution/trust.py`                     | The VM-bound host-key store: lookup, enrollment, import, re-establishment, removal                                                                | new                                                               |
| `execution/envelope.py`                  | Encode a request into one value; frame and parse the completion trailer                                                                           | `preparation.py`, adapted                                         |
| `execution/guest_launcher.py`            | The constant Bash program that runs on the guest                                                                                                  | `bootstrap.py`, adapted                                           |
| `execution/requests.py`, `results.py`    | The caller vocabulary and typed outcomes                                                                                                          | `preparation.py` types; `carrier.py` ideas                        |
| `execution/errors.py`                    | Typed failures, each subclassing the matching `agentworks.errors` kind                                                                            | new                                                               |
| `execution/access.py`                    | `VMAccess`: identity-bound, passive entry point holding `run`, `stream`, `attach` and `forward`, plus `files` and `detached`                      | new                                                               |
| `execution/files.py`                     | File operations built on `run` (D6)                                                                                                               | semantics from `native_files.py` callers                          |
| `execution/detached.py`                  | Launch, observe and stop work that outlives its connection (D7)                                                                                   | `remote_exec.py` wrapper idea, copied                             |
| `execution/attach.py`, `forwarding.py`   | Terminal attach under `agentworks.terminal.guarded_terminal`; port forwards with an explicit lifetime                                             | `transports/base.py`, `vms/manager/tailscale.py`                  |
| `execution/reachability.py`              | Wait until a VM answers over SSH, with the operator-facing wait wording                                                                           | `transports.wait_for_reconnect`                                   |
| `execution/oplog.py`                     | Per-VM operation log: label, request shape, outcome; never payloads                                                                               | `SSHLogger` file layout                                           |
| `capabilities/vm_platform/public_ssh.py` | The cloud platforms' SSH to a VM's public address, for provisioning and recovery, composed from `ssh_process`                                     | cloud `SSHTransport` uses                                         |
| `vms/contract.py`, `vms/recovery.py`     | The VM contract checks; core's recovery scripts and `vm recover`                                                                                  | new; scripts from `vms/initializer/credentials.py` and `power.py` |

`agentworks.terminal` is not one of the six legacy modules, so attach keeps using its guard. The
proof modules that do not fit are deleted when the package is adopted: `carrier.py`'s generic
carrier protocol, `carriers/proxmox.py` and `carriers/_proxmox_http.py` (R10).

## Decisions

### D1. OpenSSH client resolution (R8)

The resolver runs once per process and yields one absolute path. Every later spawn and version check
uses that path; the argv builder takes it as a required argument, so no caller can launch a bare
name. The salvage message measured both hazards this guards against: on Windows a bare `ssh`
resolves from the current directory first, and an empty `PATH` element resolves to the current
directory.

- **POSIX** resolves `ssh` along `PATH`, skipping empty and relative elements.
- **Windows** never searches `PATH`, because `PATH` commonly finds the MSYS build bundled with Git
  for Windows, whose argv handling corrupts every invocation (measured). It tries
  `%SystemRoot%\System32\OpenSSH\ssh.exe`, then `%ProgramFiles%\OpenSSH\ssh.exe` (the Win32-OpenSSH
  MSI install). It accepts a build only when its `ssh -V` banner is the `OpenSSH_for_Windows_` form.
- **Every workstation** requires 8.5 or newer. A missing, unsupported or too-old client gets a typed
  `ClientUnsupported` refusal before credentials are resolved. The refusal names the paths tried and
  how to install a supported client.
- **Windows 10.** Its inbox client is 8.1 (measured, per the salvage message), so a Windows 10
  workstation needs the MSI install. The refusal says so.
- **Doctor.** `agw doctor`'s existing check, which looks for `ssh` and `scp` on `PATH`, becomes this
  resolver's verdict, since scp is no longer used.

### D2. Policies and request delivery (R4, R8)

`ssh_process` builds every client argv from one of two policies.

- **VM policy**, for every connection the transport makes to a VM:
  - **Options.** `-F none`, so no operator or system configuration applies, plus explicit `-o`
    options adopted from the proof's list. These include `BatchMode`, connection sharing off per ADR
    0015, agent and X11 forwarding off, no proxy commands, `ConnectionAttempts=1`, an explicit
    `ConnectTimeout`, and the 8.5 `CertificateFile` workaround.
  - **Identity.** `IdentityFile` is `operator.ssh_private_key` with `IdentitiesOnly=yes`. The
    default agent stays enabled, so an encrypted key can be served by it, but only that identity is
    offered.
  - **Trust.** The VM's store options (D9).
  - **Keepalives.** `ServerAliveInterval` and `ServerAliveCountMax`, which the current stack also
    applies, on streaming, attach and forward.
- **Host policy**, for platforms' own access to a host or to a VM's public address. The platform
  chooses whether operator configuration applies and supplies the trust options. Remote Lima's host
  connection honors operator configuration, as ruled on 2026-10-23.

**Request delivery.** The encoded request goes into the client process's own environment as
`AGENTWORKS_EXEC_0`, `AGENTWORKS_EXEC_1` and so on, and the client forwards them with
`-o SendEnv=AGENTWORKS_EXEC_*`.

- **Why the client's environment.** A process's environment is readable only by its own user and
  administrators, unlike its arguments, so environment values, script content and secrets never
  reach any command line. Nothing touches disk.
- **Conflicts.** The variables are set only in the child's environment, which first drops any
  matching names the operator's shell happened to hold.
- **What reaches the command line.** The client's arguments are the policy options, `-T`, `-tt` or
  `-N -L`, the destination and the constant launcher command (D4).

### D3. The request envelope (R3, R4)

The envelope holds a version tag, a random completion nonce, the elevation choice, the interpreter
and startup flags, the literal argv or script source, the environment and the working directory.
Each field is base64-encoded, and the fields are joined with `.`, which is outside the base64
alphabet and needs no quoting. The resulting token is cut into parts of at most 30,000 bytes, one
per `AGENTWORKS_EXEC_<n>` variable, and the launcher joins them in order.

- **Why `SendEnv`.** It works in every mode, including a TTY attach, where stdin is the terminal and
  cannot carry a header, and it leaves stdin wholly to the caller, the separation R3 asks for.
- **Why one encoded token.** OpenSSH silently truncates a `SetEnv` value at a newline (measured on
  9.2p1), and the current stack's coalesced `SetEnv` is first-value-wins (ADR 0014). An encoded
  token in numbered parts avoids both.
- **Size.** Each part stays below Windows' 32,767-character limit for one environment variable and
  Linux's 131,072-byte limit for one environment string. The whole envelope is bounded at 256 KiB,
  and a larger request gets a typed refusal before dispatch. The largest program that travels in a
  request today is the native probe (`artifacts/native/probe.py`, 14,391 bytes, about 19,200
  encoded), which a single part would barely hold, hence the parts. Bulk data travels on stdin,
  never in the envelope. The complexity review measured a 131,044-byte value arriving intact by
  `SendEnv` from a 9.2p1 client; slice 2 proves the multi-part form, including on Windows.
- **Acceptance.** The VM must accept the variable. Today only Phase B writes the
  `50-agentworks-accept-env.conf` fragment (`AcceptEnv *`, `vms/initializer/shell_env.py:397-460`).
  This design adds the same fragment to the shared provisioning bootstrap script, so a brand new VM
  accepts the envelope at core's first connection (D10).
- **When it is missing.** An older VM that never received the fragment reaches the launcher with no
  envelope. The launcher then writes a constant marker, since it has no nonce. The client reports a
  typed `VMNotReady` naming `agw vm reinit` (which still writes the fragment through the legacy
  initializer) until slice 4 adds `agw vm recover`, and recovery afterwards.

### D4. The guest launcher and completion (R3, R4)

**The command line.** The remote command is constant:
`/usr/bin/env -u BASH_ENV -u ENV /bin/bash --noprofile --norc -c '<launcher>'`. It carries no
request data.

**Account shell precondition.** sshd hands that command to the destination account's login shell as
`$SHELL -c`, so the account shell parses one constant line before the launcher runs. Managed
accounts must use a POSIX-family shell (`sh`, `dash`, `bash` or `zsh`). The admin and agent
templates enforce this at the config boundary, and the base contract check verifies this for the
admin and every agent (D10). Before the launcher starts, that shell may read its own non-interactive
startup file: zsh's `.zshenv`, or the `.bashrc` read Debian's bash performs under sshd. The payload
itself runs with no startup files. This is the closest OpenSSH allows to R3's "no shell startup
implicitly"; it is listed for the operator under open questions. A shell outside the family, such as
`tcsh`, can parse the line wrongly and report an ordinary-looking status (measured), which is why
the contract check refuses it rather than leaving it to completion framing.

**What the launcher needs.** The launcher is adapted from the proof's `bootstrap.py`, keeping its
measured handling of locale and private names. It needs Bash 5.1, GNU coreutils, util-linux and
`getent`, which every recognized Debian release (bookworm and trixie, `debian.py`) provides from
provisioning onward. It needs no Python.

**What the launcher does:**

1. **Reads and checks the envelope.** With none, it writes the constant marker and exits.
2. **Elevates when asked.** If the request is elevated and the launcher is not root, it re-executes
   itself under `sudo -n`. The provisioning bootstrap adds
   `Defaults:<admin> env_keep += "AGENTWORKS_EXEC_*"` beside its `NOPASSWD:ALL` line, so the
   variable survives sudo on a VM that Phase B has not reached yet. Phase B's existing
   `env_keep += "AGENTWORKS_*"` covers older VMs. Slice 3, the first to elevate before Phase B,
   proves this on a fresh VM.
3. **Clears the variable**, then applies the environment and the working directory.
4. **Runs the payload as a child.** A literal argv runs with no shell. A script runs under the
   chosen interpreter, which reads the source from a file descriptor and never from its arguments
   (R4). Login or interactive startup applies only when requested. `sh` and `bash` are fixed paths.
   The default shell is the destination identity's `getent passwd` entry. A missing interpreter
   fails rather than being substituted.
5. **Reports completion.** Except in TTY mode, after the payload exits the launcher writes one
   framed trailer to stderr: `<nonce> exit <status>`, or `<nonce> refused <reason>` for its own
   failures. In TTY mode it execs the payload instead.

**Completion rules:**

- **The trailer is the only completion evidence.** The client strips it, so a guest exit of 255 is
  reported as completed with status 255.
- **No trailer means unknown,** whatever the client's own exit status.
- **Not delivered.** A client status of 255 with no trailer counts as not delivered only when the
  launcher provably never started: OpenSSH's own refusal of the connection or authentication.
  Anything else is unknown.
- **TTY mode.** An attach returns its exit code; a 255 is reported as "connection ended or command
  exited 255", never guessed.
- **Late output.** Background children can write to stderr after the trailer. The client takes the
  last well-formed trailer bearing the nonce and keeps later bytes as stderr.
- **Forgery.** The nonce leaves the payload's environment before it starts, so ordinary payload
  output does not forge a trailer. Another process of the same user that reads the launcher's
  `/proc/<pid>/environ` could. That is outside R4's threat model, which concerns honesty, not a
  hostile same-user process.

### D5. Requests and results (R3, R4)

| Type                   | Shape                                                                                                             | Current consumers                                                                         |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `Command`              | Literal argv tuple                                                                                                | most buffered calls                                                                       |
| `Script`               | Source plus `Shell.SH`, `Shell.BASH` or `Shell.USER_DEFAULT`, with `login` and `interactive` flags                | initializers, file operations, native probing                                             |
| Common options         | `env`, `cwd`, `elevated`, `deadline`, `observation`                                                               | `observation`: status probes that retry today                                             |
| Input                  | `NoInput`, `Bytes(data, sensitive=)`, `FromFile(path)`                                                            | `FromFile`: workspace copy (slice 7)                                                      |
| Output sink (buffered) | `Capture(limit)`, `Discard`, `ToFile(path, on_progress=)`                                                         | `Discard`: `command_checks.py`, native CLI; `ToFile`: backup and workspace copy (slice 7) |
| `Result`               | `completion` is `Completed(status)`, `NotDelivered(reason)` or `Unknown(reason)`; `stdout` and `stderr` are bytes | all                                                                                       |

Each vocabulary item lands in the slice that migrates its first consumer.

- **Bytes, not text.** Results carry bytes with no newline conversion; a `text()` helper decodes.
  Stdin is written in binary mode on every workstation (incident row I2).
- **Sensitive inputs.** Sensitive input, environment values and script content never appear in logs,
  exceptions or retained artifacts. A secret-bearing environment does not suppress output (R4). Only
  an explicitly sensitive stdin marks the streams as not retained in the operation log.
- **Deadlines.** A deadline bounds the local wait, kills the local client and returns `Unknown`. It
  never claims remote termination; the predecessors measured guest trees outliving the deadline, and
  cancellation stays deferred.
- **Retries.** Only `observation=True` requests may retry, and only after `NotDelivered` or
  `Unknown`. Mutating requests are never re-dispatched.
- **`check=True`** turns anything but `Completed(0)` into a typed error carrying the `Result`.

### D6. Files built on execution (R5)

Every file operation is a `run` of a short core-owned `sh` script, with content on stdin or stdout.
There is no scp and no Python on the guest.

| Operation                                   | Mechanism                                                                                                                           | Current consumers (slice)                                    |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| `read(path) -> (bytes, digest)`             | `cat`; the workstation computes the digest from the bytes it received, so the pair always matches                                   | artifacts, native CLI transcripts, settings (5)              |
| `write(path, data, mode, owner, group)`     | Under the lock: `mktemp` in the destination directory, content from stdin, `chown` and `chmod` on the temp file, `sync` it, `mv -f` | initializer fragments, profiles, tmuxinator, sources (5, 6)  |
| `write_if(path, data, expected, ...)`       | As `write`, after comparing the current digest (or absence) with `expected` under the lock                                          | artifact publication, settings merges (5)                    |
| `update(path, transform, ...)`              | `read`, apply `transform` on the workstation, `write_if`; retry on conflict                                                         | harness JSON and TOML settings, marked profile blocks (5, 6) |
| `remove(path)`, `remove_if(path, expected)` | Under the lock                                                                                                                      | artifact cleanup (5), workspaces (7)                         |
| `ensure_directory`, `prune_empty_parents`   | Short scripts                                                                                                                       | artifacts (5), workspaces (7)                                |
| `upload_tree(local_dir, dest, replace=)`    | A `tarfile` stream from the workstation into `tar -x -C dest`; `replace` clears `dest` first, as `copy_dir_to` does today           | sources' `fetch_dir` (6)                                     |

- **One lock for every writer.** Every mutating operation takes `flock` on the destination
  directory, so `write_if` is atomic with respect to every Agentworks writer, which is R5's
  cooperating-writer guarantee. Hostile path races stay with the permissions effort.
- **Symlinks.** A destination that is a symlink is refused, as `native_files.py` refuses today,
  rather than followed as the hand-written `tee` paths do.
- **The merge stays on the workstation.** `update` runs
  `capabilities/harness_integration/settings.py` locally. That module already keeps JSON null as a
  value and rejects malformed input, so R5's merge rules hold by construction, and TOML merges get
  the same conditional write.
- **Opaque archives.** Workspace copy moves an opaque tar stream with `run`, `ToFile` and
  `FromFile`, and never extracts on the workstation, so modes, owners and symlinks survive a Windows
  workstation.
- **Elevation and identity.** Elevated placement is the request's `elevated` flag, so callers write
  no copy, chmod or sudo wrappers. Agent writes use the agent's `VMAccess`.
- **Failures.** A failure removes only the temp file the operation created.
- **python3.** The guest still needs python3 for the consumers that run Python programs today
  (`artifacts/native/probe.py`, the native CLI's login resolution). That keeps python3 in the
  initialized contract.

### D7. Detached work (R6)

The only current core caller is VM backup. Remote Lima's host-side detached create is
platform-private (D11).

- **Launch.** `detached.launch(request) -> LaunchId` creates a fresh `mktemp -d` directory under a
  base owned by the request's identity:
  - `/var/lib/agentworks/detached`, root-owned 0700, for elevated launches;
  - `~/.local/state/agentworks/detached` for ordinary ones.

  It then starts a wrapper with `setsid nohup` that records the pid, output and exit status there,
  and returns at once. No world-writable path is used. The id names the directory, so an independent
  new launch never matches an earlier one.

- **Observe.** `detached.observe(id, offset)` is an observation request returning new output, the
  state and the status. Backup uses it across dropped connections within one invocation. A later
  invocation can do the same with the recorded id (R6).
- **Stop.** `detached.stop(id)` signals the process group. Backup calls it from its interrupt
  handler, as today.
- **Discard.** `detached.discard(id)` removes the directory once the caller is done.
- **Holds.** The caller records the id and owns the VM availability hold (the existing activation
  gate, including the WSL2 keepalive). The transport never starts or holds a VM.

### D8. Streaming, attach, forwarding and reachability (R2, R4)

- **`stream(request)`** inherits the operator's stdin and stdout, and pumps stderr through the
  trailer filter with a short hold-back window. Consumers: `vm exec`, `agent exec`.
- **`attach(request, clear_screen_on_exit=)`** runs with `-tt` under `guarded_terminal`, keeping the
  post-guard notice for a dropped connection and the Windows clear behavior. Consumers: session and
  console attach, `vm shell`, `agent shell`.
- **`forward(local, remote)`** is a context manager running `ssh -N -L` under the VM policy, minus
  the forwarding restrictions, with `ExitOnForwardFailure=yes`. It returns once the local port
  accepts a connection, raises a typed error with the client's diagnostic if the client exits first,
  and ends the client on exit. Consumer: `vm port-forward`.
- **`reachability.wait(access, ...)`** polls an observation request until it completes, with today's
  wait wording and flap double-check. Consumers: create's post-Tailscale step, start, rejoin and
  rekey.

### D9. The VM-bound host-key store (R8)

**Trust modes.** There are three, and the code that builds a connection chooses one explicitly:

| Mode       | Used by                                                                                         | Options                                                                                                                   |
| ---------- | ----------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| VM         | every connection to a VM, at its tailnet or public address                                      | the VM's store file and alias, `StrictHostKeyChecking=yes`                                                                |
| Enroll     | create's first core connection only                                                             | the VM's freshly reset store file and alias, `StrictHostKeyChecking=accept-new`                                           |
| Unverified | cloud provisioning before the VM is in the store; a cloud host-key read when the record is lost | `UserKnownHostsFile=/dev/null` (`NUL` on Windows), `StrictHostKeyChecking=no`. First-use trust, as provisioning has today |

Host connections such as Remote Lima's keep their own configuration and are not VM trust at all.

- **Layout.** One file per VM, at `~/.config/agentworks/known_hosts/<vm>`, in `known_hosts` format,
  with records under a host-key alias derived from the VM name. Every store connection sets
  `GlobalKnownHostsFile=none`, `UpdateHostKeys=no` and `HashKnownHosts=no`.
- **Identity.** The VM name keys `vms` (`db/migrations.py:240`) and survives every tailnet address
  change. Delete removes the store file, and create resets it, so a reused name never inherits a
  key.
- **Lookup.** A missing file is a typed `TrustMissing` naming the import and recovery commands. It
  never falls back to first use, so a missing record never makes an existing VM new.
- **Enrollment.** Create resets the VM's store file and makes its first core connection in Enroll
  mode. That connection runs the base contract check (D10). The store is the only place the key is
  written. If create is interrupted after the reset, the VM is retained as failed, and
  re-establishment (below) is its recovery path, so no state is left without one.
- **Import.** `agw vm import-host-keys (--all | VM...) [--known-hosts PATH]`:
  - Reads the default `~/.ssh/known_hosts`, or the named file, without modifying it.
  - Matches each VM's recorded tailnet address against each record's host field using OpenSSH's
    pattern rules (wildcards, negation, `[host]:port`, comma lists, and `|1|` hashed entries), and
    copies matching key records into that VM's store file.
  - Reports an `@cert-authority` or `@revoked` marker whose pattern matches a VM, and imports
    nothing for that VM.
  - Never replaces an existing store record with a different key.
- **Re-establishment.** This is part of `agw vm recover` (D11). It calls the platform's
  `read_host_keys`, shows the fingerprints, and writes them to the store only after the operator
  confirms.
  - **Default implementation.** `run_recovery` with a fixed script that prints the public host keys.
    Its channel does not use the VM's store: WSL2's `wsl.exe`, Proxmox's guest agent, and Lima's
    `limactl shell` over Lima's own host-local access.
  - **Clouds.** They override it. They read the keys in Unverified mode and also return the
    provider-side fingerprints they can obtain (for example EC2 console output or GCE guest
    attributes) for the operator to compare. The platform LLD settles the source per provider.
  - **Limits.** Unverified mode is used for nothing else after provisioning. Every `run_recovery`
    call is VM trust only.
- **Managed SSH entries.** Once a VM has a store record, `ssh_config.py` adds `HostKeyAlias`,
  `UserKnownHostsFile` (the store file) and `StrictHostKeyChecking yes` to its managed VM and agent
  entries, so the operator's own `ssh` verifies against the store. Entries for a VM without a record
  stay as they are today, so an operator's own `ssh` keeps working through the upgrade until they
  import. Slice 8 makes the store options unconditional, so R8's end state does not depend on every
  operator importing.
- **Legacy paths during migration.** Slice 1 gives the legacy VM-target factories an explicit trust
  binding in VM or Enroll mode. These are `transport()`, `agent_transport()`,
  `transport_for_user()`, Phase A's builder, the backup scp, the workspace-copy ssh, the port
  forward and the clouds' public-address native transports (`aws/platform.py:718`,
  `azure/platform.py:824`, `gcp/platform.py:591`), which reach the same sshd. Host targets are
  unchanged, and cloud provisioning moves to Unverified mode. This edits legacy code in the
  direction R10 allows: legacy code calls new code, never the reverse.

### D10. The VM contract and its checks (R1)

`vms/contract.py` holds one table of read-only checks. Each check carries a phase tag, `base` or
`initialized`, and the table renders one structured report naming each failed item and its fix.

- **Base checks** run on core's first connection at create, and first at reinit:
  - a recognized Debian release, under ADR 0025's policy and recording;
  - systemd as PID 1;
  - the admin account and `sudo -n true`;
  - POSIX-family login shells for the admin and every agent;
  - Tailscale up at the recorded address;
  - the envelope accepted, which the check's own request proves;
  - Bash 5.1 or newer, coreutils, util-linux, `tar` and `getent`.

  Because this is core's first operation, the release check is the first thing core does on the
  common path.

- **Initialized checks** run at the end of Phase B: python3 and the `INIT_SYSTEM_PACKAGES` tools.
- **Diagnosis.** A failure reinit can fix leaves the VM not ready and names reinit. A failure reinit
  cannot fix (an unrecognized release, no systemd) names recreation.
- **Provisioning additions.** The shared provisioning bootstrap script gains the AcceptEnv fragment
  and the envelope `env_keep` line. Phase B keeps writing both, so reinit converges older VMs whose
  envelope is accepted, and recovery covers those whose envelope is not.

### D11. The platform surface (R1, R2, R7)

Changes to `capabilities/vm_platform/base.py`:

| Today                                         | After                                                                                                                                                                                                                                                                            |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ProvisionResult.native_transport` (required) | Removed in slice 3, when Phase A and Debian attestation move onto SSH                                                                                                                                                                                                            |
| `ProvisionResult.tailscale_ip: str \| None`   | `tailnet_address: str`, required. A platform that cannot read it fails create instead of leaving core a native fallback                                                                                                                                                          |
| `native_transport(vm, ctx, config)`           | Removed in slice 4, when rejoin, rekey, logout and the native shell move                                                                                                                                                                                                         |
| (none)                                        | `run_recovery(vm, ctx, script, stdin, deadline) -> RecoveryResult`: root, once, no Tailscale dependency, VM trust only                                                                                                                                                           |
| (none)                                        | `read_host_keys(vm, ctx) -> HostKeyReading`: the VM's public host keys, plus any provider-side fingerprints. The default runs a fixed script through `run_recovery`; the clouds override it                                                                                      |
| (none)                                        | `ClassVar recovery_needs_guest_sshd: bool`                                                                                                                                                                                                                                       |
| `native_shell_unavailable_hint`               | `native_shell(vm, ctx) -> int`. A platform offers a shell by overriding it. Core detects the override on the class before the operation boundary, so it refuses with `FeatureUnavailable` before credentials are resolved (R2). A platform may set guidance text for the refusal |
| `transient_route`, `probe_failure_hint`       | Private to each platform's own native code (recovery and native shell)                                                                                                                                                                                                           |

**RecoveryResult.** `run_recovery` wraps the core script in a small `sh` wrapper that writes the
same nonce trailer as D4, so a recovery result is the script's status, delivery failure, or unknown.
It never relies on `limactl` or `wsl.exe` exit codes; the salvage message measured WSL passing raw
wait statuses through, which makes a signal and an exit indistinguishable. Sensitive stdin follows
D5. The capability takes no environment, directory or files.

**Per platform:**

| Platform        | Recovery channel                                                  | Needs guest sshd | Native shell | Private access after migration                                                                 |
| --------------- | ----------------------------------------------------------------- | ---------------- | ------------ | ---------------------------------------------------------------------------------------------- |
| Lima, local     | `limactl shell`                                                   | yes              | yes          | `limactl` wrappers, copied from `transports/lima.py`                                           |
| Remote Lima     | host SSH, then `limactl shell`                                    | yes              | yes          | host connection under the host policy; host-side detached create, copied from `remote_exec.py` |
| WSL2            | `wsl.exe --user root`                                             | no               | yes          | `wsl.exe` wrappers and the keepalive                                                           |
| Proxmox         | QEMU guest agent                                                  | no               | no           | QGA client as `plugins/proxmox/guest_agent.py`, replacing `plugins/proxmox/transport.py`       |
| AWS, Azure, GCP | `public_ssh.py` to the public address, inside the transient route | yes              | yes          | provisioning and lost-record key reads under Unverified trust; recovery under VM trust         |

- **Cloud public-address SSH.** The public address reaches the same sshd, with the same host key, as
  the tailnet, so cloud recovery verifies against the VM's store entry. It runs
  `sudo -n /bin/sh -c '<wrapped script>'`. The script is core-owned and secret-free; sensitive input
  goes on stdin. No envelope is involved, so a VM whose sshd no longer accepts the envelope can
  still be recovered.
- **Core's recovery scripts** live in `vms/recovery.py`:
  - **Rejoin** is today's `rejoin_tailscale`. The activation gate still runs it, announced, when a
    VM has lost its tailnet registration; see the open questions.
  - **Rekey** is `vm rekey`, verified over SSH afterwards, where access returns.
  - **Logout** runs at delete. Its result is the script's own status, or unknown if the connection
    drops first.
  - **Access repair** restores the admin authorized key, re-applies the Agentworks sshd fragments
    (AcceptEnv and `env_keep`) and reloads sshd on every platform, and prints the public host keys.
    Where the platform's channel does not need sshd, it also repairs and restarts sshd.
- **The operator entry point** is the new `agw vm recover <vm>`. It first offers host-key
  re-establishment (D9) when the VM has no store record, since every other step needs trust. It then
  runs access repair, rejoins if needed, and verifies over SSH. When the channel needs the guest's
  sshd and sshd is broken, or recovery fails, it stops and tells the operator to repair or recreate
  the machine (R7).
- **Debian attestation at create** moves from the native transport to the base checks (D10). The
  Phase A `tailscale ip -4` fallback goes away with the required `tailnet_address` (R10).

### D12. Delivery to consumers (R9)

- **The access object.** `VMAccess` is built from a VM row, the config and an identity, `Admin` or
  `Agent(agent)`. Construction copies names and paths and resolves nothing. The client, trust file,
  secrets and route are resolved when an operation runs.
- **Capability accessors.** `RunContext` gains `admin_access()` and `agent_access()`, returning
  `VMAccess | None`, beside `admin_target()` and `agent_target()` until the last capability consumer
  moves.
- **Core managers** call `execution.vm_access(vm, config, identity)` directly, without a
  `RunContext`.
- **Agent identity.** An agent-bound `VMAccess` connects as the agent's Linux user (ADR 0011). It
  refuses an elevated request before dispatch with a typed `PermissionDenied`. The guest's `sudo -n`
  remains a second layer, so an agent placed in a sudo group by hand still never gains admin
  authority through the transport.
- **Tests.** Manager tests use a recording fake `VMAccess` returning canned typed results, so they
  run on every CI host. The real launcher runs only in the conformance suite, against a local Bash
  on Linux and against live targets.

### D13. Errors and the operation log

`execution/errors.py` subclasses the matching `agentworks.errors` kinds:

| Error                                        | Kind                 |
| -------------------------------------------- | -------------------- |
| `ExecutionIncomplete` (carries the `Result`) | `ConnectivityError`  |
| `CommandFailed` (carries the `Result`)       | `ExternalError`      |
| `PermissionDenied`                           | `AuthorizationError` |
| `FeatureUnavailable`, `VMNotReady`           | `StateError`         |
| `RequestTooLarge`                            | `ValidationError`    |
| `TrustMissing`, `TrustMismatch`              | `StateError`         |
| `ClientUnsupported`                          | `ConfigError`        |

`ExecutionIncomplete` covers both `NotDelivered` and `Unknown` completions and names which one, so
the facts are held once.

`execution/oplog.py` writes the per-VM operation log under `~/.config/agentworks/logs/`, as today.
It records a label, the request shape and the outcome, and never payloads, so it needs no redaction
table. The readers of the old `LOG_DIR` constant (`doctor_state.py`, `vms/manager/lifecycle.py`,
`vms/manager/_helpers.py`, `vm logs`) move to the new constant in slice 2.

### D14. Enforcing independence (R10)

- **The package guard.** The existing fresh-interpreter guard in
  `cli/tests/execution/test_independence.py` stays. It walks every `agentworks.execution` submodule
  with `pkgutil.walk_packages`, so new modules are covered automatically. `vms/contract.py` joins it
  in slice 3. `public_ssh.py` and `vms/recovery.py` join it in slice 4, once the rehome leaves
  `capabilities/vm_platform/__init__.py` importing no legacy code (today it reaches `lima.py` and
  `wsl2.py`, which do).
- **The test guard.** A conftest under `cli/tests/execution/` blocks the six legacy modules for the
  whole directory, so the package's tests pass with them unavailable, as R10 requires.
- **Migrated consumers.** These cannot pass an import guard until the end, because most of the tree
  imports legacy modules transitively. Each slice instead adds its migrated files to a static check
  that rejects any direct import of a legacy module, top-level or inside a function. Deleting the
  modules in slice 8 proves the rest.

## Migration

The plan sequences eight slices. Each is complete on its own terms, and each carries live evidence
for what it touches (R10, R11).

1. **Trust.** The store, explicit trust modes, import, create's enrollment, managed entries, and the
   legacy VM-target factories bound to the store.
2. **Execution core with the operator exec surface.** Client resolution, `ssh_process`, envelope,
   launcher, results, errors and the operation log. Consumers: `vm exec`, `vm shell`, `agent exec`,
   `agent shell`, `vm verify-connection`, release probing and `vm port-forward`.
3. **First contact and the VM contract.**
   - Platforms report the required tailnet address.
   - Provisioning writes the AcceptEnv and `env_keep` lines.
   - Phase A moves onto `VMAccess`, in Enroll mode at create, with the base checks and Debian
     attestation; the initialized checks land too.
   - `ProvisionResult.native_transport` goes away, and `reachability` lands for create.

   Phase B stays on the legacy stack until slice 6.

4. **Recovery and the platform surface.**
   - `run_recovery` on every platform, with `public_ssh.py`.
   - Each platform's native code rehomed as private modules: Remote Lima's host connection and
     host-side detached create, the Proxmox QGA client, and the WSL2 and Lima wrappers.
   - Rejoin, rekey and logout move onto recovery, and `native_shell` lands.
   - `vm recover` is added, and `native_transport` and its factory are deleted.
5. **Files and capability delivery.** `files.py` and the `RunContext` accessors. Consumers: harness,
   artifact, native-file and git-credential (scout batches 8 and 9).
6. **Initialization.** VM and agent initializers, hardening, DNS, sources and the setup runners.
7. **Workspaces, sessions and backup.** Workspace backends, copy, rehome and repair; tmux, sessions
   and consoles; `detached.py` with backup.
8. **Removal.** Delete the six legacy modules, `admin_target()` and `agent_target()`, the remaining
   proof modules and their tests, after the incident inventory is re-proved and the evidence matrix
   is complete.

### Incident inventory (R10)

Each row is re-proved in the slice that moves the code carrying it, and the evidence is recorded in
the plan. Rows I1 to I3 come from the transport-improv migration strategy; the rest were added by
scouting.

| Row | Behavior                                                                                                                | Re-proved in |
| --- | ----------------------------------------------------------------------------------------------------------------------- | ------------ |
| I1  | Closed stdin instead of a forced TTY on Windows (ADR 0020)                                                              | 2            |
| I2  | Byte-exact stdin on Windows (text-mode rewrite)                                                                         | 2            |
| I3  | Guest mechanics tested on real targets, not Git for Windows tools                                                       | 2            |
| I4  | Sensitive input refused with a forced TTY; value-free errors                                                            | 2            |
| I5  | The `--` fence for dash-led commands, now inherent in literal argv                                                      | 2            |
| I6  | The attach notice after the terminal guard                                                                              | 2            |
| I6a | Console recovery after a killed `ssh -t`                                                                                | 7            |
| I7  | Keepalives on long-lived paths                                                                                          | 2            |
| I8  | `nohup` outside `sudo` is replaced by whole-operation elevation plus `setsid`; the output-capturing brace group is kept | 7            |
| I9  | Host-key preservation across stop and start                                                                             | 1            |

## Evidence matrix (R11)

Code-supported pairings, from each platform's `unsupported_reason` and readiness checks:

| Platform                 | Linux | macOS         | Windows                 |
| ------------------------ | ----- | ------------- | ----------------------- |
| Lima, local              | yes   | yes           | pending operator ruling |
| Remote Lima (macOS host) | yes   | yes (primary) | yes                     |
| WSL2                     | no    | no            | yes                     |
| Proxmox                  | yes   | yes           | yes                     |
| AWS                      | yes   | yes           | yes                     |
| Azure                    | yes   | yes           | yes                     |
| GCP                      | yes   | yes           | yes                     |

That is 19 cells. Lima defines no `unsupported_reason`, so local Lima on Windows is code-supported
whenever `limactl` is installed. Whether it is an Agentworks-supported pairing is an open question.
Each slice names the cells it covers. Removal needs every cell, or the operator's disposition of it.

## ADR amendments

| ADR                               | Change                                                                                                                    | Slice |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------- | ----- |
| 0013 (secret injection)           | Secret environment travels in the envelope, not as `-o SetEnv` arguments                                                  | 2     |
| 0014 (`AcceptEnv` wildcard)       | Environment travels in one encoded variable by `SendEnv`; the newline limit is gone; provisioning writes the fragment too | 2     |
| 0017 (console pane environment)   | The console's environment channel changes from `tmux -e` arguments to the request environment                             | 7     |
| 0019 (command plans)              | Its `admin_target`/`agent_target` description moves to the new accessors                                                  | 5     |
| 0025 (one current Debian release) | Core attests the release over SSH as the first base check                                                                 | 3     |

ADR 0003 needs no change: its statement that access after bootstrap is SSH over Tailscale becomes
exactly true.

## Risks and early proofs

| Risk                                                        | Proof, and its slice                                              |
| ----------------------------------------------------------- | ----------------------------------------------------------------- |
| `-F none` and `SendEnv` on 8.5 and on Win32-OpenSSH         | Envelope round trip at the bound, slice 2, all three workstations |
| `env_keep` for the envelope on a VM Phase B has not reached | Elevated request on a fresh Debian 12 and 13 VM, slice 3          |
| The trailer with a background child holding stderr          | Conformance vector, slice 2                                       |
| `flock` on a directory across Debian releases               | Conformance vector, slice 5                                       |
| Encrypted keys served by the agent                          | Live cell, slice 1                                                |
| Older VMs without the AcceptEnv fragment                    | Reinit path in slice 2, recovery path in slice 4                  |
| Cloud host-key re-establishment                             | Lost-record recovery on one cloud, slice 4                        |
| `UserKnownHostsFile=NUL` with Win32-OpenSSH                 | Cloud create from Windows, slice 1                                |

## Open questions for the operator

1. **Command names.** `agw vm import-host-keys` and `agw vm recover` are new operator-facing
   commands. The FRD requires their behavior, not their names.
2. **Account shell startup (R3).** The account's own shell parses the constant launcher line, so
   zsh's `.zshenv` or Debian bash's sshd-triggered `.bashrc` can run before the launcher. The
   payload gets no startup files. The lead's read: this satisfies R3's intent, because no startup
   applies to what the caller asked to run. The alternative is restricting managed accounts to `sh`
   and `dash` login shells, which would break zsh admins. Either way the admin and agent template
   `shell` fields, free strings today (`vms/admin.py:99`, `agents/templates.py:33`), become limited
   to the POSIX family at the config boundary. That is a new restriction on operator-authored
   config, with the base check kept as a backstop.
3. **Automatic rejoin (R7).** R7 says core invokes recovery only explicitly. The lead reads
   "explicitly" as a named core flow that announces what it is doing, as opposed to the transport
   silently falling back. On that reading, the activation gate's announced rejoin of a VM that lost
   its tailnet registration stays. The alternative is to fail and name `agw vm recover`.
4. **Local Lima on Windows.** Is it a supported pairing for R11's matrix?

## Requirements traceability

| Requirement | Decisions                          |
| ----------- | ---------------------------------- |
| R1          | D10, D11                           |
| R2          | D2, D8, D11                        |
| R3          | D3, D4, D5                         |
| R4          | D2, D3, D4, D5, D8                 |
| R5          | D6                                 |
| R6          | D7                                 |
| R7          | D11                                |
| R8          | D1, D2, D9, D11                    |
| R9          | D12                                |
| R10         | D14, Migration, incident inventory |
| R11         | Evidence matrix, plan              |
