# Transport Take 2: Platform Low-Level Design (Slice 4)

- Status: Draft for the private review loop.
- Date: 2026-11-01
- Architecture: [hla.md](./hla.md) D2 (host policy), D9 (re-establishment), D11 (second half), D13,
  D14; requirements: R1, R2, R4, R7, R8, R10, R11
- Plan: [plan.md](./plan.md), slice 4, delivered in PR B with slice 3
  ([contract-lld.md](./contract-lld.md)) under vm-platform `contract_version` 2
- Source baseline: `60d347220` (PR A's code with the slice 3 design merged into the SDD, slice 3 not
  yet implemented). Every `path:line` is at that commit; slice 3's shapes are cited from its LLD.

## Scope

This LLD settles slice 4: the one native capability core requires from a platform, each platform's
private access, the commands that use the capability, and the deletions that retire the general
native transport.

**The design in one paragraph.** Every platform implements one primitive,
`run_as_root(vm, ctx, script, deadline) -> RawRun`: run `/bin/bash -s` as root through its own
channel with `script` on stdin and return both streams. Core owns everything else: a wrapper that
frames the script with D4's markers, reads the status from the markers alone, and carries the one
secret a script may need inside the script text, never in an argument. The same wrapper and the same
per-platform primitive also carry provisioning's join, address read and logout, so the six platforms
lose their legacy transports in one move. `vm start` and the activation gate only wait for the VM to
answer over SSH; `agw vm recover` is the one place a rejoin happens.

**Operator direction applied.** Where detecting and alerting is enough, no mechanism: a
never-enrolled cloud VM is not logged out at delete (Open question 1), a missing Tailscale is
reported rather than installed, and a broken sshd is restarted, not rebuilt. Every type below has a
caller in this slice; what this LLD cuts from the HLA's slice 4 surface is under Amendments.

**Out of this slice.** Phase B stays legacy until slice 6. `vm delete`'s session teardown keeps its
legacy `transport()` until slice 7 (`vms/manager/power.py:282-305`). `LOG_DIR` stays until slice 8.
`transports/__init__.py`, `transports/base.py` and `transports/ssh.py` keep their legacy callers
until slice 8; this slice deletes only what it leaves with no caller.

## Module layout

| Module                                                            | Contents after this slice                                                                                                            |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `execution/root_script.py` (new)                                  | `RawRun`, `RootRunner`, `RootScriptResult`, `run(root, payload, *, deadline, secret=None)`, `run_process(argv, script, *, deadline)` |
| `execution/pump.py`, `ssh_process.py`                             | `PipeSession(nonce=None)` passes streams through; `HostPolicy` and `build_host_argv`; `spawn` names the program it failed to start   |
| `execution/trust.py`                                              | `HOST_KEYS_SCRIPT`, `read_host_keys(root)`, `fingerprint(key)`                                                                       |
| `execution/errors.py`                                             | `FeatureUnavailable`; `VMNotReady` and `TrustMissing` hints name `agw vm recover`                                                    |
| `capabilities/vm_platform/base.py`                                | The surface below                                                                                                                    |
| `capabilities/vm_platform/public_ssh.py` (new)                    | `PublicSSH`: the clouds' SSH to a public address                                                                                     |
| `capabilities/vm_platform/lima_host.py` (new)                     | `LimaHost`: limactl locally or on the placement host, its root runner and shell, Remote Lima's detached create                       |
| `capabilities/vm_platform/tailscale_join.py`                      | `join_tailscale`, `settle_tailnet_address`, `EphemeralTailscaleBootstrap`, all over a `RootRunner`                                   |
| `capabilities/vm_platform/wsl2.py`, `wsl2_bootstrap.py`           | The `wsl.exe` root runner and shell; the key-free bootstrap run through it                                                           |
| `plugins/proxmox/guest_agent.py` (new)                            | `run_root`: QGA exec of `/bin/bash -s` with `input-data`, polled to the deadline                                                     |
| `vms/recovery.py` (new)                                           | `run_recovery` and core's four scripts: access repair, rejoin, rekey, logout                                                         |
| `vms/manager/recover.py` (new)                                    | `recover_vm`, the `agw vm recover` service                                                                                           |
| `vms/manager/tailscale.py`, `power.py`, `exec.py`, `vms/nodes.py` | `_await_tailnet`; start, rekey and delete on recovery; `vm shell --platform` on `native_shell`; the gate's wait                      |

Deletions are listed under Independence and in each section.

## The root script channel

### What a platform implements

```text
class RawRun:            # frozen; both fields repr=False
    stdout: bytes
    stderr: bytes

RootRunner = Callable[[bytes, float], RawRun]   # (script, deadline in seconds), positional

VMPlatform.run_as_root(vm, ctx, script: bytes, deadline: float) -> RawRun   # abstract
```

The contract a platform author follows, in full:

- Run `/bin/bash -s` as root through the platform's channel, write `script` to its stdin, close it,
  and return everything the channel printed on each stream when it ends or `deadline` seconds pass.
  Stop waiting at the deadline; never claim the guest command stopped.
- Run it once. Never retry, never parse the output, never report an exit code: `RawRun` has no field
  for one, because no channel's status is trusted (WSL passes raw wait statuses through, per the
  salvage message, and an ssh 255 cannot be told from a guest 255).
- Treat `script` as sensitive: it never reaches an argument, a log, an exception or a file.
- Raise an `AgentworksError` (usually `ConnectivityError`) when the channel cannot be started or the
  provider call fails. Core treats a raise like an unknown outcome and never dispatches again.

The script and any secret travel together on stdin, so R7's "finite sensitive stdin" is the script
itself. A separate stdin argument is not needed: the one secret any core script uses, the Tailscale
auth key, is embedded by the wrapper below. This merges the HLA's `script` and `stdin` parameters
(Amendments).

`/bin/bash` rather than `/bin/sh` because WSL2's and Proxmox's bootstrap, which this slice moves
onto the same primitive, is a Bash script (`set -euo pipefail`, `bootstrap_script.py:62`), and
Debian's dash has no `pipefail`. Bash is Essential on Debian.

### The wrapper and its result

`root_script.run(root, payload, *, deadline, secret=None) -> RootScriptResult` builds the bytes,
calls `root(script, deadline)` once, and reads the result:

```text
{
exec </dev/null
trap '' HUP PIPE
AGW_SECRET='<secret, shell-quoted>'            # only when secret is given
printf '\036<nonce> start\n'; printf '\036<nonce> start\n' >&2
(
<payload>
)
s=$?
printf '\036<nonce> end\n'; printf '\036<nonce> exit %d\n' "$s" >&2
exit 0
}
```

Each line has a measured reason (Debian 12's `/bin/bash` 5.2.15 and `/bin/dash` 0.5.12, with a local
pipe standing in for the channel; vectors W1 to W5 repeat them):

- **One brace group.** The shell parses the whole group before running any of it, so a payload
  command that reads stdin gets end of file from `exec </dev/null`, not script bytes. A truncated
  script, as a startup file that reads stdin would leave, is a syntax error and runs nothing, not
  even the start marker.
- **`exit 0` inside the group.** dash reads ahead and ran bytes appended after the group; the `exit`
  stops both shells there.
- **`trap '' HUP PIPE`.** With the channel's read ends closed mid-payload, the payload died of
  `SIGPIPE` and its effect did not happen; with the trap it ran to its end. This replaces the
  `nohup ... &` that delete's logout uses today because "logout was disrupting the network stack and
  thus not completing" on Azure (`21bb259f9`, `vms/manager/tailscale.py:177-181`), and it keeps the
  logout's status observable when the channel survives. Children inherit the ignored dispositions,
  which core's scripts tolerate (no pipelines).
- **`AGW_SECRET` unexported.** The payload's subshell sees it and its child processes' environment
  does not (measured), so it reaches a program only where a script passes it as an argument:
  `tailscale up --auth-key "$AGW_SECRET"`, exactly today's guest-side exposure
  (`tailscale_join.py:15-17`, `lima.py:478-479`). The value is validated line-safe first
  (`require_line_safe_secret`), then `shlex.quote`d.
- **Markers.** D4's grammar and a fresh `envelope.new_nonce()`, so `envelope.StreamFilter` reads
  them. The payload is core's own code, so the nonce in the script text needs no secrecy.

```text
class RootScriptResult:        # frozen
    status: int | None         # the payload's status; None when it cannot be established
    stdout: bytes              # payload bytes between the markers, secret redacted (repr=False)
    stderr: bytes
    channel_tail: str          # last 20 lines of stderr before the start marker, sanitized, redacted
```

`status` is `N` only when stdout closes with `end` and stderr with `exit N`; anything else, a
deadline, an output limit or a raise included, leaves no status (a raise propagates). The pre-marker
stderr is where ssh, sudo, `limactl` and `wsl.exe` put their own complaints, so `channel_tail` is
what an error shows for an unknown outcome; it is passed through `ssh_process.tail_lines`
(`execution/ssh_process.py:231-240`) and has the secret replaced by `[REDACTED]`, as are both
streams. Recovery writes no operation log: nothing in this path holds an `SSHLogger` (incident row
I31).

### The channels

Four channels are local processes and share `root_script.run_process(argv, script, *, deadline)`,
built on the pump rather than a second process loop. `PipeSession` (`execution/pump.py:163-219`)
accepts `nonce=None`, passing both streams through unfiltered, so a session ends when the process
exits and its pipes go quiet, at end of file, at the deadline, or when a stream passes its 1 MiB
`Capture` (`pump.py:37`, `:255-276`). A descendant holding a pipe never stalls it, and a deadline or
limit kills the process, leaving the outcome unknown. `ssh_process.spawn` (`ssh_process.py:259-272`)
stays the one place a process starts; its `ClientUnsupported` message names `argv[0]` instead of
assuming OpenSSH.

| Platform    | `argv` (stdin is the script)                                                                           | Bounds                                   | Needs guest sshd | Native shell                                 |
| ----------- | ------------------------------------------------------------------------------------------------------ | ---------------------------------------- | ---------------- | -------------------------------------------- |
| Lima, local | `limactl shell <instance> sudo -n /bin/bash -s`                                                        | `run_process`                            | yes              | `limactl shell <instance>`                   |
| Remote Lima | host policy `ssh -T <host> '$SHELL -lc "limactl shell <instance> sudo -n /bin/bash -s"'`               | `run_process`                            | yes              | host policy `ssh -t`, `limactl shell` inside |
| WSL2        | `wsl --distribution <distro> --user root -- /bin/bash -s`                                              | `run_process`                            | no               | `wsl --distribution <distro> --user <admin>` |
| Proxmox     | QGA `exec` of `/bin/bash -s` with `input-data`, then `exec-status` every 2 s                           | polling stops at the deadline; QGA's cap | no               | none (Proxmox web console)                   |
| Clouds      | `PublicSSH`: host policy `ssh -T -l <admin> <public address> 'sudo -n /bin/bash -s'`, inside the route | `run_process`                            | yes              | host policy `ssh -t`, inside the route       |

- Every channel already carries stdin byte-exact in production: Lima's join over `limactl shell`
  (`lima.py:120`, `:490-493`), WSL2's and Proxmox's `input_text` (`transports/wsl2.py:90-95`,
  `plugins/proxmox/transport.py:74-80`), and the clouds' join over `SSHTransport`
  (`tailscale_join.py:43-48`).
- No argv carries request data, so D2's "script content out of arguments" holds on every channel,
  the cloud one the plan names included.
- Proxmox: QGA runs commands as root (`plugins/proxmox/transport.py:3-4`). `input-data` keeps the
  existing 65,536-character guard (`transport.py:24`), well above the largest script here (the
  bootstrap, 14,359 bytes measured from `generate_bootstrap_script`). A truncated QGA report raises
  (`plugins/proxmox/api.py:102-103`). At the deadline the result is empty, hence unknown.

## The host policy and `public_ssh.py`

`ssh_process` gains the HLA's second policy, for a platform's own SSH to a host or a public address.
It honors operator configuration (ruled 2026-10-23), so
`build_host_argv(client, policy, host, user, mode, remote_command)` omits `-F none` and takes:

```text
class HostPolicy:                         # frozen
    identity_file: Path | None            # the clouds pass operator.ssh_private_key; Remote Lima None
    trust: TrustBinding | HostTrust       # HOST_TRUST for Remote Lima; VM or UNVERIFIED for a cloud
```

- **Options.** `trust.ssh_options(policy.trust)` (`execution/trust.py:314-344`); with an identity,
  `IdentityFile` and `IdentitiesOnly=yes`; `ConnectTimeout=30` and the VM policy's keepalives;
  `ForwardAgent=no` and `ForwardX11=no`, because an Unverified peer must not be handed the
  operator's agent or display whatever their configuration says; `BatchMode=yes` in pipe mode only,
  since today's interactive Remote Lima hop allows prompts (`transports/remote_lima.py:135-142`).
- **Modes.** `-T` with a remote command, or `-t` for a native shell (`ArgvMode.PIPE` and
  `TERMINAL`).
- **Destination.** Operator-authored (the Remote Lima `host`, which may be `user@host`) or
  provider-reported (a public address), passed whole after `--`. It is refused if it contains
  whitespace or a control character. `user` is `-l` when given.
- The resolved client is required, as for the VM policy, so Remote Lima from Windows gets D1's
  resolution too.

`PublicSSH(host, user, identity_file, trust)` is the clouds' channel, used by their provisioning,
recovery, key read and shell:

- `run_as_root(script, deadline) -> RawRun` is `run_process` over the pipe argv, remote command
  `sudo -n /bin/bash -s`, a constant that the admin's login shell (one of the four D4 allows) parses
  unchanged.
- `wait_until_answering(*, failure_hint)` probes with
  `root_script.run(self.run_as_root, "true", deadline=10)` up to six times, three seconds apart, and
  warns `failure_hint` before raising `ConnectivityError` when none completes: today's propagation
  wait after a fresh firewall allow (`transports/__init__.py:230-244`, incident row I17).
  Re-dispatching `true` is a read-only observation, so the retry is allowed.
- `read_host_keys()` is `trust.read_host_keys(self.run_as_root)`.
- `shell() -> int` runs the terminal argv with inherited stdio and returns its status.

## The platform surface

Changes to `capabilities/vm_platform/base.py`, under `contract_version` 2 (slice 3's bump):

| Member                                                      | After this slice                                                                                                                                                                          |
| ----------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `run_as_root(vm, ctx, script, deadline)`                    | New, abstract, and in the descriptor's `required_operations` (`capabilities/vm_platform/kinds.py:138`)                                                                                    |
| `recovery_needs_guest_sshd: ClassVar[bool]`                 | New, annotation only, in `required_attributes` (`kinds.py:140`), so registration refuses a platform that does not declare it (`capabilities/conformance.py:152`)                          |
| `read_host_keys(vm, ctx) -> tuple[HostKey, ...]`            | New with a default, `trust.read_host_keys(partial(self.run_as_root, vm, ctx))`. The clouds override it. No provider fingerprints (Open question 3), so the existing `HostKey` is the type |
| `native_shell(vm, ctx) -> int`                              | New. The default raises `FeatureUnavailable`; a platform offers a shell by overriding it, and core checks `type(platform).native_shell is VMPlatform.native_shell` before the boundary    |
| `native_shell_unavailable_hint`                             | Kept, as guidance text only (Proxmox's console pointer, `plugins/proxmox/platform.py:154-158`); it no longer declares anything                                                            |
| `native_transport`, `transient_route`, `probe_failure_hint` | Deleted (`base.py:335`, `:408`, `:203`). The clouds keep their route as a private `_transient_route(vm, ctx)` reading `ctx.config`, and their hint as a module constant                   |
| `ProvisionRequest.ssh_private_key`                          | `Path`, not `Path \| None`: create always passes `config.operator.ssh_private_key` (`vms/manager/lifecycle.py:510`), a required `Path` (`config/models.py:29`)                            |

The docstrings that present the route as a public hook (`:355-375`, `:377-406`, `:433-460`) are
rewritten to match. `trust.read_host_keys(root)` runs `cat /etc/ssh/ssh_host_*_key.pub` (30 s) and
keeps each line `trust.is_valid_key` accepts (`trust.py:189`), since guest output is a boundary; an
unknown or non-zero outcome, or no valid key, raises. `trust.fingerprint(key)` is OpenSSH's
`SHA256:` form.

## Platforms' private access and provisioning

### `tailscale_join.py`

Today it takes an `ExecTransport` and raises `SSHError` (`tailscale_join.py:10`, `:13`). Every
platform now has a root runner, so it takes one, and slice 3's two settle forms (a callback form for
Lima and `settle_on_transport`) collapse into one:

- `join_tailscale(root, auth_key) -> None` runs `tailscale up --auth-key "$AGW_SECRET"` once with
  the key as the wrapper's secret and a 30-second deadline, as today (`tailscale_join.py:84`). A
  non-zero status raises `ProvisioningError`; an unknown one raises naming the node for the admin
  console, since it may have joined. Neither retries (I24).
- `settle_tailnet_address(root, *, node) -> str` keeps slice 3's behavior: up to six
  `tailscale ip -4` reads two seconds apart, each parsed by `parse_tailnet_address`, then one
  `tailscale logout` and `ProvisioningError` when they run out.
- `EphemeralTailscaleBootstrap(root, ...)` keeps its readiness wait (`:87-104`: 30 polls of `true`
  ten seconds apart, then the readiness command with 600 seconds), then the join, then the settle.
- `TAILSCALE_JOIN_STDIN_COMMAND` is deleted: the key no longer needs a `read` on the guest.

**The bootstrap script loses its key.** WSL2 and Proxmox are the last two platforms that generate
the bootstrap with the key inside it (`capabilities/vm_platform/wsl2_bootstrap.py:62-69`,
`plugins/proxmox/platform.py:401-408`) and stage it in a guest file that must be removed and
verified (`wsl2_bootstrap.py:71-151`, `plugins/proxmox/platform.py:566-624`). Both now run the
key-free script through their root runner and then call `join_tailscale`, like Lima and the clouds.
`generate_bootstrap_script` loses its `tailscale_auth_key` parameter and step 7
(`bootstrap_script.py:236-247`), so no generated script can hold the key: I10 holds by construction
on every platform, and the staging files go.

### Lima and Remote Lima

`lima_host.LimaHost` replaces `_run_lima` (`lima.py:270-311`), `_host_transport` (`:557-568`) and
the two legacy transports, and imports nothing legacy:

- `run(command, *, input=None, check=True, timeout=None) -> HostResult(status, stdout)` is today's
  `_run_lima`: `subprocess.run(shlex.split(command))` locally, or the host-policy pipe argv with
  `$SHELL -lc <command>` remotely, the login wrap that finds Homebrew's `limactl`
  (`transports/remote_lima.py:1-7`, `ssh.py:420-421`). It raises a module-private
  `LimaCommandFailed(ExternalError)` where it raises `SSHError` today, with the same rule that a
  command with input exposes neither stream (`lima.py:303-311`).
- `root_runner(instance) -> RootRunner` and `shell(instance) -> int` are the table's rows.
- `upload`, `run_detached` and `kill_detached` serve the remote create (`lima.py:577-669`), the pair
  copied from `remote_exec.py:47-213` with only the branch its one caller takes
  (`reuse_completed=False`, `quiet=True`, unelevated, `lima.py:623-631`). Its output goes to
  `request.progress` instead of a second `SSHLogger` (`lima.py:607-644`), so the create's own log
  holds it.

`create` joins with `join_tailscale(host.root_runner(instance), key)` instead of its own stdin
command (`lima.py:471-493`); the restart-sentinel probe and the cloud-init log read stay `run`
commands. `native_transport` (`:799-808`) and `_transport_for` (`:535-539`) are deleted.
`recovery_needs_guest_sshd = True`: `limactl shell` is SSH to the guest's sshd with Lima's own key.

### WSL2

The root runner is the table's `wsl.exe` argv through `run_process`; `native_shell` is today's
interactive argv (`transports/wsl2.py:137-142`) as the admin. `create` builds no `WSL2Transport`
(`wsl2.py:765-775`); `run_wsl2_bootstrap(root, ...)` runs the key-free script with a 900-second
deadline (today's, `wsl2_bootstrap.py:130-134`) and parses its markers from the result's stdout,
then create joins and settles. The `setsid` and `</dev/null` wrapping of the staged run has no
remaining job: there is no file, and the wrapper gives the payload a null stdin.
`recovery_needs_guest_sshd = False`.

### Proxmox

`guest_agent.run_root(api, node, vmid, script, deadline) -> RawRun` is written fresh from the
dispatch-and-poll half of `plugins/proxmox/transport.py:51-137`: one `guest_agent_exec` of
`["/bin/bash", "-s"]` with `input-data`, then `guest_agent_exec_status` every two seconds until it
reports exit or the deadline passes. Dispatch and status failures raise `ConnectivityError` with no
stdin in the message (`transport.py:69-123`). `create` runs the bootstrap and the join through it,
deleting `_run_bootstrap_via_agent` (`platform.py:566-636`); `native_transport` (`:488-502`) and
`transport.py` go. No `native_shell` override. `recovery_needs_guest_sshd = False`.

### AWS, Azure and GCP

Each cloud keeps one private helper, `_public_ssh(vm, ctx, trust) -> PublicSSH`, holding today's
live public-address read (`plugins/aws/platform.py:703-718`, `plugins/azure/platform.py:800-824`,
`plugins/gcp/platform.py:580-597`). An empty address raises `StateError` with the platform's own
console hint, where the factory raised it for every platform (`transports/__init__.py:215-228`,
incident row I32). Then:

- `run_as_root`: `trust.require_record(vm.name)` first, so a VM with no record opens no firewall
  rule, then inside `_transient_route`: `wait_until_answering(failure_hint=...)` and
  `run_as_root(script, deadline)` under VM trust.
- `read_host_keys`: inside the route and `trust.provisioning_binding(vm.name)` (`trust.py:281-299`),
  `wait_until_answering` and `read_host_keys()` under that Unverified file. HLA D9's only
  post-provisioning use of Unverified.
- `native_shell`: inside the route, the probe, then `shell()`.
- `create` builds
  `PublicSSH(public address, admin, request.ssh_private_key, request.provisioning_trust)` where it
  builds an `SSHTransport` (`aws/platform.py:573-582`, `azure/platform.py:643-652`,
  `gcp/platform.py:411-423`) and hands `EphemeralTailscaleBootstrap` its `run_as_root`.

`recovery_needs_guest_sshd = True`. Unverified provisioning before enrollment is unchanged
(trust-lld, "Trust modes and options").

## Core's recovery scripts

`vms/recovery.py` holds `run_recovery(platform, vm, ctx, payload, *, deadline, secret=None)`, which
is `root_script.run(partial(platform.run_as_root, vm, ctx), ...)`, and these payloads (Bash,
`set -eu`):

| Script        | What it does                                                                                                                                                                                                                                                                                                                                                     | Deadline | Secret   |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- | -------- |
| Access repair | Exit 3 if the admin account is missing; append the configured public key to the admin's `authorized_keys` unless the exact line is there, with today's modes (`bootstrap_script.py:143-155`); install python3 when `command -v` finds none; and when `REPAIR_SSHD=1`, `systemctl unmask`, `enable` and `restart ssh.service`, exiting 4 first if `sshd -t` fails | 600 s    | none     |
| Rejoin        | Exit 3 if `tailscale` is missing; `tailscale up --auth-key "$AGW_SECRET"`; print `tailscale ip -4`                                                                                                                                                                                                                                                               | 60 s     | auth key |
| Rekey         | Today's sequence (`vms/manager/power.py:499-521`): restart `tailscaled`, logout, join, restart, each followed by 15 s, then print `tailscale ip -4`                                                                                                                                                                                                              | 180 s    | auth key |
| Logout        | `tailscale logout`                                                                                                                                                                                                                                                                                                                                               | 30 s     | none     |

- `REPAIR_SSHD` is `0` or `1` from `not platform.recovery_needs_guest_sshd`, the attribute's first
  caller: where the channel is sshd, a broken sshd means the channel failed and nothing could run.
- The public key is `prepare_configured_ssh_identity(...).public_text`
  (`vms/applied_state.py:156-170`), already parsed as one key line.
- Not carried over: rejoin's `curl | sh` install (`vms/initializer/credentials.py:61-65`), since
  provisioning always installs Tailscale and its absence is now exit 3; regenerating host keys,
  which would silently change trust; and the HLA's "reload sshd on every platform", which nothing
  needs, since no script edits sshd's configuration. A missing `openssh-server` fails `sshd -t`, so
  recover stops with the repair-or-recreate message.
- No script leaves a background child, so `run_process` returns when the channel exits.
- The rejoin and rekey stdout's first line goes through `parse_tailnet_address` (slice 3), replacing
  the two `IPv4Address` checks (`credentials.py:89-96`, `power.py:520-527`).

## Commands

### `vm start` and the activation gate

The operator's rulings of 2026-10-31 and 2026-11-01: start the VM, wait for it to answer, and name
`agw vm recover` when it does not. One helper in `vms/manager/tailscale.py` serves both:

```text
_await_tailnet(db, config, vm, *, already_running: bool) -> None
```

It re-reads the row and, when it has an address, runs slice 3's `reachability.wait` on the admin's
`VMAccess`, with `VERIFY` wording for an already running VM and `RECONNECT` otherwise. `False`, or
no address, raises `ConnectivityError` ("VM 'x' is running but did not answer over SSH at ADDRESS")
with the hint `Run 'agw vm recover x'.`; the wait's own raises (`TrustMismatch`,
`Unknown(AUTH_FAILED)`, `VMNotReady`) propagate with their hints.

- `start_vm` replaces the rejoin block and its auth-key resolution (`vms/manager/power.py:85-120`)
  with `_await_tailnet` inside the same `hold_active()`. The `trust.require_record` before the
  boundary stays (`:53-56`); its comment now says the wait could only fail.
- `LiveVMNode.auto_start` replaces `_tailscale_rejoin_required` and `_ensure_tailscale`
  (`vms/nodes.py:265-282`) with `_await_tailnet(..., already_running=False)` inside `vm_active`.
- `_ensure_tailscale`, `_tailscale_rejoin_required` and `_tailscale_logout` are deleted
  (`tailscale.py:69-191`), and `TailscaleWait` and `wait_for_reconnect`
  (`transports/__init__.py:248-318`) go with their last callers. `rejoin_tailscale` and
  `_join_tailscale` are deleted (`vms/initializer/credentials.py:43-99`), with their exports
  (`vms/initializer/__init__.py:34-35`, `vms/manager/__init__.py:26`); `verify_tailscale_available`
  stays.

`vm shell --platform` opens the same gate. On a stopped VM whose Tailscale is broken, the gate now
fails after starting it; the VM is left running, so the same command run again goes straight to the
native shell. This is accepted rather than given a second gate.

### `agw vm recover <name>`

`vms/manager/recover.py`, wired in `cli/commands/vm.py` as `vm rekey` is. In order:

1. `_require_vm`, `_guard_failed_vm(vm, allow_failed_init=True)` (a failed init is often what sends
   an operator here), `verify_tailscale_available()`.
2. The boundary, as rekey builds it (`power.py:421-441`): the VM-template node with the live VM
   node, one preflight and one resolve, so the auth key resolves up front with the site's secrets
   (Open question 4).
3. A status check: anything but `RUNNING` raises `StateError` naming `agw vm start NAME` (Open
   question 5). Then everything below runs inside `vm_node.hold_active()`.
4. **Trust.** With no store record: `platform.read_host_keys`, print each key's type and fingerprint
   with a line saying to compare them with the VM's console (a cloud's serial console or system
   log), `output.confirm` defaulting to no, then `trust.write_records` and `sync_ssh_config`. A
   refusal writes nothing and stops.
5. **Access repair.** Status 0 continues. Exit 3 or 4 stops with that cause; any other status shows
   the script's stderr tail; an unknown outcome shows `channel_tail`.
6. **Verify.** With an address, `reachability.wait(vm_access(...), wording=VERIFY)`; `True`
   finishes.
7. **Rejoin**, otherwise: record the parsed address (`db.update_vm_tailscale`), `sync_ssh_config`,
   then `reachability.wait(..., wording=RECONNECT)`.

Wherever recover stops for a failed or unknown recovery step, the message adds, when
`recovery_needs_guest_sshd` is true, that this platform's recovery goes through the VM's SSH server,
and the hint is R7's: repair the machine through the provider console or recreate it.

### `vm rekey`

The boundary and the running check stay (`power.py:401-464`). The native transport and the per-step
commands (`:480-528`) become one `run_recovery(REKEY, secret=key, deadline=180)` under the existing
gate. Status 0 with a parsed address records it, syncs the SSH config and writes the `rekey` event
as today (`:532-534`), then `wait_for_share` and
`reachability.wait(vm_access(updated row), wording=RECONNECT)`, which replaces retargeting a legacy
transport in place (`:544-550`). A non-zero status raises `StateError` with the stderr tail; unknown
raises `ConnectivityError` naming `agw vm recover`, since the new address was never read. The
per-step progress lines become one line before the call ("about a minute").

### `vm delete`'s logout

Inside the existing best-effort hold (`power.py:278-310`), `_tailscale_logout` becomes
`run_recovery(LOGOUT, deadline=30)`, and the result is reported as R7 states, never checked over
SSH:

- status 0: "Tailscale node HOSTNAME logged out";
- non-zero: a warning with the status and the admin-console removal pointer naming `vm.hostname`;
- unknown, or a raise: a warning that the logout could not be confirmed, with the same pointer;
- `TrustMissing` (a cloud VM with no record, raised before any route opens): a warning that the node
  was not logged out because the VM's host key was never recorded, naming the node. This is Open
  question 1's answer.

The logout moves into its own `try` after the session teardown's, so a teardown failure (its legacy
transport raises `TrustMissing` too) no longer skips a logout that a native channel could do.
`UserAbort` still propagates from both (`:307-308`), and the backend delete still follows regardless
(I33). "Logging out of Tailscale..." prints before the call and the result after, so a cloud's route
lines fall between them in the order things happened, which is what #350 fixed (`ebb2e6fdf`).

### `vm shell --platform`

`shell_vm` (`vms/manager/exec.py:119-129`, `:163-178`) checks for the override right after resolving
the site, before the boundary, and raises `FeatureUnavailable` with the platform's hint when there
is none. Otherwise, inside `gated_vm_platform_recovery_boundary` with no environment targets, it
runs `platform.native_shell(vm, ops_ctx)` under `guarded_terminal`, sets `clean_exit` on a zero
status, and after the guard prints the 255 notice (I6) in a form that does not claim the cause: "the
native shell ended with status 255 (the connection may have dropped); terminal restored". The
environment is not composed and its secrets are not resolved (R7: the native shell does not seed
it), and `--workspace` with `--platform` is refused before anything else (Open question 2).
`platform_shell.py` is deleted.

## Retiring the lazy repair-secret protocol

The rejoin was the protocol's only user, so it goes whole:

- `GateTarget.repair_secret_refs` and `_GateSecrets` (`orchestration/activation.py:96-107`,
  `:141-182`); `ensure_active` hands `auto_start` the same `ScopedSecrets` it hands
  `observed_stopped` (`:201-202`). The module docstring's repair half (`:36-62`) is rewritten.
- `LiveVMNode._repair_refs` and `repair_secret_refs` (`vms/nodes.py:136`, `:187-204`).
- The batch boundary's per-target late branch (`sessions/manager/_scope.py:421-453`): the gate
  callback is `resolver.get` for every target, which refuses any name outside the union, and the
  docstring's exception (`:361-375`) goes.
- `Resolver.resolve_late_repair` (`secrets/resolver.py:217-225`), now without a caller.
- The `rekey_vm` gate comment that cites the repair key (`power.py:473-477`).

## Error mapping

| Situation                                              | Error                                                             | Hint or effect                                                                |
| ------------------------------------------------------ | ----------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| No native shell on the platform                        | `FeatureUnavailable` (`StateError`, new in `execution/errors.py`) | The platform's `native_shell_unavailable_hint`, else the provider's console   |
| Start or gate: no answer after the wait, or no address | `ConnectivityError`                                               | `agw vm recover NAME`                                                         |
| Recover on a VM that is not running                    | `StateError`                                                      | `agw vm start NAME`                                                           |
| Recovery step: non-zero status                         | `StateError`, with the stderr tail                                | Repair or recreate (R7)                                                       |
| Recovery step: unknown, or the platform raised         | `ConnectivityError`, with `channel_tail`, or the raise            | The sshd note where it applies; never re-dispatched                           |
| Provisioning join or bootstrap: not status 0           | `ProvisioningError`, inside the platform's rollback               | An unknown join names the node                                                |
| A cloud VM with no store record                        | `TrustMissing` from `run_as_root`, before the route               | Recover re-establishes; delete warns naming the node                          |
| `python3` missing                                      | `VMNotReady`                                                      | Now `agw vm recover NAME`, replacing the slice 2 reinit hint (`errors.py:77`) |
| No store record                                        | `TrustMissing`                                                    | Import, or `agw vm recover NAME` (`errors.py:37-40`)                          |

No error holds the script, the secret or a stream that was not redacted; `RootScriptResult`'s
streams and tail are redacted before anything can quote them.

## Independence

- **Package guard.** `test_independence.py` walks only `agentworks.execution`, so it gains explicit
  imports of `agentworks.capabilities.vm_platform.public_ssh` and `agentworks.vms.recovery`, as
  slice 3 adds `agentworks.vms.contract`. Importing `public_ssh` runs
  `capabilities/vm_platform/__init__.py` (`:30-31`), so the guard proves `lima.py`, `lima_host.py`,
  `wsl2.py`, `wsl2_bootstrap.py`, `tailscale_join.py` and `bootstrap_script.py` legacy-free
  transitively. Measured at the baseline: the other modules those import (`capabilities.base`, `db`,
  `debian`, `schema`, `topics`, `subprocess_io`, `secrets.line_safety`, `execution`) load no legacy
  module.
- **Static check.** The plugins cannot pass the import guard: importing any of them loads the plugin
  index (`plugins/__init__.py:26-35`), which loads the harness plugins and `native_files`
  (measured), until slice 5. They join `MIGRATED_FILES` instead
  (`tests/test_execution_import_boundary.py:24`): the four plugin `platform.py` files,
  `plugins/proxmox/guest_agent.py`, `vms/manager/recover.py`, `vms/manager/tailscale.py`,
  `vms/nodes.py`, `orchestration/activation.py` and `vms/initializer/credentials.py`. `power.py`
  cannot (`LOG_DIR`, the session teardown), nor can `_scope.py` (`SSHLogger`, `:29-30`); both call
  new code, which R10 allows.
- **Deleted with no caller left.** `transports/lima.py`, `remote_lima.py`, `wsl2.py` and
  `_shared.py` (their only users are the platforms and `transports/__init__.py`'s exports), the
  `native_transport` factory, `plugins/proxmox/transport.py`, and their tests. Slice 8's list loses
  the Proxmox module.

## Tests

None restates our prose or the implementation.

**Unit.**

- **The wrapper.** `root_script.run` over fabricated `RawRun`s: both closing markers give the status
  (including 255); a missing `end`, a missing trailer, a malformed trailer or no start marker give
  `None`; bytes outside the markers never reach `stdout` or `stderr`; the secret is absent from both
  streams, `channel_tail`, `repr` and any raised error; the bytes passed to the runner hold the
  payload and the secret, and the runner is called exactly once even when the result is unknown or
  it raises.
- **`run_process`** against a local `/bin/bash -s`: the deadline returns at the deadline with the
  process reaped; a 2 MiB writer stops at the limit; a background child holding stdout does not
  stall it; `ClientUnsupported` names a program that is missing.
- **Host policy.** `ssh -G` against the installed client with an operator configuration that sets
  `ForwardAgent yes`, `ForwardX11 yes` and `RequestTTY force`: the pins win and the configuration's
  other settings still apply; a destination with whitespace is refused.
- **Platform surface.** Registration refuses a vm-platform class without `recovery_needs_guest_sshd`
  (the existing `required_attributes` mechanism; this replaces the plan's "each platform declares
  sshd dependence", which would restate six constants); `read_host_keys`' default goes through
  `run_as_root`; `trust.read_host_keys` drops malformed lines and refuses an empty result.
- **Provisioning (I10).** Each of the six creates, with its channel faked at `run_process`,
  `ssh_process.spawn` or the QGA client: the auth key appears in no argv, no progress-sink call, no
  exception's message, `repr` or chain, and no file write, and appears in exactly one root-runner
  `script` (the join's). The join runs once when the address reads fail, and once when it is
  unknown. This extends `tests/test_cloud_bootstrap_secret_boundaries.py`.
- **Commands** (fakes for the platform, the database and `VMAccess`):
  - start and the gate, on a VM that does not answer: the recover hint, no auth key resolved, no
    recovery script dispatched;
  - recover: keys written only after confirmation, then the SSH config synced; exits 3 and 4 stop;
    no rejoin when the VM answers after repair, exactly one when it does not; a stopped VM refused
    before any script;
  - rekey: one script and the address recorded; an unknown outcome names recover;
  - delete: each logout outcome reported with no `VMAccess` call after it; `TrustMissing` warns
    naming the node; the backend delete runs in every case, a teardown failure included;
  - `vm shell --platform`: Proxmox refuses before the boundary resolves anything; `--workspace` is
    refused; a 255 prints the notice after the guard.
- **Retired protocol.** The batch gate refuses a name outside the union (the existing test of the
  undeclared-name refusal stays, without its repair exception).

**Conformance vectors** for the channel, added to the execution LLD's list. **L** runs
`run_process(["/bin/bash", "-s"], ...)` locally on every Linux CI host, and under `sudo -n` where
the runner grants it; **V** runs through each platform's real `run_as_root` from the tester's
workstations.

| Vector                                                                                                       | Lanes |
| ------------------------------------------------------------------------------------------------------------ | ----- |
| W1 A payload that reads stdin gets end of file; a 200 KiB script runs whole                                  | L V   |
| W2 A script cut short anywhere runs nothing, not even the start marker                                       | L     |
| W3 Bytes after the group never run                                                                           | L     |
| W4 The secret reaches the payload, not its children's environment, and the guest argv is `/bin/bash -s`      | L V   |
| W5 Closing the channel mid-payload leaves the payload to finish its effect, and the outcome reads unknown    | L V   |
| W6 Status 0, 1 and 255 from the payload come back as themselves; the channel's own status is never consulted | L V   |
| W7 A Debian 12 and 13 guest runs each core script to status 0 (access repair with python3 absent)            | V     |

W5's V cell is a delete and a rekey on Azure, the platform where the drop was seen (`21bb259f9`).

**Existing tests that change.** The `native_transport` fakes (`tests/test_vm_shell_platform.py`,
`test_wsl2_keepalive.py`, `plugins/_fixtures.py`, `vms/test_tailscale_logout_wording.py`) and the
hint and route uses move to the new members; the staging tests
(`plugins/test_proxmox_bootstrap_staging.py`, `vms/test_bootstrap_file_lifecycle.py`) give way to
the root-runner bootstrap tests; the deleted modules' tests go; `test_bootstrap_script.py` loses its
key cases; every vm-platform fake implements `run_as_root` and declares `recovery_needs_guest_sshd`.

## Live evidence

The plan's cells, with what each shows (R11). Every platform from at least one workstation, Windows
among the clouds and against WSL2, and Remote Lima from macOS and Windows (the host policy on
Win32-OpenSSH):

- create on every platform: the key-free bootstrap on WSL2 and Proxmox, the join over the root
  runner, and on one cloud the admin console showing the key used once (I10, I24);
- rekey, delete's logout and `vm recover` on every platform, and delete of a cloud VM with no
  record;
- `vm start` after the node is removed in the admin console: the recover hint, then recover rejoins;
- a broken sshd on WSL2 and on Proxmox (`ssh.service` masked and stopped), repaired by recover;
- a lost store record on one cloud and on Remote Lima, re-established against the console;
- a pre-python3 VM (WSL2's root filesystem) recovered, then reinitialized;
- `vm shell --platform` wherever it exists, and Proxmox's refusal;
- vectors W1 and W4 to W7 on each platform's channel.

## Incident audit

The entry points this slice moves are the native transport factory, each platform's create and
native access, `vm start`, the gate's auto-start, rekey, delete's logout, `vm shell --platform` and
the batch gate. The audit read their history, including `21bb259f9` (logout under nohup on Azure),
`fae1d7dd2` and `cdd7a0045` (the rekey sequence), `ebb2e6fdf` (#350, logout transcript order),
`aa8a5540f` (#251, start wording), `102adcff5` (the batch late-resolve guard) and `caca2709f`
(key-free provider payloads). Rows I10, I17 and I18 are already slice 4's. New rows for the HLA
inventory:

| Row | Behavior                                                                                                                                                                                                 | Re-proved in |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ |
| I29 | Rekey restarts `tailscaled` before the logout and after the join, with 15-second pauses, because a stale daemon hangs the logout and a rekeyed node can be unreachable (`cdd7a0045`, `power.py:489-518`) | 4            |
| I30 | A logout or rekey whose channel drops mid-command still finishes on the guest (`21bb259f9`, `fae1d7dd2`; `tailscale.py:177-181`)                                                                         | 4            |
| I31 | A rejoin or rekey key never reaches an operation log (`tailscale.py:108-118`)                                                                                                                            | 4            |
| I32 | A cloud VM whose provider reports no public address fails with a typed error naming the console, not a connection to an empty host (#118; `transports/__init__.py:215-228`)                              | 4            |
| I33 | A failing logout never skips the backend delete, and an operator abort still keeps the row (#329; `power.py:268-310`)                                                                                    | 4            |

Re-proofs: I10 by the provisioning test and create cells; I17 by `wait_until_answering`'s tests and
the Azure cells; I18 and I33 by the delete tests; I29 by the rekey cells; I30 by vector W5; I31
structurally, since the package guard keeps `SSHLogger` out of `root_script.py` and
`vms/recovery.py`; I32 by the clouds' tests. #251's wording is I15's, re-proved for start; #350's
order changes shape as described under delete; `102adcff5`'s guard becomes the plain union refusal.

## Collateral

- **ADR amendments** (status blocks): ADR 0003's "automatic rejoin on start" (`:36-37`) and ADR
  0019's conditional late acquisition of the rejoin key (`:102-106`) no longer hold; both get an
  "Amended" paragraph.
- **Platform author documentation:** `capabilities/vm_platform/README.md` and `plugins/README.md`
  state the run_as_root contract once, with the other members; the kind's prose (`kinds.py:53-77`)
  and the `TopicProse` that describes a native transport (`lima.py:173-189`,
  `plugins/proxmox/platform.py:128-153`) follow.
- **Command reference** (`cli/command-reference.md`): a `vm recover` section and table row;
  `vm start` no longer rejoins; `vm rekey`; `vm shell --platform` (`:753-769`) without the
  `--workspace` combination and the native transport wording; the reinit paragraph that sends a lost
  key to `vm shell --platform` (`:700`) and the exec paragraph that names reinit for python3
  (`:743`), both now `agw vm recover`.
- **Completions** (`completions/spec.py:225-231`): `("vm.recover", "name"): "vms"`.
- **Guides:** `docs/guides/proxmox.md:201-205` (no start-time rejoin); the troubleshooting topic
  (`guide/guide-content/troubleshooting.md:34-41`), naming recover; an operator section on recovery
  beside the Tailscale concept topic's rekey; `docs/guides/resources.md:805-808` and
  `orchestration/README.md:97-98` (no lazy exception); `docs/guides/upgrading-to-0.20.md`.
- **Reviewer persona:** `.rulesync/subagents/agentworks-reviewer.md:191-194` loses the sanctioned
  conditional-repair exception; regenerate with rulesync.
- **`cli/agentworks/execution/README.md`:** `root_script`, the host policy, `FeatureUnavailable`.
- **Comments:** `vms/manager/inspect.py:382-388` names `_ensure_tailscale` and `_tailscale_logout`.
- **The commit** carries `BREAKING CHANGE:` footers (`CONTRIBUTING.md:69`) for the platform
  contract, `vm start` no longer rejoining, and `--platform --workspace`.

## Open questions

1. **Delete's logout of a never-enrolled cloud VM under Unverified trust.** Recommendation: no.
   Delete detects the missing record (`TrustMissing` from the cloud's `run_as_root`, before any
   firewall change) and warns naming the node for the admin console. Evidence:
   - The logout carries no secret, and publickey authentication binds its signature to the session,
     so an impostor at the address gains nothing, but it could report a logout that never happened.
   - R8 never widens trust silently, and D9 keeps Unverified to provisioning and the confirmed key
     read; a delete-only exception is a second trust branch in every cloud's `run_as_root`.
   - The window is narrow: slice 3 logs out over the still-pinned provisioning channel when the
     address reads fail inside `create` (contract-lld, "One mechanism for all six platforms"). What
     remains is a Phase A failure after `create` returned, retained `FAILED`, whose node goes
     offline with the instance and is named by the warning.
   - Only the clouds are affected; the other channels do not use the store.
2. **`vm shell --platform --workspace`.** Today it `cd`s into the workspace and is documented
   (`command-reference.md:753-756`). `native_shell(vm, ctx)` takes no directory, and R7 keeps the
   native shell out of the operation's setup. Recommendation: refuse the combination, with release
   notes; the native shell is a repair hatch.
3. **Provider-side host-key fingerprints.** Recommendation: none in this slice. R7 requires only the
   operator's confirmation. EC2 console output, Azure boot diagnostics and GCE guest attributes or
   serial output may carry fingerprints, but whether each does for these images, and for how long
   after first boot, is unmeasured, and GCE's needs metadata Agentworks does not set. The prompt
   says to compare with the console; a later effort can add a source with a measured one.
4. **The rejoin key in recover's boundary.** Resolving it only when a rejoin turns out to be needed
   would bring back late resolution after a mutation, the protocol this slice deletes.
   Recommendation: resolve it up front with the site's secrets, as rekey does; an unused key is not
   spent.
5. **Recover on a stopped VM.** Recommendation: refuse, naming `agw vm start`, which starts the VM
   before its wait fails. Starting it inside recover would add power-state handling to the one
   command meant to be simple to reason about.
6. **`wsl.exe` by bare name.** Every WSL2 call runs `wsl` through `PATH` (the `_wsl` helper,
   `capabilities/vm_platform/wsl2.py:239-248`, and the keepalive, `:410`), which on Windows also
   searches the current directory, D1's hazard for `ssh`. This slice adds two more such calls.
   Recommendation: file it for the WSL2 owner with D1 as the model; resolving it here would change a
   shared helper outside this slice's contract.

## Amendments

For the lead to apply. Each points at the section above that holds the detail.

- **HLA Components.** Add `execution/root_script.py` and `capabilities/vm_platform/lima_host.py`;
  `vms/recovery.py` holds `run_recovery` and the scripts, and `vm recover` is
  `vms/manager/recover.py` (Module layout).
- **HLA D2.** The host policy's options, modes and destination (The host policy).
- **HLA D9, Re-establishment.** `read_host_keys` returns `tuple[HostKey, ...]` from a base-class
  default, not core's override detection; the script and parser are in `trust.py`; the clouds read
  under a `provisioning_binding` file; no provider fingerprints (Open question 3).
- **HLA D11.**
  - Table: `run_as_root(vm, ctx, script, deadline) -> RawRun` with no separate stdin, running
    `/bin/bash -s`; `RawRun` has no status; `native_shell_unavailable_hint` stays as guidance;
    `run_as_root` and `recovery_needs_guest_sshd` are descriptor requirements;
    `ProvisionRequest.ssh_private_key` is `Path`.
  - Prose: the wrapper is in `execution/root_script.py`, is shared with provisioning, ignores
    `SIGHUP` and `SIGPIPE` and embeds the auth key; the cloud channel's remote command is the
    constant `sudo -n /bin/bash -s`; access repair loses "reloads sshd on every platform" and
    "prints the public host keys" and repairs sshd by unmask, enable, configuration test and
    restart; rejoin no longer installs Tailscale; recover refuses a VM that is not running and
    resolves the key in its boundary; delete does not log out a cloud VM with no record and names
    the node; the per-platform table takes The channels' rows.
- **HLA D13.** `FeatureUnavailable` lands with `vm shell --platform`.
- **HLA D14.** The guard imports `public_ssh` and `vms.recovery` explicitly; the plugins join the
  static check instead.
- **HLA Migration, slice 4.** WSL2 and Proxmox run a key-free bootstrap through the root runner, and
  the legacy platform transports and `plugins/proxmox/transport.py` are deleted.
- **HLA Incident inventory** gains I29 to I33; **ADR amendments** gains 0003 and 0019 in slice 4.
- **Plan slice 4.** Link this LLD. Add `root_script.py`, `lima_host.py` and `FeatureUnavailable` to
  the implementation items; replace the "each platform declares sshd dependence" test with the
  registration refusal and add this LLD's tests and vectors W1 to W7; add the deletions under
  Independence, `Resolver.resolve_late_repair`, the bootstrap generator's key parameter and step 7,
  `TAILSCALE_JOIN_STDIN_COMMAND` and the staging files, and drop `native_shell_unavailable_hint`
  from them; add the Collateral items and Live evidence cells the plan does not list.
- **Plan slice 8.** `agentworks.plugins.proxmox.transport` is gone already.
- **contract-lld.md** (same PR). `settle_tailnet_address(root, *, node)` replaces the callback form
  and `settle_on_transport`; bootstrap step 7 is deleted rather than shortened.
- **trust-lld.md.** Delete's warning for a cloud VM with no record names the node and opens no
  route; re-establishment is this LLD's.
- **execution-lld.md.** The host policy and `FeatureUnavailable` land here; `PipeSession` accepts
  `nonce=None`; `spawn`'s refusal names the program.
- **FRD.** None: R7's "finite sensitive stdin" is the script that carries the key.
