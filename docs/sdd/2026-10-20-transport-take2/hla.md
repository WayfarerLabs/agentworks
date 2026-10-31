# Transport Take 2: High-Level Architecture

- Status: Draft for review. Nothing here is implemented.
- Date: 2026-10-31
- Requirements: [frd.md](./frd.md)
- Plan: [plan.md](./plan.md)
- Source baseline: `eee56117c` (`main` after PR #851)

## Summary

One package, `agentworks.execution`, owns everything core does on a managed VM after provisioning.
It reaches the VM only through the installed OpenSSH client at the VM's tailnet address. It runs a
constant Bash bootstrap on the guest and hands that bootstrap the whole request (argv, script,
environment, working directory, interpreter and elevation) as one encoded variable. On top of that
one mechanism sit buffered and streaming commands, terminal attachment, files, port forwarding and
detached work. Host keys live in an Agentworks-owned store bound to the VM name.

Platforms stop exposing a general native transport. Each one keeps its native access private and
supplies a single recovery capability that runs a core-owned script as root. Provisioning reports
the tailnet address, and core's first connection to a new VM enrolls its host key and checks the VM
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
                  guest request envelope  -->  constant guest bootstrap (bash)
                            |
              ssh client invocation (policy, per-invocation config, trust)
                            |
            installed OpenSSH  ==tailnet==>  sshd on the VM

 platform plugins: private native access + run_recovery(script, stdin) + optional native shell
```

## Design principles

- **One mechanism.** Files, detached work and terminals are built from the same guest request, not
  from scp, wrappers per caller or channel-specific code. A capability that cannot be built from it
  is a smell to bring back to the lead.
- **SSH only, so no lowest common denominator.** No size limit, quoting rule or staging scheme
  exists to serve a native channel. The FRD's account of the predecessor's growth names that as the
  cause.
- **Build for callers that exist.** Every operation below names its current consumers. Nothing is
  added for an imagined caller; R6's general job API and cancellation stay deferred.
- **Passive until run.** Constructing access objects resolves nothing and connects to nothing (R9).

## Components

| Module (under `cli/agentworks/execution/`) | Responsibility                                                                                                                                               | Adopted from                                  |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------- |
| `client.py`                                | Resolve the OpenSSH executable for the workstation, read its version and build, refuse unsupported builds before dispatch                                    | `carriers/ssh/client.py` version probe        |
| `policy.py`                                | The fixed option set for VM connections and the per-invocation private client config file                                                                    | `carriers/ssh/connection.py` option list      |
| `trust.py`                                 | The VM-bound host-key store: lookup, first-use enrollment, import, re-establishment, removal                                                                 | new                                           |
| `process.py`                               | Spawn the client, pump stdin, stdout and stderr, apply the local deadline, strip the completion trailer                                                      | `carriers/ssh/_io.py`                         |
| `envelope.py`, `bootstrap.py`              | Encode a request into one variable; the constant guest bootstrap that decodes it, elevates, applies environment and directory, execs, and reports completion | `preparation.py`, `bootstrap.py`, adapted     |
| `requests.py`, `results.py`, `errors.py`   | The caller vocabulary (`Command`, `Script`, `Shell`, inputs and sinks) and typed outcomes                                                                    | `preparation.py` types; `carrier.py` ideas    |
| `access.py`                                | `VMAccess`: identity-bound, passive entry point holding `run`, `stream`, `attach`, `forward` and `detached`, plus `files`                                    | new                                           |
| `files.py`                                 | Byte-exact read and write, atomic publish with mode and owner, conditional publish, JSON update, directory trees, removal                                    | new; semantics from `native_files.py` callers |
| `detached.py`                              | Launch, observe and stop work that outlives its connection, by recorded identity                                                                             | `remote_exec.py` wrapper idea, copied         |
| `forwarding.py`                            | Local port forwards with an explicit lifetime and setup-failure reporting                                                                                    | `vms/manager/tailscale.py` behavior           |
| `terminal_attach.py`                       | Interactive attach under `agentworks.terminal.guarded_terminal`, and the post-attach notice for a dropped connection                                         | `transports/base.py` and `transports/ssh.py`  |
| `recovery_ssh.py`                          | Root-script runner over an SSH endpoint without the envelope, for platforms whose native channel is SSH                                                      | new                                           |
| `logging.py`                               | Per-operation log lines with registered-value redaction; never logs request payloads                                                                         | `SSHLogger` behavior, copied                  |

`agentworks.terminal` is not one of the six legacy modules, so attach keeps using its guard.

The proof modules that do not fit are deleted when the package is adopted: `carrier.py`'s generic
carrier protocol, `carriers/proxmox.py` and `carriers/_proxmox_http.py` (R10).

## Decisions

### D1. OpenSSH client resolution (R8)

- **POSIX workstations** use `ssh` from `PATH`.
- **Windows workstations** never search `PATH`, because `PATH` commonly finds the MSYS build bundled
  with Git for Windows. They use `%SystemRoot%\System32\OpenSSH\ssh.exe`, then
  `%ProgramFiles%\OpenSSH\ssh.exe` (the Win32-OpenSSH MSI install), and accept a build only when its
  `ssh -V` banner is the `OpenSSH_for_Windows_` form.
- **Every workstation** requires version 8.5 or newer. A missing, unsupported or too-old client is a
  typed `ClientUnsupported` refusal before credentials are resolved, naming the path it tried and
  how to install a supported client.
- **Caching.** The result is cached per process. `agw doctor` reports the resolved client, its build
  and version.
- **Unverified fact.** The FRD left open whether the Windows 10 inbox client is below 8.5. This
  design does not depend on the answer, because the MSI install is accepted. A Windows 10
  workstation with only an older inbox client is refused with install guidance. The plan's first
  Windows evidence records the inbox version.

### D2. Connection policy and the per-invocation client config (R4, R8)

Every client process for a VM gets `-F <private file>` and nothing from operator or system
configuration. Agentworks writes the file per invocation, mode 0600, in a 0700 directory under
`~/.config/agentworks/run/ssh/`, and removes it when the process exits. A sweep at the start of the
next invocation removes leftovers from an interrupted run. The file holds:

- the fixed policy, adopted from the proof's option list (`BatchMode`, `IdentitiesOnly`, connection
  sharing off per ADR 0015, forwarding off, no proxy commands, `ConnectionAttempts=1`, an explicit
  `ConnectTimeout`, and the 8.5 `CertificateFile` workaround);
- the identity: `IdentityFile` set to `operator.ssh_private_key` with `IdentitiesOnly=yes`. The
  default agent stays enabled, so a key the operator keeps encrypted can be served by the agent;
  only that identity is ever offered;
- trust: `GlobalKnownHostsFile=none`, `UserKnownHostsFile` set to the VM's store file,
  `HostKeyAlias` set to the VM's alias, `StrictHostKeyChecking=yes` (or `accept-new` only for an
  enrollment, D9), `UpdateHostKeys=no` and `HashKnownHosts=no`;
- the request: `SetEnv AGENTWORKS_EXEC=<envelope>` (D3).

Writing the request into the file rather than onto the command line keeps environment values, script
content and secrets out of the workstation's process arguments, which other local users can read.
Base64 has no quote, percent or whitespace characters, so it passes through `ssh_config` parsing
unchanged. A local check on OpenSSH 9.2p1 parsed a 131,072-character `SetEnv` value from a config
file intact; the live slices re-prove this on 8.5 and Windows.

The only command-line arguments are `-F`, `-T` or `-tt`, the destination and the constant bootstrap
(D4). Paths in the file are written with forward slashes, as `ssh_config.py` already does for
Windows.

### D3. The request envelope (R3, R4)

One variable, `AGENTWORKS_EXEC`, carries the whole request: a version tag, a random completion
nonce, the identity's elevation choice, the interpreter and startup flags, the literal argv or
script source, the environment and the working directory. Each field is base64-encoded on its own
line, and the whole block is base64-encoded once more so it is a single token.

- **Why `SetEnv`.** It works in every mode, including a TTY attach, where stdin is the terminal and
  cannot carry a header. It also leaves stdin wholly to the caller, which is the separation R3 asks
  for.
- **Why one variable.** `SetEnv` is first-value-wins per invocation (ADR 0014), and OpenSSH silently
  truncates a value at a newline (the scout measured this on 9.2p1). One encoded token avoids both.
- **Where it is accepted.** The VM must accept the variable: the existing
  `50-agentworks-accept-env.conf` fragment (`AcceptEnv *`) does that. Today only Phase B writes it.
  This design moves the fragment into the shared provisioning bootstrap script as well, so a brand
  new VM accepts the envelope at core's first connection (D10).
- **When it is missing.** An existing VM that never received the fragment gets a typed `VMNotReady`
  telling the operator to run recovery (D11). The bootstrap detects the missing variable and reports
  this; the client never guesses.
- **Size.** The envelope is bounded below the Linux single-string limit for an environment entry
  (131,072 bytes including the name). A larger request is refused before dispatch with a typed
  error. Scripts in the current tree are far below the bound, and bulk data goes through stdin or
  files, never the envelope.

### D4. The guest bootstrap and completion (R3, R4)

The remote command is constant:
`/usr/bin/env -u BASH_ENV -u ENV /bin/bash --noprofile --norc -c '<bootstrap>'`. It contains no
request data. The bootstrap is adapted from the proof's `bootstrap.py`, keeping its measured
handling of startup files, locale and private names. It needs only Bash 5.1, GNU coreutils and
util-linux, which every recognized Debian release provides from provisioning onward.

The bootstrap:

1. **Reads and checks the envelope**, then removes the variable from its own environment.
2. **Elevates when the request asks.** If the request is elevated and it is not root, it re-executes
   itself with `sudo -n --preserve-env=AGENTWORKS_EXEC`. The admin's `NOPASSWD: ALL` rule implies
   the needed `SETENV`, so no further sudoers change is required. An identity without sudo fails
   here with a framed permission result. There is no fallback to another identity (R3).
3. **Applies the request**: sets the environment and changes to the working directory.
4. **Runs the payload.** A literal argv is exec'd directly, with no shell. A script runs under the
   chosen interpreter with login or interactive startup only when requested. `sh` and `bash` are
   fixed paths; the default shell is the destination identity's `getent passwd` entry. A missing
   interpreter fails rather than substituting another.
5. **Reports completion.** Except in TTY mode, the bootstrap writes one framed trailer to stderr
   after the payload exits: `<nonce> exit <status>`, or `<nonce> refused <reason>` for its own
   failures.

Completion rules:

- The client strips the trailer and treats it as the only completion evidence, so a guest exit of
  255 is reported as completed with status 255.
- No trailer means completion is unknown, whatever the client's exit status.
- A client status of 255 before any trailer means not delivered, when stderr shows OpenSSH refused
  the connection. Otherwise it is unknown.
- In TTY mode there is no trailer: a 255 from an attach is reported as "connection ended or command
  exited 255", never guessed.

Background children that keep stderr open can write after the trailer; the client takes the last
well-formed trailer for the nonce and keeps later bytes as stderr. The nonce is removed from the
environment before the payload starts, so ordinary payload output cannot forge it.

### D5. Requests and results (R3, R4)

| Type                   | Shape                                                                                                           |
| ---------------------- | --------------------------------------------------------------------------------------------------------------- |
| `Command`              | Literal argv tuple. Empty arguments, whitespace and shell characters arrive exactly                             |
| `Script`               | Source text plus `Shell.SH`, `Shell.BASH` or `Shell.USER_DEFAULT`, with `login` and `interactive` flags         |
| Common options         | `env`, `cwd`, `elevated`, `deadline`, `observation` (read-only, may retry)                                      |
| Input                  | `NoInput` (EOF), `Bytes(data, sensitive=)`, `FromFile(path)`; streaming and attach inherit the terminal         |
| Output sink (buffered) | `Capture(limit)`, `Discard`, `ToFile(path, on_progress=)`                                                       |
| `Result`               | `completion` (`Completed(status)`, `NotDelivered(reason)`, `Unknown(reason)`), `stdout: bytes`, `stderr: bytes` |

- **Bytes, not text.** Results carry bytes without newline conversion; `text()` helpers decode for
  callers that want text. Stdin is written in binary mode on every workstation (ADR 0020's
  incident).
- **Sensitive inputs.** A sensitive input, the environment and script content are never logged and
  never appear in exceptions. A secret-bearing environment does not suppress output (R4); only an
  explicitly sensitive stdin marks the result's streams as not retained in logs.
- **Deadlines.** A deadline bounds the local wait, kills the local client and returns `Unknown`. It
  never claims remote termination.
- **Retries.** Only `observation=True` requests may retry, and only after `NotDelivered` or
  `Unknown`. Mutating requests are never re-dispatched.
- **`check=True`** turns anything but `Completed(0)` into a typed error carrying the same facts.

### D6. Files built on execution (R5)

Every file operation is a request whose payload is a short core-owned shell script, with content on
stdin or stdout. There is no scp, and no Python on the guest.

| Operation                                           | Mechanism                                                                                                                                                  | Current consumers                                    |
| --------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| `read(path)`, `download(path, local)`               | `cat` into a capture or file sink                                                                                                                          | native CLI transcripts, artifacts, backup            |
| `write(path, data, mode, owner, group)`             | `mktemp` in the destination directory, content from stdin, `chown` and `chmod` on the temp file, `sync`, then `mv -f` onto the destination                 | initializer fragments, profiles, tmuxinator, sources |
| `publish_if(path, data, expected, ...)`             | As `write`, under `flock` on the destination directory, after comparing `sha256sum` of the current file with the caller's expected digest or absence       | artifact publication, harness settings merge         |
| `update_json(path, merge, policy)`                  | Read with digest, merge on the workstation with the existing `capabilities/harness_integration/settings.py` policies, then `publish_if`; retry on conflict | harness settings                                     |
| `upload_tree`, `download_tree`                      | A `tarfile` stream on the workstation into or out of `tar` on the guest; upload extracts to a sibling staging directory and swaps it into place            | sources, workspace copy                              |
| `remove`, `ensure_directory`, `prune_empty_parents` | Short scripts                                                                                                                                              | artifacts, workspaces                                |

- **The merge stays on the workstation.** `settings.py` already keeps JSON null as a value and
  rejects malformed input. Running it there means the guest needs no merge code, and R5's JSON rules
  hold by construction.
- **Atomicity.** `publish_if` is atomic with respect to every Agentworks writer, because they all
  take the same directory lock. Hostile path races stay with the permissions effort (R5).
- **Elevation and identity.** Elevated placement is the request's `elevated` flag, so callers write
  no copy, chmod or sudo wrappers. Agent writes use the agent's `VMAccess`.
- **Failures.** A failure removes only the temp file or staging directory the operation created.

### D7. Detached work (R6)

The only current caller is VM backup (`vms/backup.py`); Remote Lima's host-side detached run is
platform-private (D11).

- **Launch.** `detached.launch(request) -> LaunchId` runs a short script. It creates
  `/var/tmp/agentworks/detached/<id>/`, mode 0700 and owned by the request's identity (root when
  elevated). It then starts a wrapper with `setsid nohup` that records the pid, output and exit
  status there, and returns at once. The id is random, so an independent new launch never matches an
  earlier one.
- **Observe.** `detached.observe(id, offset)` is an observation request returning new output, a
  running or finished state, and the status.
- **Stop.** `detached.stop(id)` signals the process group. Backup calls it from its interrupt
  handler, as it does today.
- **Cleanup.** `detached.discard(id)` removes the directory after the caller has what it needs.
- **What stays outside the transport.** The caller records the id and owns the VM availability hold
  (the existing activation gate, including the WSL2 keepalive). The transport never starts a VM and
  never holds one.

### D8. Streaming, terminal attach and forwarding (R2, R4)

- **`stream(request)`** inherits the operator's stdin and stdout, and pumps stderr through the
  trailer filter so the stream is live except for a short hold-back window. Current callers are
  `vm exec` and `agent exec`.
- **`attach(request, clear_screen_on_exit=)`** runs with `-tt` under `guarded_terminal`, keeping the
  post-guard notice and the Windows clear behavior. Current callers are session and console attach,
  `vm shell` and `agent shell`.
- **`forward(local, remote) -> context manager`** runs `ssh -N -L` under the same policy with
  `ExitOnForwardFailure=yes`. It returns only once the local port accepts a connection, raises a
  typed error with the client's diagnostic if the client exits first, and ends the client on exit.
  The current caller is `vm port-forward`.

### D9. The VM-bound host-key store (R8)

- **Layout.** One file per VM under `~/.config/agentworks/known_hosts/<vm>`, in `known_hosts`
  format, holding the VM's records under a host-key alias derived from the VM name. The VM name is
  the VM's identity throughout Agentworks (it keys `VMRow`) and survives every tailnet address
  change, which answers the FRD's open question about the identifier.
- **Lookup.** A connection uses `StrictHostKeyChecking=yes`. A missing file is a typed
  `TrustMissing` error naming the import and recovery commands. It never falls back to first use, so
  a missing record never makes an existing VM new.
- **Enrollment.** Only `vm create` enrolls. Its first core connection after provisioning uses
  `StrictHostKeyChecking=accept-new` against an empty store file that create has just reset. That
  connection runs the contract check (D10). The store is the only place the key is written.
- **Import (`agw vm import-host-keys [--known-hosts PATH] [VM...]`).**
  - Reads the default `~/.ssh/known_hosts`, or the named file, without modifying it.
  - Matches each VM's recorded tailnet address against plain and hashed (`|1|`) host fields, and
    copies matching key records into that VM's store file.
  - Reports an `@cert-authority` or `@revoked` marker that applies to a VM and imports nothing for
    that VM.
  - Never replaces an existing store record with a different key.
- **Re-establishment.** This is the trust step of `agw vm recover` (D11). It reads the VM's public
  host keys through the platform's recovery capability, shows their fingerprints, and writes them
  only after the operator confirms.
- **Removal.** `vm delete` removes the VM's store file after the platform delete succeeds.
- **Managed aliases.** `ssh_config.py` adds `HostKeyAlias`, `UserKnownHostsFile` (the store file)
  and `StrictHostKeyChecking yes` to each `awvm--` and `awagent--` entry, so the operator's own
  `ssh awvm--<vm>` verifies against the same store.
- **Legacy paths during migration.** Legacy core SSH paths change their trust options to the same
  store in the first slice. Nothing in core writes the operator's `known_hosts` from then on. These
  paths are the legacy argv builders, the backup scp, the workspace-copy ssh and the port forward.
  This is a small edit to legacy code in the direction R10 allows: legacy code calls new code, never
  the reverse.

### D10. The VM contract and its checks (R1)

`vms/contract.py` holds two read-only probe scripts and their diagnosis rendering. They run over
`VMAccess` and return one structured report naming each failed item and the fix.

- **Base contract.** Runs on core's first connection at create, and first at reinit. It checks:
  - a recognized Debian release, under ADR 0025's policy and recording;
  - systemd as PID 1;
  - the admin account and `sudo -n true`;
  - Tailscale up with the expected address;
  - the envelope accepted, which the request itself proves;
  - Bash 5.1 or newer, coreutils, util-linux, `tar` and `getent`.

  Because it is the first operation, the release check is the first thing core does on the common
  path.

- **Initialized contract.** Runs at the end of Phase B. It checks python3 and the
  `INIT_SYSTEM_PACKAGES` tools.

A failed check reinit can fix leaves the VM not ready and names reinit. A failed check reinit cannot
fix (an unrecognized release, no systemd) names recreation. The shared provisioning bootstrap script
adds the AcceptEnv fragment (D3); Phase B keeps writing it, so reinit converges older VMs whose
envelope is accepted, and recovery covers those whose envelope is not.

### D11. The platform surface (R1, R2, R7)

Changes to `capabilities/vm_platform/base.py`:

| Today                                         | After                                                                                                                    |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `ProvisionResult.native_transport` (required) | Removed. Core starts over SSH (R1)                                                                                       |
| `ProvisionResult.tailscale_ip: str \| None`   | `tailnet_address: str`, required. A platform that cannot read it fails create rather than leaving core a native fallback |
| `native_transport(vm, ctx, config)`           | Removed                                                                                                                  |
| (none)                                        | `run_recovery(vm, ctx, script, stdin, deadline) -> RecoveryResult`: root, once, no Tailscale dependency                  |
| (none)                                        | `ClassVar recovery_needs_guest_sshd: bool`                                                                               |
| `native_shell_unavailable_hint`               | Kept. Platforms that offer it implement `native_shell(vm, ctx) -> int`                                                   |
| `transient_route`, `probe_failure_hint`       | Become each platform's private detail inside `run_recovery`                                                              |

`RecoveryResult` carries the script's exit status and bounded stdout and stderr, or a delivery
failure, or unknown completion. Sensitive stdin follows D5. The capability takes no environment,
directory or files.

Per platform:

| Platform    | Recovery channel                           | Needs guest sshd | Private access kept                                                  |
| ----------- | ------------------------------------------ | ---------------- | -------------------------------------------------------------------- |
| Lima, local | `limactl shell`                            | yes              | `limactl` wrappers (copied from `transports/lima.py`)                |
| Remote Lima | host SSH, then `limactl shell`             | yes              | host SSH honoring operator config; host-side detached run for create |
| WSL2        | `wsl.exe --user root`                      | no               | `wsl.exe` wrappers and the keepalive                                 |
| Proxmox     | QEMU guest agent                           | no               | QGA client, rewritten as `plugins/proxmox/guest_agent.py`            |
| AWS         | SSH to the public address, transient allow | yes              | provisioning SSH with a throwaway known-hosts file                   |
| Azure       | SSH to the public address, transient allow | yes              | same                                                                 |
| GCP         | SSH to the public address, transient allow | yes              | same                                                                 |

- **Cloud public-address SSH.** This was the FRD's open question about which store holds the hop's
  keys. The public address reaches the same sshd, with the same host key, as the tailnet. So cloud
  `run_recovery` verifies against the VM's store entry through `recovery_ssh.py`. It runs
  `sudo -n /bin/bash -c '<script>'`, with the script on the command line (it is core-owned and holds
  no secrets) and sensitive input on stdin. It needs no envelope, so a VM whose sshd no longer
  accepts the envelope is still recoverable. Provisioning, which happens before any store record
  exists, uses a throwaway known-hosts file per create. The cloud hop therefore stops writing the
  operator's `known_hosts`. Remote Lima's host connection keeps honoring operator configuration, as
  ruled.
- **Core's recovery scripts** live in `vms/recovery.py`. Each one runs through `run_recovery`:
  - **Rejoin** is today's `rejoin_tailscale`, still invoked automatically by the activation gate.
  - **Rekey** is `vm rekey`. It is verified over SSH afterwards, where access returns.
  - **Logout** runs at delete. Its result is the script's own status, or unknown if the connection
    drops first.
  - **Access repair** restores the admin authorized key, re-enables sshd with the Agentworks
    fragments where the platform's channel does not need it, and prints the public host keys.
- **The operator entry point** is the new `agw vm recover <vm>`. It runs access repair, rejoins if
  needed, offers host-key re-establishment (D9) and verifies over SSH. When the platform's channel
  needs the guest's sshd and sshd is broken, or recovery fails, it stops and tells the operator to
  repair or recreate the machine (R7).
- **Debian attestation at create** moves from the native transport to the base contract check (D10).
  The Phase A `tailscale ip -4` fallback goes away with the required `tailnet_address` (R10).

### D12. Delivery to consumers (R9)

- **The access object.** `VMAccess` is built from a VM row, the config and an identity (`Admin` or
  `Agent(agent)`). Construction is pure: it copies names and paths and resolves nothing. The client,
  trust file, secrets and route are resolved only when an operation runs.
- **Capability accessors.** `RunContext` gains `admin_access()` and `agent_access()`, returning
  `VMAccess | None`, beside today's `admin_target()` and `agent_target()` until the last capability
  consumer moves.
- **Core managers** call `execution.vm_access(vm, config, identity)` directly, without a
  `RunContext`.
- **Agent identity.** An agent-bound `VMAccess` connects as the agent's Linux user (ADR 0011). An
  elevated request through it fails at the bootstrap's `sudo -n` with a typed permission error; it
  never yields admin authority.
- **Test seams.** Seams that tests patch today (`_mgr.transport` and similar) are replaced by
  injecting a fake `VMAccess` factory. A small in-process fake under `cli/tests/execution/` runs
  requests against a local bootstrap, so manager tests exercise real request shapes.

### D13. Errors and logging

`execution/errors.py` defines `ExecutionError` and its subtypes:

- `NotDelivered`, `CompletionUnknown`, `CommandFailed` (carrying the status);
- `PermissionDenied`, `FeatureUnavailable`, `RequestTooLarge`;
- `TrustMissing`, `TrustMismatch`;
- `ClientUnsupported`, `VMNotReady`.

`ExecutionError` subclasses `agentworks.errors.ConnectivityError`, as `SSHError` does today, so CLI
rendering keeps working. Consumers catch the new types as they migrate; `cli/_entry.py`'s `SSHError`
special case is removed with the old stack.

`execution/logging.py` writes the same per-VM operation log under `~/.config/agentworks/logs/`. It
records an operation label, the request shape and the outcome, never payloads. Readers of the old
`LOG_DIR` constant (`doctor_state.py`, `vm logs`) switch to the new module's constant.

### D14. Enforcing independence (R10)

The existing fresh-interpreter guard in `cli/tests/execution/test_independence.py` stays and keeps
walking every `agentworks.execution` submodule with the six legacy modules blocked. A second guard
is added per migrated consumer module group: as each batch lands, its modules join a list asserted
importable with the legacy modules blocked. The list ends as the whole package, at which point the
legacy modules are deleted.

## Migration

The plan sequences eight landings. Each is complete on its own terms and carries live evidence for
what it touches (R10, R11).

1. **Trust.** The store, import command, create-time enrollment and managed alias options. Legacy
   SSH paths verify against the store. This removes the address-keyed first use everywhere before
   any consumer moves.
2. **Execution core with the operator exec surface.** Client resolution, policy, envelope,
   bootstrap, results and logging. The consumers are `vm exec`, `vm shell`, `agent exec`,
   `agent shell`, `vm verify-connection`, release probing and `vm port-forward`.
3. **VM contract and platform surface.** The required tailnet address, `run_recovery`, the base and
   initialized contract checks, and `vm recover`. The recovery flows (rejoin, rekey, logout) move,
   `native_transport` is deleted, and Debian attestation moves onto SSH. Every platform's private
   access stops importing legacy modules.
4. **Files and capability delivery.** `files.py`, the `RunContext` accessors and the harness,
   artifact, native-file and git-credential consumers (scout batches 8 and 9).
5. **Initialization.** VM and agent initializers, hardening, DNS, sources and setup runners.
6. **Workspaces and sessions.** Workspace backends, copy, rehome and repair; tmux, sessions and
   consoles.
7. **Backup and detached work.** Backup's launch, observation, interrupt and download.
8. **Removal.** Delete the six legacy modules, `admin_target()`/`agent_target()`, the proof
   leftovers and their tests, after the incident inventory is re-proved and the full evidence matrix
   is complete.

The incident inventory (R10) starts from the transport-improv migration strategy's three rows. The
scouted additions are:

- the sensitive-input pairing rules;
- the `--` fence;
- the post-guard attach notice;
- keepalives on long-lived paths;
- `nohup` outside `sudo`;
- console recovery after a killed `ssh -t`;
- host-key preservation across stop and start.

Each row names the slice whose evidence re-proves it.

## Evidence matrix (R11)

Supported pairings, from each platform's `unsupported_reason` and readiness checks:

| Platform                 | Linux | macOS         | Windows |
| ------------------------ | ----- | ------------- | ------- |
| Lima, local              | yes   | yes           | no      |
| Remote Lima (macOS host) | yes   | yes (primary) | yes     |
| WSL2                     | no    | no            | yes     |
| Proxmox                  | yes   | yes           | yes     |
| AWS                      | yes   | yes           | yes     |
| Azure                    | yes   | yes           | yes     |
| GCP                      | yes   | yes           | yes     |

That is 18 pairings. Local Lima on Windows is excluded because Lima's Windows support is not a
supported Agentworks path today; the plan records that disposition for the operator. Each slice
names the cells it must cover; removal needs all 18, with any gap sent to the operator.

## ADR amendments

- **ADR 0014** (SSH `SetEnv` delivery): amended in slice 2. Environment now travels inside the
  single encoded envelope variable from a private client config. The newline limitation is gone. The
  `AcceptEnv *` fragment and sudoers `env_keep` remain.
- **ADR 0025** (release probing over the native transport): amended in slice 3. Core attests the
  release over SSH as the first contract check.
- **ADR 0003** needs no change. Its statement that access after bootstrap is SSH over Tailscale
  becomes exactly true.

## Risks and early proofs

| Risk                                                                    | Proof, and the slice that carries it                                   |
| ----------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `SetEnv` from a config file behaves differently on 8.5 or Win32-OpenSSH | Envelope round trip at the size bound, slice 2, all three workstations |
| sshd or PAM limits on a large single environment value                  | Bound probe on Debian 12 and 13, slice 2                               |
| Per-invocation config file paths on Windows                             | Slice 2 Windows cells                                                  |
| The trailer on stderr under background children                         | Conformance vector, slice 2                                            |
| `flock` on a directory fd across Debian releases                        | Conformance vector, slice 4                                            |
| Operators with encrypted keys and no agent                              | Live cell with an agent-held key, slice 1                              |
| Existing VMs lacking the AcceptEnv fragment                             | Recovery path, slice 3                                                 |

## Open questions for the operator

1. **Command names.** `agw vm import-host-keys` and `agw vm recover`. Both are new operator-facing
   commands. The FRD requires their behavior, not their names.
2. **Request data on local disk.** The per-invocation client config briefly holds the encoded
   request, including any secret environment values, in a user-only file under
   `~/.config/agentworks/run/ssh/`, removed when the client exits. This replaces today's placement
   of `SetEnv` values on the client's command line, where other local users can read them. The
   lead's recommendation is to accept it.
3. **Local Lima on Windows.** Confirm it is outside the evidence matrix.

## Requirements traceability

| Requirement | Decisions             |
| ----------- | --------------------- |
| R1          | D10, D11              |
| R2          | D2, D8, D11           |
| R3          | D3, D4, D5            |
| R4          | D2, D4, D5, D8        |
| R5          | D6                    |
| R6          | D7                    |
| R7          | D11                   |
| R8          | D1, D2, D9, D11       |
| R9          | D12                   |
| R10         | D14, Migration        |
| R11         | Evidence matrix, plan |
