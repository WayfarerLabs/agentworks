# Transport Take 2: Platform Low-Level Design (Slice 4)

- Status: Implemented in PR B, with the live evidence the plan lists still open.
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
answer over SSH; `agw vm recover` runs one repair script, which joins Tailscale only when the node
needs a login, then the address settle create uses, then one wait.

**Operator direction applied.** Where detecting and alerting is enough, no mechanism: a
never-enrolled cloud VM is not logged out at delete, a missing Tailscale is reported rather than
installed, and a broken sshd is restarted, not rebuilt. Every type below has a caller in this slice.

**Out of this slice.** Phase B stays legacy until slice 6. `LOG_DIR` stays until slice 8.
`transports/__init__.py`, `transports/base.py` and `transports/ssh.py` keep their legacy callers
until slice 8; this slice deletes only what it leaves with no caller.

**In-PR order.** The `run_as_root` implementations and `root_script.py` land before slice 3's
address settle, so slice 3's `settle_on_transport` and its callback form are never written
(Amendments).

## Module layout

| Module                                                            | Contents after this slice                                                                                        |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `execution/root_script.py` (new)                                  | `RawRun`, `RootScriptResult`, `run`, `run_process`, `read_host_keys`                                             |
| `execution/pump.py`, `ssh_process.py`                             | `PipeSession(nonce=None)`; `build_host_argv`; `spawn`'s error for a program other than OpenSSH                   |
| `execution/trust.py`                                              | `fingerprint(key)`                                                                                               |
| `execution/errors.py`                                             | `FeatureUnavailable`; the `VMNotReady` and `TrustMissing` hints name `agw vm recover`                            |
| `capabilities/vm_platform/base.py`                                | The surface below                                                                                                |
| `capabilities/vm_platform/public_ssh.py` (new)                    | `PublicSSH`: the clouds' SSH to a public address, under the VM policy                                            |
| `capabilities/vm_platform/lima_host.py` (new)                     | `LimaHost`: limactl locally or on the placement host, its `run_as_root` and shell, Remote Lima's detached create |
| `capabilities/vm_platform/tailscale_join.py`                      | `JOIN_FRAGMENT`, `join_tailscale`, `read_tailnet_address`, `settle_tailnet_address`, `complete_cloud_bootstrap`  |
| `capabilities/vm_platform/bootstrap_script.py`                    | `run_bootstrap`, the key-free bootstrap run through a `run_as_root`; `parse_bootstrap_steps`                     |
| `capabilities/vm_platform/wsl2.py`                                | The `wsl.exe` `run_as_root` and shell                                                                            |
| `plugins/proxmox/guest_agent.py` (new)                            | `run_as_root`: QGA exec of `/bin/bash -s` with `input-data`, polled to the deadline                              |
| `vms/recovery.py` (new)                                           | Core's two scripts and one function each: `repair`, `rekey`                                                      |
| `vms/manager/recover.py` (new)                                    | `recover_vm`, the `agw vm recover` service                                                                       |
| `vms/manager/tailscale.py`, `power.py`, `exec.py`, `vms/nodes.py` | `_await_tailnet`; rekey and delete on the scripts; `vm shell --platform` on `native_shell`; the gate's wait      |

Deletions are listed under Independence and in each section.

## The root script channel

### What a platform implements

```text
class RawRun:            # frozen; both fields repr=False
    stdout: bytes
    stderr: bytes

VMPlatform.run_as_root(vm, ctx, script: bytes, deadline: float) -> RawRun   # abstract
```

Every helper that takes the callable names its parameter `run_as_root`, of type
`Callable[[bytes, float], RawRun]`; there is no second name for it. The contract a platform author
follows, in full:

- Run `/bin/bash -s` as root through the platform's channel, write `script` to its stdin, close it,
  and return everything the channel printed on each stream when it ends or `deadline` seconds pass.
  Stop waiting at the deadline; never claim the guest command stopped. A channel that reaches the
  guest through an HTTP API bounds each socket wait by what remains, so a response that keeps
  arriving can carry one request past the deadline.
- Run it once. Never retry, never parse the output, never report an exit code: `RawRun` has no field
  for one, because no channel's status is trusted (WSL passes raw wait statuses through, per the
  salvage message, and an ssh 255 cannot be told from a guest 255).
- Treat `script` as sensitive: it never reaches an argument, a log, an exception or a file.
- Raise an `AgentworksError` when the channel cannot be started or the provider call fails. Core
  treats a raise like an unknown outcome and never dispatches again.

The script and any secret travel together on stdin, so R7's "finite sensitive stdin" is the script
itself, and the HLA's separate `stdin` parameter goes. `/bin/bash` rather than `/bin/sh` because
WSL2's and Proxmox's bootstrap, which this slice moves onto the same primitive, is a Bash script
(`set -euo pipefail`, `bootstrap_script.py:62`), and Debian's dash has no `pipefail`. Bash is
Essential on Debian, and every channel below runs it.

### The wrapper and its result

`root_script.run(run_as_root, payload, *, deadline, secret=None, log=None) -> RootScriptResult`
builds the bytes, calls `run_as_root(script, deadline)` once, and reads the result:

```text
{
exec </dev/null
AGW_SECRET='<secret, shell-quoted>'            # only when secret is given
printf '\036<nonce> start\n'; printf '\036<nonce> start\n' >&2
(
<payload>
)
s=$?
printf '\036<nonce> end\n'; printf '\036<nonce> exit %d\n' "$s" >&2
}
```

Measured with Debian 12's `/bin/bash` 5.2.15, a local pipe standing in for the channel (vectors W1
and W3 repeat it on every channel; W2 runs locally only):

- **One brace group.** Bash parses the whole group before running any of it, so a payload command
  that reads stdin gets end of file from `exec </dev/null`, not script bytes. A truncated script, as
  a startup file that reads stdin would leave, is a syntax error and runs nothing, not even the
  start marker. Bash did not read past the group (dash did, which is one more reason the channels
  run Bash).
- **`AGW_SECRET` unexported.** The payload's subshell sees it and its child processes' environment
  does not, so it reaches a program only where a script hands it over. The one script that does is
  the join, which pipes it from the `printf` builtin into `tailscale up --auth-key=file:/dev/stdin`,
  so the key is in no process's argv on the guest either. Tailscale reads a `file:` value with
  `os.ReadFile` and trims it (`resolveValueFromFile` in `cmd/tailscale/cli/up.go`, added in
  `b822b5c79`, 2021-09-29); provisioning installs a current Tailscale. Today the guest `tailscale`
  process holds the key in its argv (`tailscale_join.py:15-17`, `lima.py:478-479`), which any
  account on the VM, an agent included, can read from `/proc/*/cmdline`; vector W3 proves the
  change. The value is validated line-safe first (`require_line_safe_secret`), then `shlex.quote`d.
- **Markers.** D4's grammar and a fresh `envelope.new_nonce()`, so `envelope.StreamFilter` reads
  them. The payload is core's own code, so the nonce in the script text needs no secrecy.

```text
class RootScriptResult:        # frozen
    status: int | None         # the payload's status; None when it cannot be established
    stdout: bytes              # payload bytes between the markers, secret redacted (repr=False)
    stderr: bytes
    channel_tail: str          # what the channel printed before the start marker, sanitized, redacted
```

`status` is `N` only when stdout closes with `end` and stderr with `exit N`; anything else, a
deadline, an output limit or a raise included, leaves no status (a raise propagates). `channel_tail`
is the last 20 lines of stderr before the start marker; when neither stream has a start marker,
stdout's bytes are included too, because `wsl.exe` can print its own errors there. NUL bytes are
dropped before decoding, as `_wsl` does for `wsl.exe`'s UTF-16LE
(`capabilities/vm_platform/wsl2.py:239-253`), and the text goes through `ssh_process.tail_lines`
(`ssh_process.py:231-240`). The secret is replaced by `[REDACTED]` in every field.

**The operation log.** `log` is slice 3's callable, `Callable[[RunRequest, Result], None]`, and
`root_script.run` records through it so `format_record` writes the block: the request is
`Script(payload, Shell.BASH)`, whose source `format_record` never writes, and the result is an
execution `Result` carrying the redacted streams, `channel_tail` as its diagnostics tail, and
`Completed(status)`, `Unknown(TRUNCATED)` after a start marker or `Unknown(CONNECTION)` without one.
The script's stderr reason (`sshd -t`'s output included) therefore lands in the log. Redaction holds
at both ends: `root_script.run` replaces the secret before the record exists, and the logger the
command passes is constructed with the auth key in its redaction set before its first write, as
slice 3 requires of every operation log (incident row I28). The commands below say who passes a log.

### The channels

Four channels are local processes and share `root_script.run_process(argv, script, deadline)`, built
on the pump rather than a second process loop. `PipeSession` (`execution/pump.py:163-219`) accepts
`nonce=None`, passing both streams through unfiltered, so a session ends when the process exits and
its pipes go quiet, at end of file, at the deadline, or when a stream passes its 1 MiB `Capture`
(`pump.py:37`, `:255-276`). A descendant holding a pipe never stalls it, and a deadline or limit
kills the process, leaving the outcome unknown. `ssh_process.spawn` (`ssh_process.py:259-272`) stays
the one place a process starts. Its `ClientUnsupported` stays OpenSSH-only: a program other than the
resolved client that cannot start raises `ConnectivityError` naming it, the precedent
`_ensure_limactl` set for a missing local tool (`lima.py:327-344`).

| Platform    | `argv` (stdin is the script)                                                                               | Bounds                                              | Needs guest sshd | Native shell                                 |
| ----------- | ---------------------------------------------------------------------------------------------------------- | --------------------------------------------------- | ---------------- | -------------------------------------------- |
| Lima, local | `limactl shell <instance> sudo -n /bin/bash -s`                                                            | `run_process`                                       | yes              | `limactl shell <instance>`                   |
| Remote Lima | host argv, `ssh -T <host> '$SHELL -lc "limactl shell <instance> sudo -n /bin/bash -s"'`                    | `run_process`                                       | yes              | host argv `ssh -t`, `limactl shell` inside   |
| WSL2        | `wsl --distribution <distro> --user root --exec setsid -w /bin/bash -s`                                    | `run_process`                                       | no               | `wsl --distribution <distro> --user <admin>` |
| Proxmox     | QGA `ping` until it answers, then `exec` of `/bin/bash -s` with `input-data`, then `exec-status` every 2 s | pinging and polling stop at the deadline; QGA's cap | no               | none (Proxmox web console)                   |
| Clouds      | VM policy argv, `ssh -T -l <admin> <public address> 'sudo -n /bin/bash -s'`, inside a route                | `run_process`                                       | yes              | VM policy `ssh -t`, inside a route           |

- Every channel already carries stdin byte-exact in production: Lima's join over `limactl shell`
  (`lima.py:120`, `:490-493`), WSL2's and Proxmox's `input_text` (`transports/wsl2.py:90-95`,
  `plugins/proxmox/transport.py:74-80`), and the clouds' join over `SSHTransport`
  (`tailscale_join.py:43-48`). No argv carries request data, so D2's "script content out of
  arguments" holds on every channel.
- WSL2 uses `--exec`, so root's login shell is not between `wsl.exe` and Bash. The program name is a
  literal today (`wsl2.py:247-248`, and `transports/wsl2.py:139`, which goes); this slice introduces
  the constant `_WSL` in `wsl2.py`, used by `_wsl` and the two new calls, and absolute on Windows
  (below).
- Proxmox: QGA runs commands as root (`plugins/proxmox/transport.py:3-4`). `input-data` keeps the
  65,536-character guard (`transport.py:24`). The largest script is the bootstrap, 14,359 bytes as
  generated at the baseline. A truncated QGA report raises `_TruncatedQGAOutput`
  (`plugins/proxmox/api.py`), which the channel reports as a script that exited with its output cut
  short. At the deadline the result is empty, hence unknown.

### A channel that drops

`trap '' HUP PIPE` is not in the wrapper; it is set only by the two scripts whose own commands can
take the network away: rekey (below) and the one logout, `tailscale_join.LOGOUT_SCRIPT`. Measured
locally with Bash 5.2.15: with the channel's read ends closed mid-payload, a payload that then wrote
died of `SIGPIPE` and its effect never happened. The trap alone is not enough under `set -e`: the
next write fails with `EPIPE`, and `set -e` ends the script before its remaining steps. So those two
scripts write nothing until they are done. Each step runs as `out=$(command 2>&1) || s=$?`, since a
bare `out=$(...)` under `set -e` exits on failure, and its output is appended to one buffer. The
script stops at the first failed step and prints the buffer once, at the very end, with `|| true`,
then exits with that step's status. Measured: a rekey-shaped script whose channel closed after its
first step still ran its later steps; the same script printing after each step stopped before the
join. No external program, Tailscale's Go binary included, writes to the channel at all. This
replaces the `nohup ... &` delete uses today because "logout was disrupting the network stack and
thus not completing" on Azure (`21bb259f9`, `vms/manager/tailscale.py:177-181`), and keeps the
logout's status observable when the channel survives. Other scripts keep default dispositions: the
bootstrap's `curl | sh` (`bootstrap_script.py:232`) relies on them.

What holds depends on the channel (incident row I30):

- **SSH channels** (Lima's `limactl shell`, Remote Lima, the clouds): the guarantee above, vector
  W4.
- **QGA**: there is no connection to drop; the guest agent runs the process to its end whatever the
  API client does, and a lost status poll is an unknown outcome.
- **WSL2**: the channel is local, so it ends early only at the deadline or an operator interrupt.
  When `wsl.exe` exits, systemd's `KillUserProcesses` reaped a backgrounded bootstrap (`1ae933a88`),
  so the payload may be killed rather than finish, though the hangup of `wsl.exe`'s terminal reaches
  only `setsid`'s session, not the payload's; vector W7 records which. The scripts that need the
  guarantee do not depend on it here, since nothing on WSL2 drops a channel.

## The host policy and `public_ssh.py`

**The host policy serves Remote Lima only.**
`ssh_process.build_host_argv(client, host, mode, remote_command)` builds the placement host's argv:
no `-F none`, since a host connection honors operator configuration (ruled 2026-10-23);
`trust.ssh_options(HOST_TRUST)` (`execution/trust.py:314-344`); `ConnectTimeout=30` and the VM
policy's keepalives; `BatchMode=yes` in pipe mode only, since today's interactive hop allows prompts
(`transports/remote_lima.py:135-142`); `-T` with a remote command or `-t` for the shell
(`ArgvMode.PIPE`, `TERMINAL`); and the host after `--`. The resolved client is required, so Remote
Lima from Windows gets D1's resolution. There is no policy type: the trust is fixed and there is no
identity. The host string is operator-authored, so `LimaSshPlacement.host` (`lima.py:145`) refuses
whitespace and control characters at the config boundary.

**The clouds use the VM policy.** `PublicSSH(host, user, identity_file, trust)` builds its argv with
`build_argv` (`ssh_process.py:100-192`) from `VMPolicy(identity_file, trust)`,
`Destination(host, user)`, `ArgvMode.PIPE`, the remote command `sudo -n /bin/bash -s` and the client
log, with `trust` the VM binding or an Unverified provisioning binding. `-F none` already keeps
agent and X11 forwarding off and connection sharing out, so no unverified master can be reused, and
`Destination` validates the provider-reported address and the admin name. FRD R8 lets a
public-address connection honor operator configuration (`frd.md:507-509`) but does not require it.
The price: an operator's `ssh_config` stanza for a cloud address (a `ProxyJump` for a network that
blocks port 22, say) stops applying to recovery and provisioning, and an encrypted key must be in
the agent, as for every VM connection.

- `run_as_root(script, deadline) -> RawRun` is `run_process` over that argv. The remote command is a
  constant that the admin's login shell (one of the four D4 allows) parses unchanged. After a run
  whose stderr holds no start marker (ssh's log also holds the server's banner, and a script that
  started has connected and logged in), the client log's `Host key verification failed` raises
  `TrustMismatch`, and its `Permission denied (publickey` raises `KeyRefused`, a `ConnectivityError`
  whose hint names the provider console: recover logs in with the same key it would repair, so a
  cloud VM that refuses it needs the provider's own access, such as its serial console, or
  recreation. When the key is encrypted and the agent does not hold it, the hint leads with the
  passphrase hint from `client.agent_load_hint`, the check `VMAccess.passphrase_hint` makes, since
  it is given only when it is the likely cause. Its `Connection refused`
  (`ssh_process.CONNECTION_REFUSED`) raises `_ConnectionRefused`, a `ConnectivityError`: the address
  answered and nothing listens on the SSH port, so nothing ran.
- `wait_until_answering(*, seconds=PROBE_WAIT_SECONDS, retry_refused_key=False)` probes with
  `root_script.run(self.run_as_root, "true", deadline=10)` three seconds apart until one completes
  or `seconds` (120 s by default) have passed, then raises `root_script.ChannelUnreachable`, a
  `ConnectivityError` saying the channel's own bounded wait found no way to the VM, so callers stop
  at the first one instead of retrying it. Its hint names a restart (`agw vm stop` and
  `agw vm start`) or the provider's serial console. A `_ConnectionRefused` probe is waited out like
  no answer, since a booting VM refuses too; when the last probe was refused, the hint says the VM's
  SSH server is likely not running, and nothing more. When no probe got an answer, the wait first
  warns `PROBE_FAILURE_HINT`, the module's `operator.ssh_allow_cidrs` advice, and its hint names the
  restart for a VM that has hung: an address that refuses is one the allow already lets through, so
  only silence points at the allow. It replaces today's propagation wait after a fresh firewall
  allow (`transports/__init__.py:230-244`, incident row I17) and also covers a VM that has just
  started: a cloud's `start` returns at the provider's state, before sshd runs, and a booting sshd
  refuses fast, so the bound is time rather than a count of probes. `true` reads nothing, so
  retrying it is allowed. A key refusal (`KeyRefused`, a `ConnectivityError`) or `TrustMismatch`
  raises at once: on an enrolled VM, waiting cannot change either. Create passes
  `retry_refused_key=True`, since a cloud's guest agent may install the key after sshd starts; after
  a refusal the next probe waits 10 s (`REFUSED_KEY_INTERVAL_SECONDS`), so about 60 failed logins
  rather than 200 do not trip sshd's `PerSourcePenalties` (Debian 13), and a refusal still standing
  at the bound then raises itself, with no firewall warning. So does a probe with no start marker
  whose channel tail has a line starting `sudo:` (sudo's prefix when `-n` refuses), which
  `root_script.run` otherwise reports as an unknown outcome: it raises `SudoRefused`, a `StateError`
  saying the VM answered but its admin lacks passwordless sudo, rather than retrying and warning the
  firewall hint.
- `shell() -> int` runs the `TERMINAL` argv with no remote command (a login shell) and returns its
  status.

## The platform surface

Changes to `capabilities/vm_platform/base.py`, under `contract_version` 2 (slice 3's bump):

| Member                                                      | After this slice                                                                                                                                                                                                         |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `run_as_root(vm, ctx, script, deadline)`                    | New, abstract, and in the descriptor's `required_operations` (`capabilities/vm_platform/kinds.py:138`)                                                                                                                   |
| `recovery_needs_guest_sshd: ClassVar[bool]`                 | New, annotation only, in `required_attributes` (`kinds.py:140`); the generic mechanism refuses a platform without it (`capabilities/conformance.py:152`)                                                                 |
| `read_host_keys(vm, ctx) -> tuple[HostKey, ...]`            | New with a default, `root_script.read_host_keys(partial(self.run_as_root, vm, ctx))`; the clouds override it                                                                                                             |
| `native_shell(vm, ctx) -> int`                              | New. The default raises `FeatureUnavailable`; a platform offers a shell by overriding it, and core checks first with `require_native_shell`, which compares `type(platform).native_shell` with `VMPlatform.native_shell` |
| `native_shell_unavailable_hint`                             | Kept, as guidance text only (Proxmox's console pointer, `plugins/proxmox/platform.py:154-158`)                                                                                                                           |
| `native_transport`, `transient_route`, `probe_failure_hint` | Deleted (`base.py:335`, `:408`, `:203`). The clouds keep a private `_transient_route(vm, ctx)` reading `ctx.config`, abstract on `PublicAddressPlatform`; the probe's firewall advice is `public_ssh.PROBE_FAILURE_HINT` |
| `ProvisionRequest.ssh_private_key`                          | `Path`, not `Path \| None`: create always passes `config.operator.ssh_private_key` (`vms/manager/lifecycle.py:510`), a required `Path` (`config/models.py:29`)                                                           |

The docstrings that present the route as a public hook (`:355-375`, `:377-406`, `:433-460`) are
rewritten to match. `root_script.read_host_keys(run_as_root)` runs `cat /etc/ssh/ssh_host_*_key.pub`
(30 s) and keeps each line `trust.is_valid_key` accepts (`trust.py:189`), since guest output is a
boundary; an unknown or non-zero outcome, or no valid key, raises. It lives in `root_script.py`
rather than `trust.py` because `trust` sits below `ssh_process`, which the pump imports
(`ssh_process.py:23`), so `root_script` imports `trust` and never the reverse. `trust.fingerprint`
is OpenSSH's `SHA256:` form.

## Platforms' private access and provisioning

### `tailscale_join.py`

Today it takes an `ExecTransport` and raises `SSHError` (`tailscale_join.py:10`, `:13`). It now
takes a `run_as_root`:

- `JOIN_FRAGMENT` is the one join command,
  `printf '%s\n' "$AGW_SECRET" | tailscale up --auth-key=file:/dev/stdin`, used by `join_tailscale`
  and by recover's repair script, so create and recover join the same way.
- `join_tailscale(run_as_root, auth_key, *, node)` runs it once with a 30-second deadline, as today
  (`tailscale_join.py:84`). A non-zero status raises `ProvisioningError` quoting the last five lines
  of `tailscale up`'s stderr, its own reason (an invalid auth key, say), which `root_script.run` has
  already redacted of the key and `tail_lines` neutralized for the terminal; create's operation log
  records it as the create's outcome. An unknown one, or a channel that raised `ConnectivityError`,
  raises naming `node`, with the admin console in its hint, since it may have joined. Neither
  retries (I24). An empty or whitespace-only key never reaches it: create refuses one at its
  boundary, beside the line-safety check (`vms/manager/lifecycle.py`), since `tailscale up` would
  wait for an interactive login.
- `read_tailnet_address(run_as_root, *, node, log=None) -> str` is slice 3's read loop: up to six
  `tailscale ip -4` reads (15 s each) two seconds apart. A read fails on a non-zero status, an
  unknown outcome, a `ValueError` from `parse_tailnet_address`, or a `ConnectivityError` the
  platform raises, the channel's counterpart of today's `SSHError`; any other `AgentworksError`
  (`TrustMismatch`, `TrustMissing`, a provider's `StateError`) propagates at once, because a retry
  cannot change it, and so does `ChannelUnreachable`: its channel already waited about two minutes
  for the VM, so six reads would wait twelve (a dead public sshd on AWS made a rekey take 925 s).
  Exhaustion raises `ConnectivityError` naming the node. `log` records each read in the caller's
  operation log (recover and rekey pass theirs); recover's read alone, on exhaustion, adds a hint
  naming `agw vm logs NAME`.
- `settle_tailnet_address(run_as_root, *, node, rerun_hint, log=None) -> str` is that read plus the
  one logout, both recorded through `log`: on exhaustion it runs `log_out` once (30 s) and raises
  `ConnectivityError` from the read's error. When the logout returned status 0, the message says so
  and the hint is `rerun_hint`; when it ran and returned another status, the hint names the node for
  removal in the admin console, then gives `rerun_hint`; when it raised or its outcome is unknown,
  the hint is `rerun_hint` alone, since nothing shows the node is stuck. A `ChannelUnreachable` read
  propagates with `rerun_hint` added and no logout, which could not reach the VM either. Create
  passes `CREATE_RERUN_HINT`, which says the join spent the auth key, so a single-use key needs
  replacing before the create runs again (the rollback reports for itself whether the backend was
  removed), and rekey passes `agw vm recover NAME`: a node whose address cannot be read is unusable,
  and the next recover's repair reads `NeedsLogin` and rejoins it.
- `log_out(run_as_root, *, log=None) -> RootScriptResult` runs `LOGOUT_SCRIPT` once (30 s), the one
  logout for the settle and for `vm delete`. It is the trapped form in "A channel that drops":
  `trap '' HUP PIPE`, `out=$(tailscale logout 2>&1) || s=$?`, then `printf` the output with
  `|| true` and exit with `s`.
- `ConnectivityError` rather than slice 3's `ProvisioningError`, because it is true for all three
  callers (the VM did not report an address over its root channel). Create still reports a
  `ProvisioningError`: the manager wraps any untyped failure of `create` in one
  (`vms/manager/lifecycle.py:578-584`). That wrapper, and the AWS and Azure create arms'
  `wrap_ec2_error` and `wrap_azure_error`, keep the wrapped `AgentworksError`'s hint, so the
  settle's guidance (and `PublicSSH`'s key-refusal hint) reaches the operator.
- `complete_cloud_bootstrap(target, auth_key, *, node, readiness_command, readiness_label)` replaces
  `EphemeralTailscaleBootstrap`; `target` is the create's `PublicSSH`. Its readiness wait is the
  channel's own,
  `target.wait_until_answering(seconds=READINESS_WAIT_SECONDS, retry_refused_key=True)` (600 s), so
  `PublicSSH`'s `Permission denied (publickey` refusal is retried during create. That covers a guest
  agent that writes the key after sshd starts, as GCP's may (an inference, not measured). A
  `sudo -n` refusal or a host-key mismatch ends it at once; the refusal becomes a
  `ProvisioningError` saying the image's admin needs passwordless sudo, with the refusal's own VM
  advice left unchained, since the rollback removes the VM. When the wait runs out, the
  `ProvisioningError` carries the wait's error's hint (a standing refusal's `agent_load_hint` for an
  encrypted key not loaded in the agent), except a `ChannelUnreachable`'s, whose restart does not
  fit a VM the rollback is about to remove; a readiness command whose channel raises keeps that
  error's hint too. Then the readiness command runs once (600 s); the default
  `cloud-init status --wait --long` exiting 2 (`CLOUD_INIT_RECOVERABLE_STATUS`, finished with
  recoverable errors) warns and carries on, as Proxmox's wait does, while any other non-zero status,
  or 2 from another command such as GCP's marker wait, fails create. That error quotes the status
  (or that the outcome is unknown) and the last `READINESS_REASON_LINES` (12) lines the command
  printed, through `ssh_process.tail_lines`, since the rollback destroys the evidence and `--long`
  carries cloud-init's own detail and errors. Then the bootstrap joins and settles.
  `TAILSCALE_JOIN_STDIN_COMMAND` and the transport join leave `tailscale_join.py`; they stay in
  `vms/initializer/credentials.py` only for rekey and `vm start`'s rejoin, and go with them.

**The bootstrap script loses its key.** WSL2 and Proxmox are the last two platforms that generate
the bootstrap with the key inside it (`capabilities/vm_platform/wsl2_bootstrap.py:62-69`,
`plugins/proxmox/platform.py:401-408`) and stage it in a guest file that must be removed and
verified (`wsl2_bootstrap.py:71-151`, `plugins/proxmox/platform.py:566-624`). Both now run the
key-free script through `bootstrap_script.run_bootstrap(run_as_root, *, ...)`, whose keywords are
the bootstrap's inputs, its deadline and the progress sink, and then call `join_tailscale`, like
Lima and the clouds. `run_bootstrap` reports each step to the console and the create log, records
both streams' sizes there, and raises unless the script exits 0; `wsl2_bootstrap.py` goes.
`parse_bootstrap_steps(stdout)` replaces `parse_bootstrap_output` and `BootstrapResult`, since every
caller judges the outcome by its own status. `generate_bootstrap_script` loses its
`tailscale_auth_key` parameter and step 7 (`bootstrap_script.py:236-247`), so no generated script
can hold the key: I10 holds by construction, and the staging files go.

**The bootstrap runs once per VM.** Lima runs its system provisioners on every boot, so each
`vm start` ran the bootstrap again: it reset the admin's `.bashrc` and `.zshrc` to the seed
(dropping init's source lines and the operator's edits, against `skel.py`'s one-time policy),
appended the create-time key that `vm recover` or `vm reinit` had replaced, and ran
`apt-get dist-upgrade` with `--force-confnew`. GCE already wrapped the same payload in a durable
success marker because its `startup-script` runs on every boot too. That wrapper moves into
`bootstrap_script.run_once_script(script, *, stem)`, which keeps `<stem>.sh` and the marker
`<stem>.complete` (GCE's paths are unchanged) and refuses a payload holding its heredoc delimiter as
a line. Lima's provision step uses it with its own stem, `/var/lib/agentworks/lima-bootstrap`; the
payload itself is unchanged, and the platforms that run it once already (WSL2, Proxmox, cloud-init's
per-instance `runcmd` on AWS and Azure) do not wrap it. A Lima VM created before this keeps its old
provision step; the 0.20 upgrade guide says so.

**The bootstrap's output against the 1 MiB capture.** Its package steps run `apt-get -qq`
(`bootstrap_script.py:114-117`) and every other step prints one marker line. No create log exists
under this account's `~/.config/agentworks/logs` (`ssh.py:89`), and other accounts' logs on this
machine are not this lane's to read, so the size is unmeasured. The WSL2 and Proxmox create cells
record it; if it passes 256 KiB, the bootstrap's `apt-get` lines send stdout to `/dev/null`, as
WSL2's own package step does (`wsl2.py:682`), keeping stderr.

### Lima and Remote Lima

`lima_host.LimaHost` replaces `_run_lima` (`lima.py:270-311`), `_host_transport` (`:557-568`) and
the two legacy transports, and imports nothing legacy:

- `run(command, *, input=None, check=True, timeout=None) -> str` is today's `_run_lima`:
  `subprocess.run(shlex.split(command))` locally, or the host argv with `$SHELL -lc <command>`
  remotely, the login wrap that finds Homebrew's `limactl` (`transports/remote_lima.py:1-7`,
  `ssh.py:420-421`). It makes one attempt, as today: the remote branch passes `retries=1` or
  `SSH_DEFAULT_RETRIES` (`lima.py:293`), which is 1 (`ssh.py:289`). It raises
  `LimaCommandFailed(ExternalError)`, public in `lima_host.py` because `lima.py` catches it, where
  it raises `SSHError` today, and a command with input exposes neither stream (`lima.py:303-311`).
- `run_as_root(instance, script, deadline)` and `shell(instance) -> int` are the table's rows.
- `upload`, `run_detached` and `kill_detached` serve the remote create (`lima.py:577-669`), the pair
  copied from `remote_exec.py:47-213` with only the branch its one caller takes
  (`reuse_completed=False`, `quiet=True`, unelevated, `lima.py:623-631`). Its output goes to
  `request.progress` instead of a second `SSHLogger` (`lima.py:607-644`), so the create's own log
  holds it. Every check the poll and its closing reads make treats ssh's 255 as unanswered, whatever
  it printed, since none of them exits 255 by itself: the poll asks again at its next interval
  within the timeout, warning once that the host is not answering, and each read is asked up to six
  times. A launch the host does not answer, or a status it never answers for, raises
  `LimaCommandFailed` saying the host stopped answering and the outcome is unknown, never an exit
  status nothing read, and the create's rollback handles it as any other failure. The wrapper starts
  as its own process group's leader, and the kill the rollback, the poll's timeout and
  `kill_detached` share signals that group, so the `limactl` the wrapper started dies with it (a
  host bash that gave the job no group of its own gets the wrapper alone); it does so only while the
  PID file's process is still that run's wrapper, since the file can be days old by the time a later
  delete reads it. A Remote Lima `delete` stops, best effort and before deleting the instance, the
  detached create and removes the files a retained or abandoned rollback left on the host.

The remote create keeps its private staging directory (`lima.py:671-691`). Piping the template to
`limactl create -` would need `create` to be quick, and it is not: `createAction` calls
`instance.Prepare` (`cmd/limactl/start.go`), which downloads the base image when it is absent
(`fileutils.DownloadFile` in `pkg/instance/start.go`, Lima's main branch), so the create belongs in
the detached run with `start`. `create` joins with
`join_tailscale(partial(host.run_as_root, instance), key, node=...)` instead of its own stdin
command (`lima.py:471-493`), after the restart-sentinel probe and any `limactl restart`, so only the
address read sits between the join and the settle that logs a failed one out. Before the join, one
key-free root script waits up to 60 seconds for `systemctl is-active --quiet tailscaled`, since
`limactl start` and `restart` return once sshd answers, not once `tailscaled` is up; if it never
becomes active, create raises `ProvisioningError` inside the rollback with the key unsent. Only Lima
waits: the clouds' readiness loop already covers it, and WSL2 and Proxmox join right after their own
bootstrap installed and started `tailscaled`. The probe and the cloud-init log read stay `run`
commands. `native_transport` (`:799-808`) and `_transport_for` (`:535-539`) are deleted.
`recovery_needs_guest_sshd = True`: `limactl shell` is SSH to the guest's sshd with Lima's own key.
`start` turns a failed `limactl start`, which waits up to ten minutes for that sshd, into a
`ConnectivityError` naming the VM, with the `LimaCommandFailed` as its cause and a hint naming
`agw vm recover NAME`, which repairs sshd while Lima can still reach the VM.

### WSL2

`run_as_root` is the table's argv through `run_process`; `native_shell` is today's interactive argv
(`transports/wsl2.py:137-142`) as the admin. Both use `_WSL`, the program constant this slice
introduces for them and for `_wsl`. On Windows it is `%SystemRoot%\System32\wsl.exe` (`C:\Windows`
when `SystemRoot` is unset), so the process handed the auth key on stdin is never found by the
bare-name lookup D1 avoids for `ssh` (Windows searches the current directory); elsewhere, where only
tests build WSL2 argvs, it is the bare `wsl`. `create` builds no `WSL2Transport`
(`wsl2.py:765-775`); `run_bootstrap(run_as_root, ...)` runs the key-free script with a 900-second
deadline (today's, `wsl2_bootstrap.py:130-134`) and parses its markers from the result's stdout,
then create joins and settles. The bootstrap stays synchronous: a detached run would be in its own
systemd-logind session, where `KillUserProcesses` can reap it when `wsl.exe` exits.

Today's `setsid sudo -n /bin/bash FILE </dev/null` (`wsl2_bootstrap.py:131`) becomes
`setsid -w /bin/bash -s` under `--user root`. `setsid` was added because sudo's `use_pty` gave the
bootstrap a terminal whose foreground group was sudo's monitor, so `dpkg`'s triggers stopped apt
with `SIGTTIN` (`1ae933a88`). The new argv needs no sudo, but no sudo is not enough: measured on WSL
2.7.14.0, `wsl.exe --exec` makes its program a session leader with a pseudo-terminal as its
controlling terminal (`pts/0`, its own foreground group, `/dev/tty` opens) whether or not `wsl.exe`
has a console and although its stdin is a pipe (since `3b8a73624`). A payload that reads or resets
`/dev/tty`, as a maintainer script may, then stops until the deadline. `setsid` (util-linux,
essential in Debian 12 and 13) starts Bash in a new session with no controlling terminal; since
`wsl.exe`'s program is already a group leader it forks, and `-w` waits for Bash so the channel ends
when the script does. Bash inherits all three streams; the channel's own exit status is not read
(D4). Incident row I34 holds this, with vector W7, and a unit test runs the in-distro argv under a
controlling pseudo-terminal (`tests/capabilities/test_wsl2_root_channel.py`).
`recovery_needs_guest_sshd = False`.

### Proxmox

`guest_agent.run_as_root(api, node, vmid, script, deadline) -> RawRun` is written fresh from the
dispatch-and-poll half of `plugins/proxmox/transport.py:51-137`. First it pings the guest agent
(`guest_agent_ping`, `POST .../agent/ping`) every two seconds until it answers, since a VM that has
just started (recover starts a stopped one with no wait) has no agent running yet. A ping carries no
script, so asking again is safe; a token rejection raises `TokenRejectedError` at once, any other
`ProxmoxAPIError` means not yet (anything else the client raises is a bug and propagates), and no
answer within `AGENT_WAIT_SECONDS` (120 s) or the deadline, whichever comes first, raises
`ChannelUnreachable` saying no script was dispatched, so a VM with no agent fails in about two
minutes rather than after the repair's 600. Then, unless the pinging left none of the deadline
(which raises `ChannelUnreachable` the same way), one `guest_agent_exec` of `["/bin/bash", "-s"]`
with `input-data`, then `guest_agent_exec_status` every two seconds until it reports exit or the
deadline passes. Each request's wait is bounded by what remains of the deadline, at most the API's
30-second `REQUEST_TIMEOUT_SECONDS`. A socket timeout bounds the TCP connect and each wait for data,
not name resolution or a whole response, so an API that keeps a response arriving can carry one
request past the deadline; the `run_as_root` contract states the same limit for any channel over an
HTTP API. The operator's [2026-11-11 ruling](./frd.md#2026-11-11) keeps that limit documented rather
than enforced: the API is the site's own server, and a whole-response bound would mean replacing
`urllib` and the proxy support that comes with it, reaching into private socket attributes, or
abandoning a worker thread at the deadline. A poll carries only the PID, so a failed one is asked
again at the next interval; only polls that keep failing for `STATUS_RETRY_SECONDS` (30 s) give up,
as they do for an agent that restarted and lost the PID. Dispatch and status failures raise
`ConnectivityError` with no stdin in the message (`guest_agent.py:80-118`), except an HTTP 401 or
403, which raises `TokenRejectedError` at once; a status failure of either kind says the script may
still be running, and a status failure from the API adds the API's own error text (an agent that
restarted mid-script answers `Agent error: PID <pid> does not exist`), since that request carries
only the PID. A dispatch failure adds none, since its request carries the script. A
`_TruncatedQGAOutput` report raises `ConnectivityError` saying the script exited with its output cut
short. No failure chains the API error, whose request quotes the script. `create` waits for
cloud-init through it (`cloud-init status --wait --long`, the cloud bootstrap's readiness command,
420 s, which covers the agent ping's 120 and cloud-init's 300, status 2 a warning, and any other
failure quoting cloud-init's last lines as the cloud bootstrap does; a channel failure is asked
again within that budget, since cloud-init can restart the guest agent and the command changes
nothing), with no separate wait for a guest IP, then runs the bootstrap through it with
`run_bootstrap` and today's 600-second deadline (`platform.py:600`), then joins and settles;
`guest_agent_exec_wait` and `guest_agent_network` leave the API client. `out-data` and `err-data`
are encoded with `errors="replace"`, since the API's JSON can carry a lone surrogate, and `_request`
types `http.client.HTTPException` as `ProxmoxAPIError` like any transport failure.
`_run_bootstrap_via_agent` (`platform.py:566-636`), `native_transport` (`:488-502`) and
`transport.py` go. No `native_shell` override. `recovery_needs_guest_sshd = False`.

**Which guest holds the VMID.** Proxmox reissues a freed VMID to the next create, and a node listing
hides every guest a pool-scoped token cannot audit, so neither a row's VMID nor its absence from a
listing identifies the guest. Create names the clone the VM's hostname, which the row records (a row
the vm-sites migration converted also answers to its old clone name, the VM name). Every operation
that addresses a guest by VMID (delete, start, stop, status, the root channel above and create's
rollback) first calls `identity.find_guest` (`plugins/proxmox/identity.py`): the guest is the row's
only when the name `status/current` reports matches. When that read fails,
`GET /cluster/nextid?vmid=N`, which any valid token may call whatever its ACLs, decides: a free VMID
means the guest is gone, and a VMID held by a guest the token cannot audit (the read's 403) raises
`StateError` naming the VMID; anything else is unknown and re-raises the read's error, never read as
gone. A guest of another name holding the VMID is gone too. Delete of a gone guest returns, warning
and leaving the holder untouched when another guest has the VMID; every other operation refuses a
gone guest with a hint naming `agw vm delete`, so delete's logout, which reads `status` first and
then goes through the root channel, never reaches another guest. `teardown.remove_guest` stops and
deletes a confirmed guest and returns once a destroy task completes; a failed delete counts as done
only when `/cluster/nextid` says the VMID is free. A rollback whose clone lost its VMID to another
create finds the winner's name there and leaves it untouched. A caller with a budget passes
`find_guest` its expiry: each read's wait is bounded by what remains of it, at most
`REQUEST_TIMEOUT_SECONDS`, and a budget spent before a read asks nothing more and leaves the
identity unknown (`ProxmoxAPIError`). `status` gives the whole check 10 s and reads an API failure
as unknown. The root channel's check spends the script's own deadline, and the guest agent is handed
the same expiry instant (`guest_agent.run_as_root_until`), not a duration it would restart later; a
check the deadline runs out on, an API failure it cut short included, raises `ChannelUnreachable`
with nothing dispatched.

### AWS, Azure and GCP

The three clouds subclass one shared base, `PublicAddressPlatform(VMPlatform)` in `public_ssh.py`,
which holds the root channel once. Each cloud supplies two things:
`_public_address(vm, ctx) -> str`, today's live public-address read
(`plugins/aws/platform.py:703-718`, `plugins/azure/platform.py:800-824`,
`plugins/gcp/platform.py:580-597`), and `public_address_hint`, its own console hint, an abstract
property each cloud sets as a class attribute, so registration refuses a cloud without it. The
base's private context manager `_public_ssh(vm, ctx, binding)` opens the cloud's
`_transient_route(vm, ctx)`, reads the address, and raises `StateError` with `public_address_hint`
when it is empty, where the factory raised it for every platform (`transports/__init__.py:215-228`,
incident row I32); GCP's read raises `GCEError` itself instead. It then builds
`PublicSSH(address, admin, identity_file, binding)` with the identity file from
`ctx.config.operator.ssh_private_key` (every operation's context carries the configuration), runs
`wait_until_answering()`, and yields it for as long as the route is open, all inside
`termination_as_interrupt()`. That block owns Ctrl-C (`SIGINT`) as well as `SIGTERM`, `SIGHUP` and,
on Windows, `SIGBREAK` (Ctrl-Break), so all of them take one path while a route may be open, for
every cloud command, not only the shell; `create_vm` opens the same block around its own stack, so a
cloud create's bootstrap allow is removed by its interrupt rollback too. The C runtime is believed
to deliver a console window's close as `SIGBREAK` too, so closing the window may start the same
unwind, but Windows ends the process within seconds, so nothing may rely on it finishing, and the
docs tell the operator to remove a rule a closed window left; a kill runs nothing at all. Whichever
signal arrives first starts the unwind, and from then until the outermost block exits the
terminations are ignored, so a closed terminal or a second termination cannot cut the route's close
short; a second Ctrl-C still interrupts, since that is how the operator abandons a cleanup that
hangs. A block nested in another, or off the main thread, changes nothing, and a signal already
ignored (`nohup`) stays ignored.

A hangup needs nothing more. A terminal that hung up fails every write, as does a pipe into a
program the hangup ended too (`| tee create.log`) or one whose reader exited (`| head`), and the
first line a rollback printed would end it. The output handlers write every line through
`output.tolerating_dead_sink`: a write that fails because nothing reads the stream
(`output.consumer_left`: `EPIPE`, `ECONNRESET`, `EIO`, and `EINVAL` on Windows) points that stream's
descriptor at the null device and carries on, so the cleanup runs to the end, silently on that
stream, while any other failure (a full disk under `> file`) still raises. The CLI entry point
flushes both streams through the same guard before it exits, so the interpreter's own flush has
nothing left to fail on. Prompts are not guarded, since a dead stream cannot answer one.

Each cloud's `_transient_route` removes its allow under `interrupts.interrupts_deferred`, which
holds Ctrl-C and the terminations until the provider call returns and then delivers the first that
arrived to the handler it would have reached, so an interrupt that lands mid-removal waits for it.
Only what was actually removed is reported closed: AWS counts a revoke that EC2 answers with the
rule listed under `UnknownIpPermissions`, like `InvalidPermission.NotFound`, as nothing revoked, and
Azure reports a rule already gone (404) as nothing. Once the route has closed, `_public_ssh` calls
`close_provisioning_access_or_warn` (contract-lld, Phase A), so a bootstrap allow that create's own
close left open is removed by the next root-channel operation, `vm recover`, `vm rekey`, delete's
logout or a platform shell; a close with nothing left to remove is silent. Azure's poke removes a
rule whose create was interrupted, by the name it chose. `_transient_route` is an abstract member of
the base, which each cloud supplies and nothing outside the cloud calls. Then:

- `run_as_root`: `trust.require_record(vm.name)` first, so a VM with no record opens no firewall
  rule, then `run_as_root(script, deadline)` inside `_public_ssh` under VM trust.
- `read_host_keys`: a VM that has a record raises `StateError`, since its keys are read under that
  record, never around it. Otherwise, inside `trust.provisioning_binding(vm.name)`
  (`trust.py:281-299`) and `_public_ssh` under that binding,
  `root_script.read_host_keys(target.run_as_root)` under that Unverified file. HLA D9's only
  post-provisioning use of Unverified.
- `native_shell`: `trust.require_record(vm.name)` first, as for `run_as_root`, then inside the route
  under VM trust, the probe, then `shell()`.
- `create` builds
  `PublicSSH(public address, admin, request.ssh_private_key, request.provisioning_trust)` where it
  builds an `SSHTransport` (`aws/platform.py:573-582`, `azure/platform.py:643-652`,
  `gcp/platform.py:411-423`) and hands `complete_cloud_bootstrap` its `run_as_root`.

Every cloud `run_as_root` opens and closes its own route on purpose, so no firewall allow outlives
one script, at the cost of a provider round trip and a probe per call. Recover pays one route for
the key read when the record is missing, one for the repair, and one per address read (normally
one). `recovery_needs_guest_sshd = True`. Unverified provisioning before enrollment is unchanged
(trust-lld, "Trust modes and options").

## Core's recovery scripts

`vms/recovery.py` holds two payloads (Bash, `set -eu`), each with one function that calls
`root_script.run(partial(platform.run_as_root, vm, ctx), ...)`. Core reads a script's status only as
zero or non-zero; a failed check prints its reason on stderr and exits 1.

| Script | What it does, in order                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | Deadline | Secret   |
| ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- | -------- |
| Repair | Append `/usr/sbin:/sbin` to `PATH` (`dpkg` needs `ldconfig` and `start-stop-daemon` on it). Fail if the admin account is missing. Write the admin's whole managed `authorized_keys` as reinit writes it (`ssh_keys.authorized_keys_content`: the header, the configured key, then each `operator.extra_ssh_public_keys` file) through a temporary file and `mv`, so a key it no longer lists stops working now, with today's modes (`bootstrap_script.py:143-155`). Create `/run/sshd`, fail if `sshd -t` fails, then `systemctl unmask`, `enable`, `reset-failed` (a unit that crash-looped hit its start limit, and systemd refuses to restart it until that is cleared) and `restart ssh.service`, before the package install. Unless `/usr/bin/python3 -I -S -c 'import json'` succeeds, `apt-get update` and `DEBIAN_FRONTEND=noninteractive apt-get install -y python3`, both with `-o DPkg::Lock::Timeout=120` and their output on stderr, then fail unless that check passes. Fail if `tailscale` is missing, start `tailscaled` unless `systemctl is-active` says it runs, then read `BackendState` from `tailscale status --json` with python3, reading it again up to 15 times two seconds apart while it is `NoState` or `Starting` (a daemon just started reads those before it settles): `Running` does nothing, `NeedsLogin` runs `JOIN_FRAGMENT` and prints `joined`, anything else fails naming the state | 600 s    | auth key |
| Rekey  | `trap '' HUP PIPE`; today's sequence (`vms/manager/power.py:499-518`): restart `tailscaled`, logout, `JOIN_FRAGMENT`, restart, each followed by 15 s, the logout under `timeout 60`. Each step's output is captured with its status, the first failure stops the sequence, and the buffer is printed once at the end (A channel that drops)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | 180 s    | auth key |

- The check runs the interpreter agw-launch runs (`ssh_process.DEFAULT_PYTHON`), so it covers both
  of agw-launch's `VMNotReady` causes: no python3, and `python3-minimal` alone, the `STDLIB_MISSING`
  case (execution-lld, "agw-launch"), which has no `json`. It does not ask `dpkg`, which still
  reports a removed but not purged `python3` (`deinstall ok config-files`) as installed. Debian's
  `python3` package depends on its standard library, so installing it repairs both.
- The sshd step runs on every platform. A script that runs proves its channel works now, not that
  sshd is healthy: Lima's `limactl shell` reuses the host agent's persistent SSH connection, which
  outlives a stopped or masked `ssh.service`, and a running sshd does not reread a configuration
  that would stop it from starting again. The live Lima cell skipped the repair under the earlier
  `REPAIR_SSHD` gate and then reported "repaired". Restarting `ssh.service` ends no session, since
  Debian's unit has `KillMode=process`, so the channel survives; a failed `sshd -t` still stops the
  repair before the restart. `recovery_needs_guest_sshd` is now read only by recover's failure hint.
- The `BackendState` check is unmeasured. Vector W9 proves that a node removed in the admin console,
  and one whose ephemeral key expired, read `NeedsLogin`, and that a healthy one reads `Running`.
- The file is the one reinit writes, built by the same function before the boundary from
  `prepare_configured_ssh_identity(...).public_text` (`vms/applied_state.py:156-170`, already parsed
  as one key line) and `operator.extra_ssh_public_keys`, so the keys recover removes (one the
  configured key replaced, a manual edit) are exactly the ones reinit would. Only the admin's file
  is rewritten; agent accounts keep theirs.
- Not carried over: rejoin's `curl | sh` install (`vms/initializer/credentials.py:61-65`), since
  provisioning always installs Tailscale and its absence now fails with a reason; regenerating host
  keys, which would silently change trust; and the HLA's "reload sshd on every platform", since no
  script edits sshd's configuration.
- No script leaves a background child, so `run_process` returns when the channel exits.

## Commands

### `vm start` and the activation gate

The operator's rulings of 2026-10-31 and 2026-11-01: start the VM, wait for it to answer, and name
`agw vm recover` when it does not. One helper in `vms/manager/tailscale.py` serves both:

```text
_await_tailnet(db, config, vm, *, already_running: bool) -> None
```

It re-reads the row and, when it has an address, runs slice 3's `reachability.wait` on the admin's
`VMAccess`, with `VERIFY` wording for an already running VM and `RECONNECT` otherwise. `False`, or
no address, raises `VMNotAnswering`, a `ConnectivityError` ("VM 'x' is running but did not answer
over SSH at ADDRESS") whose hint is `Run 'agw vm recover x'.`; the wait's own raises
(`TrustMismatch`, `Unknown(AUTH_FAILED)`, `VMNotReady`) propagate with their hints.

- `start_vm` replaces the rejoin block and its auth-key resolution (`vms/manager/power.py:85-120`)
  with `_await_tailnet` inside the same `hold_active()`. Before anything resolves it runs
  `verify_tailscale_available()` and `execution.resolve_ssh()`, as rekey and recover do, since its
  wait reaches the VM over the tailnet with the OpenSSH client. The `trust.require_record` before
  the boundary goes (`:53-56`): a stopped VM with no record must still start, so that its wait
  raises `TrustMissing` naming recover, which starts a stopped VM itself (below). A start that
  booted the VM says so before the wait, so a failed wait still reads as a running VM. Otherwise an
  idle-stopped VM from before the host-key store could neither start nor be recovered.
  `LiveVMNode.auto_start` keeps its check, since the gating commands' own boundary refuses such a VM
  earlier.
- `LiveVMNode.auto_start` replaces `_tailscale_rejoin_required` and `_ensure_tailscale`
  (`vms/nodes.py:265-282`) with `_await_tailnet(..., already_running=False)` inside `vm_active`.
- `_ensure_tailscale` and `_tailscale_rejoin_required` are deleted (`tailscale.py:69-191`);
  `_tailscale_logout` stays as delete's logout over `tailscale_join.log_out` (below), and
  `TailscaleWait` and `wait_for_reconnect` (`transports/__init__.py:248-318`) go with their last
  callers. `rejoin_tailscale` and `_join_tailscale` are deleted
  (`vms/initializer/credentials.py:43-99`), with their exports (`vms/initializer/__init__.py:34-35`;
  `vms/manager/__init__.py:26`, `:67`, `:70`, `:78`, `:83`, `:107`, and its docstring's mention of
  `_ensure_tailscale` at `:13`); `verify_tailscale_available` stays.

`vm shell --platform` opens the same gate. On a stopped VM whose Tailscale is broken, the gate now
fails after starting it; the VM is left running, so the same command run again goes straight to the
native shell. This is accepted rather than given a second gate.

**Ephemeral-key VMs** (operator ruling: document it). `README.md:238-240` and ADR 0003 (`:36-37`)
promise that ephemeral keys are supported with an automatic rejoin on start. Without it, a VM whose
ephemeral node was removed while it was offline fails `vm start` with the recover hint, after the
full wait of about four minutes. `agw vm recover` then rejoins it in one step: its repair script
sees `NeedsLogin` and joins at once, with no wait before the join. Nothing new detects the case; the
start failure is the signal. The README and ADR 0003 are updated to say so (Collateral).

### `agw vm recover <name>`

`vms/manager/recover.py`, wired in `cli/commands/vm.py` as `vm rekey` is. In order:

1. `_require_vm`; `_guard_incomplete_vm(vm, allow_failed_init=True)`, which still refuses a VM whose
   provisioning failed (`vms/manager/_helpers.py:229-235`): a create retained as failed before
   enrolling is deleted and recreated, not recovered. A VM with no host-key record is refused
   without a terminal, since recording its keys needs the operator's confirmation; the hint names an
   interactive recover or `agw vm import-host-keys NAME --known-hosts FILE`. Then
   `verify_tailscale_available()` and `execution.resolve_ssh()`, so a workstation off Tailscale or
   an unsupported client is refused before any secret resolves.
2. The boundary, as rekey builds it (`power.py:421-441`): the VM-template node with the live VM
   node, one preflight and one resolve, so the auth key resolves up front with the site's secrets.
   `require_tailscale_auth_key`, the check create and rekey share, refuses a key that is blank or
   not one line.
3. The power state, read as rekey does (`power.py:458`). `UNKNOWN` raises `StateError`: the platform
   could not report the VM's state, so check it in the provider or with `agw vm describe NAME`.
   `STOPPED` or `DEALLOCATED` is started in step 5, before the hold as `start_vm` starts before its
   own (`power.py:63-67`, since WSL2's keepalive boots a stopped distro itself), through
   `platform.start` with no wait for SSH, recording running intent as an explicit start does and
   printing that it did. Refusing it instead loops for a VM with no record: start boots it and its
   wait raises `TrustMissing` naming recover, and a WSL2 distro can idle out again after start's
   hold ends, before recover runs. Recover needs no wait of its own because each remote root channel
   waits about two minutes for a VM that has just started: the clouds' `wait_until_answering` and
   Proxmox's guest-agent ping.
4. The operation log, from a factory in `vms/manager/_helpers.py` (a legacy file, which owns
   `LOG_DIR`'s other uses) that constructs an `SSHLogger` for the `recover` stem with the auth key
   in its redaction set. `recover.py` types it with slice 3's small log Protocol (contract-lld,
   "Independence"), so it imports nothing legacy.
5. Inside the operation log, the start of a stopped VM (step 3); then, inside
   `vm_node.hold_active()`, **trust**: with no store record, `platform.read_host_keys`, each key's
   type and fingerprint printed with a line saying to compare them with the VM's console (a cloud's
   serial console or system log), `output.confirm` defaulting to no, then `trust.write_records` and
   `sync_ssh_config`. A refusal writes nothing and stops.
6. **Repair**, one script with the auth key and the log. A failed or unknown outcome stops recover.
7. **Address**: when the repair printed `joined`, `settle_tailnet_address` with recover as its rerun
   hint, as create settles after its join; otherwise `read_tailnet_address` alone, so a flaky
   channel never logs out a node that was working. Record the address when it changed, then
   `sync_ssh_config` whatever the address did: the aliases name the configured key, which the repair
   just installed and may be a new one.
8. **One wait**: `reachability.wait` with `RECONNECT` wording when the repair printed `joined`, else
   `VERIFY`. `False` raises `ConnectivityError`: the VM was repaired but does not answer over SSH at
   ADDRESS; the hint is to check tailnet sharing and ACLs, then recreate the VM (R7).
9. **Identity**: the wait logged in with the configured key alone (`IdentitiesOnly=yes`), which the
   repair installed, so recover records it as the VM's SSH identity with reinit's own proof,
   `build_vm_initialization_slices(AuthorizedKeysApplied(...), include_hardware=False)` written by
   `replace_applied_slices` under the operation `vm-recover`. Without it an operator who changed
   keys gets a recover that succeeds while every SSH command refuses the drift, and reinit refuses
   it too. The proof reads the private key again; a key replaced while recover ran yields no slice,
   and recover raises `StateError` asking for a rerun, leaving the old record.
10. **Agents**: after its result line, when the VM has agents, recover names them: the repair
    rewrote only the admin's keys, so if `operator.ssh_private_key` changed, each agent account
    still accepts only the previous key until `agw agent reinit` runs for it. Whether the key
    changed since an agent was set up is not recorded, so the note is worded for that condition.

Recover runs under the VM family guard, selecting the VM again by its identity and checking it once
the guard is held. Its hold follows intent (Desired runtime state, below): a VM that is running
while its intent is stopped is not held, so on WSL2 a long repair can idle out between root-channel
calls. Rekey's gate holds the same way.

When the repair fails or its outcome is unknown, the message names the script and its status (or
"unknown") and points at `agw vm logs NAME`. A repair that exited with a status ran, so its channel
works and the platform's native shell, which uses that channel, is the way in: the hint names
`agw vm shell --platform NAME` where the platform has one (on Lima, its persistent connection still
reaches a VM whose sshd has stopped), and the platform's `native_shell_unavailable_hint` where it
does not. An unknown outcome on a platform whose channel goes through the guest's sshd instead adds
that a broken SSH server needs a way in that does not use it (a cloud provider's serial console) or
recreation.

### `vm rekey`

The boundary and the running check stay (`power.py:401-464`), and rekey gains an operation log built
the same way (the `rekey` stem). Before the boundary it runs `verify_tailscale_available()`, as
recover does: its final wait reaches the VM over the tailnet, so a workstation off Tailscale is
refused before the key is spent. Its boundary refuses a blank auth key with
`require_tailscale_auth_key`. The native transport and the per-step commands (`:480-528`) become the
rekey script under the existing gate, then `settle_tailnet_address`, which replaces today's own
`tailscale ip -4` read (`:520-527`). Then, as today, the address is recorded, the SSH config synced
and the `rekey` event written (`:532-534`), then `wait_for_share` and `reachability.wait` on the
updated row with `RECONNECT`, replacing the in-place repointing of a legacy transport (`:544-550`).

- When the settle's reads run out, the node is logged out and the error names `agw vm recover NAME`,
  which rejoins it.
- The running check refuses a power state the platform cannot report, as recover does
  (`_require_known_power_state`), rather than calling it not running.
- A failed or unknown script raises naming the script, its status and the log, with the hint
  `agw vm recover NAME`. A rekey script whose channel raises `ChannelUnreachable` never ran, so the
  error propagates at once, after the channel's own hint (a restart, on a cloud), with the same
  `agw vm rekey NAME` command again, with every option given, once the VM answers: recover's channel
  is the same one, so it could not do better.
- A final wait that returns `False` raises `VMNotAnswering`, whose hint names `agw vm recover NAME`,
  as `vm start` does; the new address is recorded first, since the old one died with the logout.
- The per-step progress lines become one line before the call ("about a minute").

### `vm delete`'s logout

The operator's [2026-11-08 ruling](./frd.md#2026-11-08) keeps R7's logout at delete, over the root
channel: whoever deletes a VM wants it gone, and `vm rekey` puts one whose delete then fails back on
Tailscale. `delete_vm` (`vms/manager/power.py`) runs every check that can refuse first: the child
count and the confirmation, then the binding. Once the binding holds, and only for a row with a
tailnet address, `_tailscale_logout` runs inside `vm_node.hold_active()`, a hold-only span that
wants the VM up if it happens to be (WSL2's hold boots a stopped distro) but never gates, since a VM
with stopped intent would refuse; a hold that fails is one more reason the node was not logged out.
Hold admission depends on intent (Desired runtime state, below): a VM whose intent is stopped is not
held, since the hold could boot it, so a stopped WSL2 distro with that intent stays stopped and is
reported as not logged out. Then `platform.delete` runs. No session teardown precedes any of it: the
provider delete ends the workloads.

`_tailscale_logout(vm, platform, ctx) -> str | None` (`vms/manager/tailscale.py`) reads the power
state and calls `tailscale_join.log_out(...)` with no log, checking nothing over SSH. It returns
`None` once the node has logged out, printing "Tailscale node HOSTNAME logged out", or why it was
not:

- stopped or deallocated (a failed status read counts as unknown and asks): not asked, since no
  channel reaches the VM; a WSL2 distro the hold has booted reports running;
- a status the platform refuses with a `StateError`: the platform cannot confirm the guest is this
  VM's (a Proxmox VMID that is gone, reissued or held by a guest the token cannot audit), so nothing
  is sent, and the platform's delete decides;
- a non-zero status, or an unknown outcome or a raise, which could not be confirmed;
- `TrustMissing` (a cloud VM with no record, raised before any route opens): the host key was never
  recorded. Delete never logs out under Unverified trust: an impostor could report a logout that
  never happened, and slice 3's in-create logout covers the window inside `create`.

The reason is reported once the delete's outcome is known: when the row goes, a warning names the
node for removal in the Tailscale admin console. `UserAbort` propagates from the hold, the logout
and the delete, keeping the row, and no other logout failure skips the backend delete (I33).
"Logging out of Tailscale..." prints before the call and the result after it. For a cloud, the
route-close line therefore comes before the result, where #350 (`ebb2e6fdf`) put the result inside
the route window; that order is accepted, since the result is known only when the call returns.

**A delete that does not complete.** Without `--force`, a binding failure (an unset secret, a
removed site or plugin) refuses with a `StateError` naming the site's failure, and a
`platform.delete` that raises propagates; both keep the row and the store record (I18). The hint
from `_finish_delete_hint` leads with finishing the delete: fix the cause and rerun, or remove the
backend by hand, named by the site, hostname and platform metadata the row holds (a Proxmox VMID, a
cloud instance ID, a Lima instance name), and rerun with `--force`. Only after a successful logout
does it add, as an aside, that `agw vm rekey NAME` puts the VM back on Tailscale instead, as the
ruling asks. `--force` has one meaning: also delete the children, and remove the row and the record
whatever kept the backend delete from completing (a site that cannot bind, a platform refusal, a
Proxmox identity the token cannot verify, a provider error), warning with the same identifiers
(`_warn_backend_left`) and reporting that the backend was not deleted. A forced delete past a
binding failure runs no logout.

### `vm shell --platform`

`shell_vm` (`vms/manager/exec.py:119-129`, `:163-178`) hands `--platform` to `_shell_native`, which
refuses `--workspace` before anything else, with the hint to `cd` inside the shell. It calls
`require_native_shell` right after resolving the site, before the boundary, which raises
`FeatureUnavailable` with the platform's hint when there is none. Otherwise, inside
`gated_vm_platform_channel_boundary` with no environment targets, it runs
`platform.native_shell(vm, ops_ctx)` under `guarded_terminal`, sets `clean_exit` on a zero status,
and after the guard prints the 255 notice (I6) without claiming the cause: "the native shell ended
with status 255 (the connection may have dropped); terminal restored". The environment is not
composed and its secrets are not resolved (R7: the native shell does not seed it).
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

## Desired runtime state

Main's desired runtime state (#885) arrived after this slice's design; the merge keeps this slice's
structure and adapts its rules as follows.

- **Start and activation.** `vm start` accepts running intent and runs main's prepared runtime
  (`vms/manager/runtime.py`) under the VM family guard. Its start and activation, and the gate's
  `auto_start`, end in `_await_tailnet` inside the hold, as above, and resolve no Tailscale key. An
  explicit start proceeds on an unknown power state, boots a VM with no host-key record, and says
  what it did before the wait; an activation refuses both, as the gate does.
- **Incomplete VMs.** `prepare_vm_runtime` runs `_guard_incomplete_vm`, so start, stop and the
  console paths refuse a VM whose provisioning did not complete, and reconciliation and its preview
  block that VM's family, as every other command but delete does.
- **Holds follow intent.** Hold admission checks intent under the family guard, since a hold can
  boot the VM. A VM whose intent is stopped is never held: delete's logout, recover and rekey run on
  such a VM without a hold, as their sections say.
- **No repair credentials.** The desired runtime state FRD's R7
  ([frd.md:195-198](../2026-11-09-desired-runtime-state/frd.md#r7-prepare-the-entire-batch-before-workload-changes))
  lets activation acquire conditional Tailscale repair credentials. After this slice's R7 and the
  operator's [2026-11-01 ruling](./frd.md#2026-11-01) (a start waits for the VM, then names
  `agw vm recover`), there are none to acquire: activation waits, and a VM that does not answer
  blocks its family with a failure naming recover. That SDD is locked, so the supersession is
  recorded here.

## Error mapping

Slice 3's D13 ruling holds: no message, `repr` or hint holds a stream. A script's reason is in the
operation log, and the error names the script, its status and `agw vm logs NAME`.

| Situation                                                  | Error                                                                | Hint or effect                                                                                                    |
| ---------------------------------------------------------- | -------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| No native shell on the platform                            | `FeatureUnavailable` (`StateError`, new in `execution/errors.py`)    | The platform's `native_shell_unavailable_hint`, when it declares one                                              |
| `--platform` with `--workspace`                            | `ValidationError`                                                    | `cd` inside the shell                                                                                             |
| Start or gate: no answer after the wait, or no address     | `ConnectivityError`                                                  | `agw vm recover NAME`                                                                                             |
| Recover or rekey: power state unknown                      | `StateError`                                                         | Check the provider or `agw vm describe NAME`; a stopped VM is started instead                                     |
| Repair or rekey: non-zero status                           | `StateError` naming the script and status                            | The log; the native shell or the platform's hint (repair) / recover (rekey)                                       |
| Repair or rekey: unknown, or the platform raised           | `ConnectivityError`, or the raise                                    | The log, the sshd note where it applies; never re-dispatched                                                      |
| Recover: no answer after the repair                        | `ConnectivityError`                                                  | Tailnet sharing and ACLs, then recreate                                                                           |
| Address read or settle exhausted                           | `ConnectivityError` naming the node; create's wrappers keep its hint | Hint: after a logout, `rerun_hint`; after a failed one, the admin console too; read alone, the log                |
| A channel's own wait finds no way to the VM                | `ChannelUnreachable`, at the first call; no retry and no logout      | The channel's (a cloud's names a restart), then the caller's: `rerun_hint`, or a rekey rerun                      |
| Cloud: the VM refuses the key at its public address        | `KeyRefused` from `PublicSSH`                                        | The provider console, or recreate                                                                                 |
| A cloud VM with no store record                            | `TrustMissing` from `run_as_root`, before the route                  | Recover re-establishes; delete warns naming the node                                                              |
| Cloud: the public address refuses the connection           | `_ConnectionRefused`, waited out; `ChannelUnreachable` at the bound  | A restart, or the provider's serial console; no firewall advice                                                   |
| Proxmox: the guest at the VMID is not the row's            | `StateError`, before anything is sent                                | `agw vm delete NAME`; delete itself removes the row and leaves another guest untouched                            |
| Delete: the site cannot bind, or the backend delete raises | `StateError`, or the platform's error; the row and record stay       | Finish the delete (fix and rerun, or remove the backend and rerun with `--force`); the rekey aside after a logout |
| Provisioning join or bootstrap: not status 0               | `ProvisioningError`, inside the platform's rollback                  | An unknown join names the node                                                                                    |
| `python3` missing, or without its standard library         | `VMNotReady`                                                         | Now `agw vm recover NAME` (`execution/errors.py:77`)                                                              |
| No store record                                            | `TrustMissing`                                                       | Import, or `agw vm recover NAME` (`errors.py:37-40`)                                                              |
| `limactl` or `wsl.exe` cannot start                        | `ConnectivityError` naming the program                               | Install it                                                                                                        |

`TrustMissing`'s hint keeps "delete and recreate a VM whose create never enrolled", because recover
refuses a provisioning-failed VM (step 1).

## Independence

- **Package guard.** `test_independence.py` walks only `agentworks.execution`, so it gains explicit
  imports of `agentworks.capabilities.vm_platform.public_ssh` and `agentworks.vms.recovery`, as
  slice 3 adds `agentworks.vms.contract`. Importing `public_ssh` runs
  `capabilities/vm_platform/__init__.py` (`:30-31`), so the guard proves `lima.py`, `lima_host.py`,
  `wsl2.py`, `tailscale_join.py` and `bootstrap_script.py` legacy-free transitively. Measured at the
  baseline: the other modules those import (`capabilities.base`, `db`, `debian`, `schema`, `topics`,
  `subprocess_io`, `secrets.line_safety`, `execution`) load no legacy module.
- **Static check.** The plugins cannot pass the import guard: importing any of them loads the plugin
  index (`plugins/__init__.py:26-35`), which loads the harness plugins and `native_files`
  (measured), until slice 5. They join `MIGRATED_FILES` instead
  (`tests/test_execution_import_boundary.py:24`): the four plugin `platform.py` files,
  `plugins/proxmox/guest_agent.py`, `vms/manager/recover.py`, `vms/manager/tailscale.py`,
  `vms/nodes.py`, `orchestration/activation.py` and `vms/initializer/tailscale_preflight.py`.
  `power.py` cannot (`LOG_DIR`), nor can `_scope.py` (`SSHLogger`, `:29-30`) or `_helpers.py`; all
  three call new code, which R10 allows.
- **Both guards for the platform package.** The package guard runs no function body and skips
  `TYPE_CHECKING` blocks, so it cannot see a legacy import there (`base.py` held one, for the
  deleted `ExecTransport`). Every `vm_platform` module it proves is therefore in `MIGRATED_FILES` as
  well, as `vms/contract.py` is: the interim `lima_host.py` and `public_ssh.py` entries, and the
  package's other modules once they imported nothing legacy.
- **Deleted with no caller left.** `transports/lima.py`, `remote_lima.py`, `wsl2.py` and
  `_shared.py` (their only users are the platforms and `transports/__init__.py`'s exports), the
  `native_transport` factory, `plugins/proxmox/transport.py`, `vms/manager/platform_shell.py`, and
  their tests. `ExecTransport`, which only the native path used, folds into `Transport`. Slice 8's
  list loses the Proxmox module, and so does `tests/execution/legacy_modules.py`.

## Tests

None restates our prose or the implementation. Registration of `recovery_needs_guest_sshd` gets no
test of its own: the generic required-attribute check (`conformance.py:152`) covers every attribute.

**Unit.**

- **The wrapper.** `root_script.run` over fabricated `RawRun`s: both closing markers give the status
  (including 255); a missing `end`, a missing trailer, a malformed trailer or no start marker give
  `None`; bytes outside the markers never reach `stdout` or `stderr`; NUL-interleaved pre-marker
  stdout reads as text in `channel_tail`; the secret is absent from every field, `repr`, any raised
  error and the logged record; the runner is called exactly once even when the result is unknown or
  it raises; the log receives one record per call.
- **`run_process`** against a local `/bin/bash -s`: the deadline returns at the deadline with the
  process reaped; a 2 MiB writer stops at the limit; a background child holding stdout does not
  stall it; a missing program raises `ConnectivityError`, not `ClientUnsupported`.
- **Argv.** `ssh -G` against the installed client: `PublicSSH`'s argv ignores an operator
  configuration that sets `ForwardAgent yes`, `ControlMaster auto` and `RequestTTY force`; the host
  argv still honors one; `LimaSshPlacement` refuses a host with whitespace.
- **Platform surface.** `read_host_keys`' default goes through `run_as_root`; the parser drops
  malformed lines and refuses an empty result.
- **Provisioning (I10).** Each of the six creates, with its channel faked at `run_process`,
  `ssh_process.spawn` or the QGA client: the auth key appears in no argv, no progress-sink call, no
  exception's message, `repr` or chain, and no file write, and appears in exactly one `run_as_root`
  script, the join's. The join runs once when the address reads fail, and once when it is unknown.
- **Address.** A platform `ConnectivityError` counts as a failed read and `TrustMismatch` propagates
  from the first; `read_tailnet_address` never logs out, and `settle_tailnet_address` logs out once
  on exhaustion; create's readiness wait retries a `KeyRefused` and stops at its time bound. This
  extends `tests/test_cloud_bootstrap_secret_boundaries.py`.
- **Commands** (fakes for the platform, the database and `VMAccess`):
  - start and the gate, on a VM that does not answer: the recover hint, no auth key resolved, no
    script dispatched; start off Tailscale or with an unsupported client refuses before any secret
    resolves;
  - recover: keys written only after confirmation, then the SSH config synced; a failed repair stops
    before the address read; `joined` selects the settle and the reconnect wording, and without it
    no logout runs; a stopped VM started before the hold and then repaired, and an unknown power
    state refused before any script; a provisioning-failed VM refused; a blank auth key refused
    before any script;
  - rekey: one script, then the settle, the address recorded; a failed script names recover; a
    `False` wait raises `VMNotAnswering`;
  - delete: each logout outcome returned with no `VMAccess` call after it; `TrustMissing`, a stopped
    or deallocated VM, and a status the platform refuses dispatch nothing; the logout runs before
    the backend delete with no session teardown, and the backend delete runs whatever the logout
    did; without `--force` a binding failure or a refused delete keeps the row and the record with
    the finishing hint, the rekey aside only after a successful logout; `--force` removes both past
    either, warning with the backend's identifiers, and runs no logout past a binding failure;
  - `vm shell --platform`: Proxmox refuses before the boundary resolves anything; `--workspace` is
    refused; a 255 prints the notice after the guard.

**Conformance vectors** for the channel, added to the execution LLD's list. **L** runs
`run_process(["/bin/bash", "-s"], ...)` locally on every Linux CI host, and under `sudo -n` where
the runner grants it; **V** runs through each platform's real `run_as_root` from the tester's
workstations.

| Vector                                                                                                                                                                                                                                                   | Lanes                               |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------- |
| W1 A payload that reads stdin gets end of file; a 16 KB script, larger than the bootstrap and under QGA's 65,536-character cap, runs whole                                                                                                               | L V                                 |
| W2 A script cut short anywhere before its closing brace runs nothing, not even the start marker                                                                                                                                                          | L                                   |
| W3 The secret reaches the payload and not its children's environment; during a join, no `/proc/*/cmdline` on the guest holds the key, and the guest argv is `/bin/bash -s`                                                                               | L V                                 |
| W4 With the trap, closing the channel mid-payload leaves it to finish: Bash and `/bin/echo` write after the close and the effect lands; the rekey script closed after its logout step still lands the join and the final restart; the outcome is unknown | L; V on Lima, Remote Lima and Azure |
| W5 Status 0, 1 and 255 from the payload come back as themselves                                                                                                                                                                                          | L V                                 |
| W6 Each core script runs to status 0 on a Debian 12 and 13 guest, the repair with python3 absent and with only `python3-minimal`                                                                                                                         | V                                   |
| W7 WSL2: the payload has no controlling terminal (`! { tty -s </dev/tty; } 2>/dev/null`); a root startup file does not run under `--exec`; killing `wsl.exe` mid-payload, recorded                                                                       | V on WSL2                           |
| W8 WSL2: a missing distro's `wsl.exe` error reads as text in `channel_tail`                                                                                                                                                                              | V on WSL2                           |
| W9 `BackendState` reads `NeedsLogin` for a node removed in the admin console and for an expired ephemeral key, and `Running` for a healthy node                                                                                                          | V                                   |

**Existing tests that change**, including:

- the `native_transport` fakes and the hint and route uses: `tests/test_vm_shell_platform.py`,
  `test_wsl2_keepalive.py`, `plugins/_fixtures.py`, `vms/test_tailscale_logout_wording.py` (replaced
  by `vms/test_delete_logout.py`);
- the legacy transports' tests: `transports/test_sensitive_stdin.py`,
  `transports/test_factories.py`, `plugins/test_proxmox_transport.py`,
  `transports/test_{lima,remote_lima,wsl2}.py`;
- the creates and the join: the five `*_create_interrupt.py` (Azure, WSL2, Proxmox, Lima, Remote
  Lima), `test_tailscale_stdin_join.py`, `test_bootstrap_script.py`'s key cases, and the staging
  tests (`plugins/test_proxmox_bootstrap_staging.py`, `vms/test_bootstrap_file_lifecycle.py`),
  replaced by the `run_as_root` bootstrap tests;
- the start and gate paths: `test_ensure_tailscale_wording.py`, `test_delete_vm_gating.py`,
  `test_vm_nodes.py`, and the `_GateSecrets` and batch-gate tests;
- every vm-platform fake, which implements `run_as_root` and declares `recovery_needs_guest_sshd`.

## Live evidence

The plan's cells, with what each shows (R11). Every platform from at least one workstation, Windows
among the clouds and against WSL2, and Remote Lima from macOS and Windows:

- create on every platform: the key-free bootstrap on WSL2 and Proxmox with its output size
  recorded, the join over `run_as_root`, and on one cloud the admin console showing the key used
  once (I10, I24);
- a Lima create on an Apple Virtualization host, where the SVE mask's restart runs: the join
  succeeds the first time, after the `tailscaled` wait;
- rekey, delete's logout and `vm recover` on every platform, and delete of a cloud VM with no
  record;
- `vm start` after the node is removed in the admin console, and after an ephemeral key's node
  expired: the recover hint, then recover joins at once;
- a broken sshd on WSL2 and on Proxmox (`ssh.service` masked and stopped), repaired by recover;
- a lost store record on one cloud and on Remote Lima, re-established against the console;
- a pre-python3 VM (WSL2's root filesystem) recovered, then reinitialized;
- `vm shell --platform` wherever it exists, and Proxmox's refusal;
- the V cells of vectors W1 and W3 to W9.

## Incident audit

The entry points this slice moves are the native transport factory, each platform's create and
native access, `vm start`, the gate's auto-start, rekey, delete's logout, `vm shell --platform` and
the batch gate. The audit read their history, including `21bb259f9` (logout under nohup on Azure),
`fae1d7dd2` and `cdd7a0045` (the rekey sequence), `1ae933a88` (WSL2's synchronous, terminal-free
bootstrap), `3b8a73624` (closed stdin on local transports), `ebb2e6fdf` (#350), `aa8a5540f` (#251),
`102adcff5` (the batch late-resolve guard) and `caca2709f` (key-free provider payloads). Rows I10,
I17 and I18 are already slice 4's. New rows for the HLA inventory:

| Row | Behavior                                                                                                                                                                                                 | Re-proved in |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ |
| I29 | Rekey restarts `tailscaled` before the logout and after the join, with 15-second pauses, because a stale daemon hangs the logout and a rekeyed node can be unreachable (`cdd7a0045`, `power.py:489-518`) | 4            |
| I30 | Over an SSH channel, a logout or rekey whose channel drops mid-command still finishes on the guest (`21bb259f9`, `fae1d7dd2`; `tailscale.py:177-181`)                                                    | 4            |
| I31 | A Tailscale key never reaches an operation log in clear; today the rejoin refuses any logger (`tailscale.py:108-118`), now the logger registers the key before its first write                           | 4            |
| I32 | A cloud VM whose provider reports no public address fails with a typed error naming the console, not a connection to an empty host (#118; `transports/__init__.py:215-228`)                              | 4            |
| I33 | A failing logout never skips the backend delete, and an operator abort still keeps the row (#329; `power.py:268-310`)                                                                                    | 4            |
| I34 | WSL2's bootstrap runs with no controlling terminal, so `dpkg` cannot stop apt with `SIGTTIN` (`1ae933a88`; `wsl2_bootstrap.py:131`)                                                                      | 4            |

Re-proofs: I10 by the provisioning test and create cells; I17 by `wait_until_answering`'s tests and
the Azure cells; I18 and I33 by the delete tests; I29 by the rekey cells; I30 by vector W4; I31 by
the wrapper test and the logger's construction-time set; I32 by the clouds' tests; I34 by vector W7.

The wording of #251 is I15's, re-proved for start; #350's order changes as stated under delete; and
the guard of `102adcff5` becomes the plain union refusal.

### Result

Audited at batch C2's code, after the native transport's deletion. Every row holds in the new path;
the live half of each proof is the slice's Live evidence.

| Row | Where it holds now                                                                                                                                                                               | Proof                                                                                                   |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------- |
| I29 | The rekey script: restart, logout under `timeout 60`, join, restart, 15 s after each (`vms/recovery.py:155-168`)                                                                                 | `test_rekey_restarts_logs_out_joins_and_restarts_with_a_pause_after_each`; the rekey cells              |
| I30 | Rekey's prologue and `tailscale_join.LOGOUT_SCRIPT` set `trap '' HUP PIPE` and write nothing until done                                                                                          | Vector W4 (`conformance/root_vectors.py:128`, `:267`): L on CI, V on Lima, Remote Lima and Azure        |
| I31 | `root_script.run` redacts the secret from every field and record (`execution/root_script.py:142`); `_operation_log` registers the key before the first write (`vms/manager/_helpers.py:215-223`) | The wrapper tests                                                                                       |
| I32 | `PublicAddressPlatform._public_ssh` raises `StateError` with `public_address_hint` before any connection (`capabilities/vm_platform/public_ssh.py:209-219`); GCP's read raises `GCEError` itself | `test_a_cloud_vm_with_no_public_address_is_a_state_error`                                               |
| I33 | The logout is a best-effort span inside a hold-only span that re-raises only `UserAbort`; `platform.delete` follows it (`delete_vm`, `vms/manager/power.py`)                                     | `tests/vms/test_delete_logout.py`, `tests/vms/test_delete_vm_gating.py`                                 |
| I34 | WSL2's root channel is `wsl.exe --user root --exec setsid -w /bin/bash -s` over a pipe: no sudo, and Bash in a session with no controlling terminal (`capabilities/vm_platform/wsl2.py:66-87`)   | Vector W7 (`conformance/root_vectors.py:180`), V on WSL2; its root-startup-file half is checked by hand |

The deleted entry points carried this other incident-derived behavior:

- **The factory's propagation probe (I17)** survives as `PublicSSH.wait_until_answering`, which
  `_public_ssh` runs before every cloud script and native shell. Its retry over the other platforms
  (Lima, Remote Lima, WSL2 and Proxmox) is retired: a channel with no firewall allow has nothing to
  wait for, so it fails or answers at once. The probe now runs `true` under `sudo -n /bin/bash -s`
  rather than `echo ok` as the admin, which the VM contract's passwordless `sudo` check already
  requires.
- **The attach notice after the guard (I6)** survives in `_shell_native`, for every platform's
  native shell rather than only the SSH-backed transports, worded without claiming the cause.
- **The interactive hop's keepalives** survive: Remote Lima's host argv and the clouds' VM policy
  both send `ServerAliveInterval=15` and `ServerAliveCountMax=4`, the legacy
  `SSH_INTERACTIVE_ALIVE_*` values.
- **Closed stdin on the local transports (`3b8a73624`)** survives in the root channel, whose wrapper
  runs `exec </dev/null` and whose `run_process` writes the script and closes stdin. The native
  shell inherits the operator's terminal on purpose.
- **The execution-only refusal of `vm shell --platform`** (the factory's `Transport` type check
  after construction, and `native_shell_unavailable_hint` before it) becomes the class-level
  `require_native_shell` check, before the boundary.
- **The native shell's environment, workspace `cd` and `--workspace` combination** are retired
  deliberately: the native shell composes no environment and resolves no secret (R7), and
  `--workspace` with `--platform` is refused with the hint to `cd` inside the shell.

## Collateral

- **ADRs** (status blocks): ADR 0003's firewall bullet names native-transport sessions
  (`docs/adrs/0003-use-tailscale-for-vm-networking.md:34-35`), and its ephemeral-key bullet's
  "automatic rejoin on start" (`:36-37`) becomes: ephemeral keys stay supported; a VM whose node was
  removed while offline fails `vm start` with the recover hint, and `agw vm recover` rejoins it. ADR
  0019 keeps the rejoin key's conditional late acquisition (`:102-106`). Both get an "Amended"
  paragraph.
- **Platform author documentation:** `capabilities/vm_platform/README.md` and `plugins/README.md`
  state the `run_as_root` contract once, with the other members; the kind's prose (`kinds.py:53-77`)
  and the `TopicProse` that describes a native transport (`lima.py:173-189`,
  `plugins/proxmox/platform.py:128-153`) follow, as does the `required_attributes` comment
  (`kinds.py:139`), which says the set is empty.
- **Command reference** (`cli/command-reference.md`): a `vm recover` section and table row;
  `vm start` no longer rejoins; `vm rekey`; `vm shell --platform` (`:753-769`) without the
  `--workspace` combination or the native transport wording; the reinit paragraph that sends a lost
  key to `vm shell --platform` (`:700`) and the exec paragraph that names reinit for python3
  (`:743`), both now `agw vm recover`.
- **READMEs:** `README.md:229-241` (provisioning's native transport; "re-joins on `vm start`" goes,
  and the ephemeral-key sentence says what ADR 0003's amendment says); `cli/README.md:581` (restore
  a lost key with `vm recover`); `cli/agentworks/execution/README.md` (`root_script`, the host argv,
  `FeatureUnavailable`).
- **Sample config:** `cli/agentworks/sample-config.toml:170-172` loses the lazy Tailscale repair
  sentence.
- **Completions** (`completions/spec.py:225-231`): `("vm.recover", "name"): "vms"`.
- **Guides:** `docs/guides/proxmox.md:201-205` (no start-time rejoin); the troubleshooting topic
  (`guide/guide-content/troubleshooting.md:31-42`), naming recover for python3 and Tailscale repair;
  an operator section on recovery beside the Tailscale concept topic's rekey;
  `docs/guides/resources.md:805-808` and `orchestration/README.md:97-98` (no lazy exception);
  `docs/guides/upgrading-to-0.20.md`.
- **Reviewer persona:** `.rulesync/subagents/agentworks-reviewer.md:191-194` loses the sanctioned
  conditional-repair exception; regenerate with rulesync.
- **Docstrings and comments:** `transports/__init__.py:1-21`, `transports/base.py:3-9`,
  `transports/ssh.py:87`, `vms/initializer/tailscale_preflight.py:1`,
  `vms/manager/inspect.py:382-388`.
- **The commit** carries `BREAKING CHANGE:` footers (`CONTRIBUTING.md:69`) for the platform
  contract, `vm start` no longer rejoining (ephemeral keys included), and the
  `--platform --workspace` refusal.

## Amendments

These changes were folded into `hla.md`, `plan.md`, `contract-lld.md`, `execution-lld.md` and
`trust-lld.md`, which now describe the result; the list is kept as the record of what changed. Two
edits rode along in the fold: the HLA's and plan's slice 8 now delete the remaining legacy modules,
since this slice deletes the Proxmox transport, and the HLA's slice 3 migration item states the
version move from 1 to 2, which a formatter had turned into a list number.

- **HLA Components.** Add `execution/root_script.py` and `capabilities/vm_platform/lima_host.py`;
  `vms/recovery.py` holds the three scripts, and `vm recover` is `vms/manager/recover.py`.
  `RecoveryResult` is `RootScriptResult` throughout the HLA and plan.
- **`run_recovery`, HLA and plan.** It is `root_script.run` with `vms/recovery.py`'s per-operation
  functions: `hla.md:76`, `:521`, `:528`, `:597`, `:754`, and `:602-608`, whose `sh` wrapper is the
  Bash wrapper above; `plan.md:266`, `:279` and `:294`.
- **HLA D2.** The host policy is for a platform's own SSH to its host, Remote Lima's, and drops "or
  to a VM's public address"; it is `build_host_argv` with fixed `HOST_TRUST`. The clouds' public
  address uses the VM policy, at the price stated under The host policy. `hla.md:760` and
  `plan.md:281-282` drop `public_ssh.py` as a host-policy caller.
- **HLA D9, Re-establishment.** `read_host_keys` returns `tuple[HostKey, ...]` from a base-class
  default; the script and parser are in `root_script.py`; the clouds read under a
  `provisioning_binding` file; no provider fingerprints. Line 506's "From PR B, re-establishment
  (below) is its recovery path" for a create retained as failed becomes delete and recreate.
- **HLA D11.**
  - Table: `run_as_root(vm, ctx, script, deadline) -> RawRun` with no separate stdin, running
    `/bin/bash -s`; `RawRun` has no status; `native_shell_unavailable_hint` stays as guidance;
    `run_as_root` and `recovery_needs_guest_sshd` are descriptor requirements;
    `ProvisionRequest.ssh_private_key` is `Path`.
  - Prose: the wrapper is shared with provisioning and embeds the auth key, which reaches Tailscale
    through `--auth-key=file:/dev/stdin`; only logout and rekey ignore `SIGHUP` and `SIGPIPE`, and
    they print their captured output once at the end; the cloud channel is the VM policy with the
    constant `sudo -n /bin/bash -s`; scripts fail with exit 1 and a reason, which reaches the
    operation log, never an error. Rejoin is no longer its own script: the repair joins only on
    `NeedsLogin`, installs python3 when agw-launch's interpreter does not run, repairs sshd by
    unmask, enable, configuration test and restart, and loses "reloads sshd on every platform" and
    "prints the public host keys"; recover then reads the address (settling only after a join) and
    waits once. Recover refuses a VM that is not running and resolves the key in its boundary;
    delete does not log out a cloud VM with no record and names the node. The per-platform table
    takes The channels' rows.
- **HLA D13.** `FeatureUnavailable` lands with `vm shell --platform`.
- **HLA D14.** The guard imports `public_ssh` and `vms.recovery` explicitly; the plugins join the
  static check instead.
- **HLA Migration, slice 4.** WSL2 and Proxmox run a key-free bootstrap through `run_as_root`, and
  the legacy platform transports and `plugins/proxmox/transport.py` are deleted.
- **HLA Incident inventory** gains I29 to I34. **ADR amendments** gains 0003 and 0019 in slice 4,
  and "ADR 0003 needs no change" (`hla.md:855-856`) is replaced: ADR 0003 promises a rejoin on
  start.
- **Plan slice 4.** Link this LLD. Add `root_script.py`, `lima_host.py`, `FeatureUnavailable` and
  the recover and rekey operation logs to the implementation items; drop the "each platform declares
  sshd dependence" test and add this LLD's tests and vectors W1 to W9; add the deletions under
  Independence, `Resolver.resolve_late_repair`, the bootstrap generator's key parameter and step 7,
  `TAILSCALE_JOIN_STDIN_COMMAND` and the staging files, and drop `native_shell_unavailable_hint`
  from them; add the Collateral items and Live evidence cells the plan does not list.
- **Plan slice 8.** `agentworks.plugins.proxmox.transport` is gone already.
- **contract-lld.md** (same PR).
  - Split the helper: `read_tailnet_address(run_as_root, *, node)` and
    `settle_tailnet_address(run_as_root, *, node, rerun_hint)`, which replace the callback form and
    `settle_on_transport`; the `run_as_root` implementations land first, so neither is written.
    Create and rekey settle; recover settles only after it joined.
  - The failed-read predicate (`:131-133`) and its test (`:595-597`): a non-zero status, an unknown
    outcome, a `ValueError` or a platform `ConnectivityError` is a failed read, and any other
    `AgentworksError` propagates. Exhaustion raises `ConnectivityError`, not `ProvisioningError`.
  - The per-platform "Join / Settled with" table, the "WSL2 and Proxmox transports with `sudo=True`"
    paragraph and "the auth-key transport of the first join ... is not touched" (Scope) go;
    bootstrap step 7 is deleted rather than shortened. After a successful exhaustion logout, the
    message gives the caller's `rerun_hint` where it says create can be run again.
- **trust-lld.md.** Lines 171-172: a create that fails before enrolling stays delete and recreate
  from PR B too. Delete's warning for a cloud VM with no record names the node and opens no route.
- **execution-lld.md.** The module table (`:46-55`) gains `root_script.py` and `trust.py`'s
  `fingerprint`. The host argv and `FeatureUnavailable` land here; `PipeSession` accepts
  `nonce=None`; `spawn` raises `ConnectivityError` for a program other than OpenSSH.
- **FRD.** None: R7's "finite sensitive stdin" is the script that carries the key, and R8 lets a
  public-address connection honor operator configuration without requiring it.
