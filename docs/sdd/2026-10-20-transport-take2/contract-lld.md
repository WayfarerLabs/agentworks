# Transport Take 2: Contract Low-Level Design (Slice 3)

- Status: Draft, awaiting review.
- Date: 2026-11-01
- Architecture: [hla.md](./hla.md) D4 (elevation), D5 (observation), D8 (reachability), D10 (the
  contract), D11 (first half), D13 (the operation log); requirements: R1, R4, R7, R10, R11
- Plan: [plan.md](./plan.md), slice 3, delivered in PR B with slice 4
- Source baseline: `f3499ea81` (the PR A head, with slices 1 and 2 implemented). Every `path:line`
  below is at that commit unless it says otherwise.

## Scope

This LLD settles the details the HLA leaves open for first contact with a new VM and the contract
every VM must satisfy:

- the provisioning additions: python3, and the required, validated `tailnet_address`;
- Phase A on `VMAccess`: the first connection in Enroll mode, release attestation inside the
  fail-closed span, and `vm confirm-release` and reinit's release probe off the legacy stack;
- elevation for Phase A and the contract's sudo check, with `observation` and `reachability`;
- how the wait meets Debian 13 sshd's `PerSourcePenalties`;
- the contract's check table and diagnoses, including the pam_env warning moved from reinit;
- the operation log, `oplog.py`;
- tests, the incident audit, collateral, the ADR 0025 amendment and live evidence.

Where the HLA or plan was silent the narrowest choice is made here. Choices that are the lead's or
the operator's are in Open questions, each with a recommendation. Amendments the other SDD files
need are listed at the end, as [execution-lld.md](./execution-lld.md) lists its own.

**Out of this slice.** Phase B and the initializers stay on the legacy stack until slice 6 (they
receive a legacy `Transport`, `driver.py:248`). `native_transport` the platform method, rejoin,
rekey, logout, `vm start`'s wait and the activation gate stay until slice 4, so the legacy
`wait_for_reconnect` and `TailscaleWait` (`transports/__init__.py:248`, `:279`) keep their callers
(`vms/manager/tailscale.py:127`, `:149`, `vms/manager/power.py:550`) and are deleted there. The
auth-key transport of the first join (the stdin command in `tailscale_join.py:15`, WSL2's staged
script, Proxmox's guest-agent script) is not touched except where a retry or a logout is added after
the join succeeded.

## Module layout

| Module                                                                                      | Contents                                                                                                                    |
| ------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `agentworks/vms/contract.py` (new)                                                          | The check table, `observe_release`, `check_base`, `check_initialized`, `BaseReport`, `Finding`, `enforce`                   |
| `agentworks/execution/reachability.py` (new)                                                | `Wording`, `wait`                                                                                                           |
| `agentworks/execution/oplog.py` (new)                                                       | `LOG_DIR`, `OperationLog`, `logs_for`, `latest_log`                                                                         |
| `agentworks/execution/access.py`                                                            | `enrollment_access`, `VMAccess` gains `log`, `may_elevate` and a trust mode; `vm_access` gains `log`                        |
| `agentworks/execution/requests.py`                                                          | `elevated` and `observation` on `BoundedRequestOptions`                                                                     |
| `agentworks/execution/ssh_process.py`, `envelope.py`, `results.py`, `errors.py`             | `stage0_command(elevated=)`, the `AUTH_FAILED` classification, `PermissionDenied`, the `ExecutionIncomplete` wording        |
| `agentworks/capabilities/vm_platform/base.py`, `tailscale_join.py`, `bootstrap_script.py`   | `ProvisionResult.tailnet_address`, `parse_tailnet_address`, `settle_tailnet_address`, the script's address loop             |
| `agentworks/capabilities/vm_platform/cloud_init.py`                                         | python3 in `PROVISIONING_PACKAGES`; `INIT_SYSTEM_TOOLS`, from which `INIT_SYSTEM_PACKAGES` is derived                       |
| The six platforms' `create`                                                                 | Return `tailnet_address`; Lima and the clouds call the shared settle helper                                                 |
| `agentworks/vms/initializer/driver.py`, `agentworks/vms/manager/lifecycle.py`, `release.py` | Phase A on `VMAccess`; the contract at create, reinit and the end of Phase B; release reconciliation split from observation |
| `agentworks/ssh.py`                                                                         | `SSHLogger` delegates its sink to `OperationLog`; `ssh.LOG_DIR` is deleted                                                  |
| `agentworks/debian.py`                                                                      | `probe_debian_release` deleted (no caller remains); `verify_os_release` stays                                               |
| `agentworks/vms/manager/pam_env.py`                                                         | Deleted; its probe and parser become the contract's pam_env row                                                             |

`contract.py`, `reachability.py` and `oplog.py` import nothing from the six legacy modules (R10).
`contract.py` joins the package guard (HLA D14) and `release.py`, `driver.py`'s Phase A section and
the other migrated callers join the static check as they stop importing legacy modules (below). The
fresh-interpreter guard walks only `agentworks.execution` (`tests/execution/test_independence.py`),
so it gains an explicit import of `agentworks.vms.contract`; `agentworks/vms/__init__.py` is empty,
so that import pulls in nothing else. The `agentworks.execution` package's lazy `vm_access` wrapper
(`execution/__init__.py:19`) gains the `log` keyword and a sibling `enrollment_access` wrapper.

## Provisioning

### python3

`PROVISIONING_PACKAGES` (`capabilities/vm_platform/cloud_init.py:16`) gains `python3`. Every
platform's bootstrap installs the list in one `apt-get install` (`bootstrap_script.py:117`), and the
six consumers already pass it (`lima.py:391`, `wsl2_bootstrap.py:65`, Proxmox `platform.py:404`,
Azure `:420`, AWS `:424`, GCP `:307`). Debian's `python3` package depends on the full standard
library, so the `python3-minimal` case of D4 cannot arise from a provisioned VM.

Init keeps installing it, as the operator ruled on 2026-11-01. `INIT_SYSTEM_PACKAGES` (`:27`) is a
list of package names that includes `acl`, whose command is `setfacl`, so the initialized checks
cannot derive command names from it. The list becomes derived from one mapping of package to the
command that proves it is installed, in the same module:

```text
INIT_SYSTEM_TOOLS = {"python3": "python3", "git": "git", "unzip": "unzip", "tmux": "tmux",
                     "tmuxinator": "tmuxinator", "acl": "setfacl", "jq": "jq", "mise": "mise",
                     "zstd": "zstd"}
INIT_SYSTEM_PACKAGES = list(INIT_SYSTEM_TOOLS)
```

One declaration, so a package added to init is checked by the initialized phase without a second
edit. The installer (`vms/initializer/packages.py:152`) still reads `INIT_SYSTEM_PACKAGES`.

### `ProvisionResult` and the address

`ProvisionResult` (`capabilities/vm_platform/base.py:104`) becomes

```text
@dataclass
class ProvisionResult:
    tailnet_address: str
    platform_metadata: dict[str, str] = field(default_factory=dict)
```

`native_transport` and `tailscale_ip` are deleted. Every construction site is keyword-only already
(`lima.py:465`, `wsl2.py:788`, Proxmox `platform.py:432`, AWS `:601`, Azure `:674`, GCP `:470`), so
the edit at each is: drop the transport, rename the address, and stop building a transport only to
return it (Lima's `self._transport_for` at `lima.py:456`, Proxmox's `target` at `:414`, the
`assert prov_transport is not None` at `aws/platform.py:599`). The clouds keep their provisioning
transport as a local, since `EphemeralTailscaleBootstrap` still uses it inside `create`.

**Validation.** `parse_tailnet_address(text) -> str` in `base.py` strips, parses with
`ipaddress.ip_address` and returns the canonical text, or raises `ValueError`. It is called in two
places, for two boundaries:

1. By the platform's own address read, inside its rollback window, so a bad read fails create
   cleanly (below). `parse_bootstrap_output` already parses candidates this way
   (`bootstrap_script.py:335-341`); it calls the shared function so one rule exists.
2. By `bootstrap_vm` before it writes the address to the row, because `ProvisionResult` is the
   platform plugin boundary (D3: arguments from a caller our type checker does not check). It is not
   in `ProvisionResult.__post_init__`: a value object that raised there would raise after the
   platform's rollback window closed (every platform builds it as its last statement,
   `lima.py:465`), and core's unwind would then delete the row for a backend VM that exists. In
   `bootstrap_vm` a bad value is an ordinary Phase A failure: the VM is retained as failed with
   delete guidance, after `secure_failed_vm`.

The check is "parses as an IP address", not "lies in Tailscale's range": a range check would reject
a valid self-hosted tailnet with another prefix, and the connection that follows is verified against
the VM-bound host key whatever the address (rejected alternative).

**Settling the address.** After a successful join, every platform must produce the address, retrying
a transient failure within its own step, and when the reads are exhausted must log the node out over
the channel that sent the key and fail create. There are two mechanisms, because two shapes of
platform exist:

| Platform        | Where the join runs                                       | Where the address is settled                                    |
| --------------- | --------------------------------------------------------- | --------------------------------------------------------------- |
| WSL2            | In the key-bearing script (`bootstrap_script.py:238-241`) | In that script, a shell loop (below)                            |
| Proxmox         | The same script, via the guest agent                      | The same script and loop                                        |
| Lima, Remote    | `limactl shell` stdin command (`lima.py:425`)             | `settle_tailnet_address` with a `limactl shell` read and logout |
| AWS, Azure, GCP | `join_tailscale_ephemerally` (`tailscale_join.py:22`)     | `settle_tailnet_address`, inside `EphemeralTailscaleBootstrap`  |

_Python platforms._ `tailscale_join.py` gets one helper:

```text
settle_tailnet_address(read: Callable[[], str], logout: Callable[[], None], *, node: str) -> str
```

It calls `read` up to `ADDRESS_READS` times, `ADDRESS_PAUSE_SECONDS` apart, accepting the first
value `parse_tailnet_address` accepts and treating `SSHError` and `ValueError` as a failed read.
`tailscale up --auth-key` returns when the node is running, so a failed read right after it is rare
and short: six reads two seconds apart (a 10 second window) is the starting budget; the numbers are
named constants, to be corrected by the live cells. When the reads are exhausted it calls `logout`
once, then raises `ProvisioningError`:

- if the logout succeeded: the node was logged out, and create can be run again;
- if it failed or raised: the message names `node` (the VM's hostname, `request.hostname`) for
  removal in the Tailscale admin console.

The exception is raised from inside the platform's existing rollback `try`, so the existing arms
(`lima.py:457-463` and the matching arms of the other five) delete the partial backend as for any
other create failure; `RetainedProvisioningError` is still raised only by a rollback that cannot be
confirmed (`base.py:265-273`). Neither the read nor the logout carries the key, so no message,
`repr` or chained exception holds it. A `KeyboardInterrupt` during the window takes the platform's
interrupt arm as today and leaves the node, as it does for an interrupt anywhere after the join;
that is not new behavior.

- `EphemeralTailscaleBootstrap.complete` (`tailscale_join.py:65`) now returns `str`: it joins
  exactly once, then returns `settle_tailnet_address(read_ip, logout, node=hostname)` with `read_ip`
  the existing `tailscale ip -4` command (`:106`, `sudo=True`, `check=True`, `timeout=15`) and
  `logout` the same transport running `tailscale logout` (`sudo=True`, `check=True`, `timeout=30`).
  Its three callers pass the hostname.
- Lima passes `limactl shell <instance> sudo tailscale ip -4` and `... sudo tailscale logout`, via
  the `_run_lima` it already uses for the join and the read (`lima.py:425`, `:449`).
- The "Phase A will retry IP discovery without the auth key" warnings (`tailscale_join.py:111`,
  `lima.py:454`) go, with the fallback they promised.

_Shell platforms._ WSL2 and Proxmox run `generate_bootstrap_script` with the key, and the script
reads the address once under `set -euo pipefail` (`bootstrap_script.py:240`): a failed read exits
the script, `parse_bootstrap_output` reports it not ok (`:319`), create rolls back, and the node is
never logged out. The script's step 7 gets the loop:

```sh
TS_IP=""
for _agw_try in 1 2 3 4 5 6; do
    TS_IP=$(tailscale ip -4 2>/dev/null) || TS_IP=""
    [ -n "$TS_IP" ] && break
    sleep 2
done
if [ -z "$TS_IP" ]; then
    echo "##ERROR## the node's Tailscale address could not be read"
    if tailscale logout >/dev/null 2>&1; then
        echo "##WARN## the node was logged out of Tailscale"
    else
        echo "##WARN## could not log the node out; remove node $VM_HOSTNAME in the Tailscale admin console"
    fi
    exit 1
fi
echo "##SUCCESS## tailscale-ip=$TS_IP"
```

The loop lives in a module constant (`TAILNET_ADDRESS_SNIPPET`) that `SCRIPT_TEMPLATE` embeds, so a
test can run it under a stub `tailscale` (below). `tailscale logout` prints nothing secret, and its
output is discarded. WSL2 already shows step warnings (`wsl2_bootstrap.py:160-170`); Proxmox's
`_run_bootstrap_via_agent` raises a bare `ProvisioningError` (`platform.py:636`) without showing
steps, so it appends the `##WARN##` lines the script printed to its message. The script reads the
node name from the `VM_HOSTNAME` it already has (`bootstrap_script.py:68`).

**Why two mechanisms.** A script that has just run the join is the only place that can retry and log
out without sending the key a second time through another channel, and a Python helper is the only
place the Python platforms can do it. Unifying them would mean re-sending the script or moving the
join out of the script, both worse. Both mechanisms are bounded by constants of the same order and
tested at the unit level.

**Lima ordering.** The read happens after the restart-sentinel `limactl restart` today
(`lima.py:434-454`). A restart can make the instance's shell channel flaky exactly when the read and
a possible logout need it. The node's tailnet address belongs to its node key and persists across a
restart (not verified here; see the live cell), so this LLD moves the read to directly after the
join and before the sentinel check. See Open questions.

**Interaction with create.** Core no longer reads the address at all. `lifecycle.py:599-626` (the
attestation block) and `driver.py:399-401` (the `tailscale ip -4` fallback) are deleted.

### The provisioning trust file's lifetime

`ProvisionRequest.provisioning_trust` is entered on `init_stack` so the Unverified file outlives
`create` for core's post-create use of the returned transport (`lifecycle.py:515`;
[trust-lld.md](./trust-lld.md), "Clouds before enrollment"). With no returned transport that use is
gone, so the file is scoped to the platform's `create` call: entered in a `with` that closes when
`create` returns or raises. The binding is still passed through `ProvisionRequest`. This is a
consequence of deleting `native_transport`, not an extra change.

## Requests: elevation and observation

Both options go on `BoundedRequestOptions` (`requests.py:61`), beside `deadline`, which `LoginShell`
already lacks:

```text
elevated: bool = False
observation: bool = False
```

**Elevation.** `elevated=True` runs the whole operation under `sudo -n`. It is not in the envelope:
it is the prefix of the stage 0 command (D4). `stage0_command` (`ssh_process.py:64`) gains
`elevated: bool = False`, producing `sudo -n /usr/bin/python3 -I -S -c '<stage 0>'`, and raises
`ValueError` when `elevated` and `handover_socket` are both given, so D4's "never combined" is
enforced by the one function that builds the line, not by a caller's care. The root-owned launcher
then rebuilds its environment from the passwd entry of uid 0 (`agw_launch.py`, step 4 of the
execution LLD), so nothing has to survive `sudo`'s `env_reset`.

- **Agent elevation.** `VMAccess` gains `may_elevate: bool = False`, set by `vm_access` from
  `isinstance(identity, Admin)`. `run` and `stream` check it first, before `resolve_ssh()`, and
  raise `PermissionDenied` (new in `errors.py`, subclassing `AuthorizationError`, naming the VM and
  the account, never the request). Defaulting to refusal keeps a directly constructed `VMAccess`
  from ever being an administrator by omission. The guest's `sudo -n` remains the second layer.
- **Elevated attach.** `attach` takes `LoginShell`, which inherits `RequestOptions` and not
  `BoundedRequestOptions`, so an elevated attach cannot be expressed and needs no runtime refusal,
  as `deadline` on an attach needed none (execution LLD, requests). The plan's "elevated attach
  refused" is satisfied by that structure plus the `stage0_command` invariant; no test restates the
  type. See Amendments.
- **`use_pty`.** The execution LLD (Left to the first consumer) flags that a sudoers `use_pty`
  default would turn descriptors 1 and 2 into a pty, which the stream check refuses
  (`agw_launch.py:177-188`). sudoers(5) says "If the sudo process is not attached to a terminal,
  use_pty has no effect", and an elevated request is always `ssh -T` with no controlling terminal
  (`ssh_process.py:172`). The only way to attach sudo to a terminal is a terminal-mode connection,
  which is exactly the elevated attach that cannot be expressed. The hazard therefore cannot arise
  for any request this slice can express. This rests on the documentation, not on a measurement:
  Debian's default sudoers could not be read here (`/etc/sudoers` is root-only on the dev host), so
  the fresh-VM vectors below record `sudo -V` and the effective `use_pty` on Debian 12 and 13. With
  sudo I/O logging on (`log_output`, `log_stdin`), sudo relays the standard streams over pipes it
  creates, which the check accepts and D3 already notes.
- **Failure shape.** `sudo -n` without passwordless sudo exits 1 with no start marker, which the
  classifier reports as `NotDelivered(LAUNCHER_FAILED)`. Under `sudo` a missing python3 also exits 1
  rather than 127 (D4), so the unelevated request always runs first and types the missing python3;
  an elevated `LAUNCHER_FAILED` after it is the sudo policy.
- **Phase A's elevated request** is the contract's sudo check,
  `Command(("id", "-u"), elevated=True)`: it proves the admin account's sudo policy and, on a fresh
  VM with the distribution default sudoers, the whole elevated path Phase B will rely on (sudo,
  stdin passed through, the stream check, a root-owned launcher).

**Observation.** `observation=True` declares that the request only reads, so dispatching it again
after an ambiguous outcome is safe (D5). `VMAccess` never retries; the one consumer in this slice is
`reachability.wait`, which refuses a request without the flag. The flag is client-side policy: it is
not in the envelope. The contract's checks do not set it (they are not polled).

## Authentication failures and sshd's per-source penalties

Debian 13's sshd (OpenSSH 10.0p1; read from the trixie `sshd_config(5)` page, not measured on a live
host) enables `PerSourcePenalties` by default. The documented defaults are `crash:90s`,
`authfail:5s`, `noauth:1s`, `grace-exceeded:10s`, `max:10m` and `min:15s`: penalties accumulate per
source address, up to `max`, and are enforced only once the accumulated penalty reaches `min`; an
enforced source has its connections dropped. `noauth` is a client that disconnects without
attempting authentication, which our `ConnectTimeout` and deadline kills can be. Debian 12's sshd
(9.2) has none of this (the feature arrived in OpenSSH 9.8, from memory of its release notes; the
Debian 12 live cell confirms it).

The hazard is a polling loop that turns a persistent authentication failure into a lockout of the
workstation, after which even a correct key fails the same way and the loop cannot tell. Two rules
keep the wait from creating one:

1. **An authentication failure ends the wait at once.** `Permission denied (publickey` (the constant
   `AUTH_FAILURE`) in the client log (the same ssh-diagnostic channel that already types
   `Host key verification failed`, `ssh_process.py:58`) classifies a 255 with no start marker as
   `Unknown(AUTH_FAILED)`, a new `UnknownReason` beside `HOST_KEY`. `classify` (`envelope.py:277`)
   gains `auth_failed` and the last line becomes: host-key failure, else authentication failure,
   else `CONNECTION`. `ssh_process.outcome` (`ssh_process.py:305`) passes `AUTH_FAILURE in text`.
   Polling stops after exactly one failed authentication, so the wait adds at most one `authfail` (5
   s, below `min`).
2. **The pause is not shorter than the largest per-connection penalty.** `PAUSE_SECONDS = 5` (the
   legacy value, `transports/__init__.py:317`) is at least `authfail`'s 5 seconds and far above
   `noauth`'s 1 second, so if penalties expire in real time (as an expiry-based design implies; the
   man page does not state the decay and this is unverified) no polling pattern accumulates toward
   `min`. If they do not decay, rule 1 still bounds the `authfail` cost to one failure, and what is
   left is `noauth`: only a poll that reaches sshd and is killed before authenticating adds its 1
   second, at most 16 in one wait, which sits at the 15 second `min`. The live control below
   measures which model is true.

Connection-level failures stay retryable by polling: a dropped or reset connection is
indistinguishable from a VM that is still coming up (`Unknown(CONNECTION)`), and an enforced lockout
looks like one. After a wait is exhausted (about 240 s) its caller's error says that sshd may be
refusing this workstation temporarily, that Debian 13 releases such a source within the default ten
minutes at most, and to retry later. The text is conditional because it cannot be told from a
stopped VM.

`AUTH_FAILED` needs the rest of the vocabulary to follow:

- `Result.require_completed` (`results.py:83`) falls through to `ExecutionIncomplete`. Its wording
  for this reason says the VM did not accept the configured SSH key and that nothing ran, with the
  conditional passphrase hint appended as for `CONNECTION`.
- `_passphrase_hint_on_connection_failure` (`access.py:196`) adds the passphrase hint for
  `Unknown(CONNECTION)` today. An encrypted key the agent does not hold fails as
  `Permission denied (publickey)`, so after this change that is `AUTH_FAILED` and the hint would
  silently stop appearing in the case it exists for. The condition becomes `CONNECTION` or
  `AUTH_FAILED`.
- The existing vectors and the live cell that confirm the client log captures ssh's diagnostics on
  8.5, 9.x and Win32-OpenSSH (execution LLD, Live evidence) also confirm this token.

## Reachability

`execution/reachability.py` replaces `transports.wait_for_reconnect` for create; slice 4 moves the
rest. The measured legacy behavior (`transports/__init__.py:279-318`):

- at most 16 attempts; each runs `echo ok` with a 10 second limit; a failure sleeps 5 seconds (also
  after the last);
- a success on the first attempt returns at once; a success after any failed attempt sleeps 2
  seconds and probes again, and only a second success returns (a failure there is one more failed
  attempt);
- the exhausted wait warns "Tailscale SSH did not reconnect after ~240s, proceeding anyway" and
  returns `False`; the success line comes from the closed `TailscaleWait` pairs.

The new module keeps these numbers (`ATTEMPTS = 16`, `PROBE_DEADLINE = 10`, `PAUSE_SECONDS = 5`,
`CONFIRM_PAUSE_SECONDS = 2`, so about 235 to 240 seconds, incident row I15) and drops only the sleep
after the last attempt and the warning, which belongs to a caller that knows whether it proceeds.

```text
class Wording(Enum): RECONNECT, VERIFY     # the same two (waiting, reached) pairs as TailscaleWait
PROBE = Command(("true",), deadline=10, observation=True)

def wait(access: VMAccess, *, wording: Wording = Wording.RECONNECT, probe: RunRequest = PROBE) -> bool
```

`wait` raises `ValueError` if `probe.observation` is false, so a mutating request can never be
re-dispatched by it. Each attempt is one `access.run(probe)`, classified from a whitelist:

| Outcome of the attempt                                                                   | Action                                                             |
| ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| `Completed(0)`                                                                           | The VM answered                                                    |
| `NotDelivered(LAUNCHER_FAILED)`; `Unknown` with `CONNECTION`, `TRUNCATED`, `DEADLINE`    | Not yet: keep polling                                              |
| Anything else (`HOST_KEY`, `AUTH_FAILED`, python3 missing, a refusal, a non-zero `true`) | Raise at once through `Result.require_completed` / `CommandFailed` |

A failure that polling cannot fix is raised rather than waited out: the legacy wait spent 240
seconds on a changed host key. Errors raised before dispatch (`TrustMissing`, `ClientUnsupported`)
propagate from `access.run` on the first attempt, as `wait_for_reconnect` already did for
`TrustMissing` (`transports/__init__.py:292`). A source refused by Debian 13's sshd looks like
`CONNECTION` and is polled (above). The function returns `True` when the VM answered and held, and
`False` when the attempts ran out. Callers decide: create's first contact raises, its post-ready
wait warns and proceeds as today (`driver.py:225-227`), and slice 4's `vm start` hints at
`agw vm recover`.

Tests patch `agentworks.execution.reachability.time.sleep` and use the recording fake `VMAccess`
(`tests/vm_access_support.py`) with a scripted sequence of results.

## First contact: Phase A on `VMAccess`

### Entry

`vm_access(vm, config, identity)` builds a VM-mode `VMAccess` (`access.py:312`; `_policy` is
hard-wired to `TrustMode.VM` at `:207`). The first connection to a new VM must be Enroll mode. The
row already has the address when Phase A connects (it records it first, `driver.py:403`), so the
row-based construction needs only a different trust mode, not a different source for the host.
`VMAccess.__init__` gains `trust_mode: TrustMode = TrustMode.VM` (`_policy()` uses it), and the
package gains a second, deliberately differently named factory:

```text
execution.enrollment_access(vm, config, *, log=None) -> VMAccess   # admin, TrustMode.ENROLL
```

It is a separate function rather than a parameter of `vm_access`, so the one call that may accept a
new host key is easy to find by search and does not look like the ordinary factory (principle 2; HLA
D9 "the code that builds a connection chooses one explicitly"). Its docstring says create's first
connection is its only caller. `ENROLL` still covers exactly one connection's job: after it, Phase A
checks the store holds a key (kept, `driver.py:438`) and every later request uses `vm_access` in VM
mode.

### The sequence

`bootstrap_vm` (`driver.py:94`) loses `exec_target` and `tailscale_ip` and gains
`tailnet_address: str` and `expected_release: DebianRelease`; it returns
`(Transport, home, DebianRelease)` (the release replaces the `observed_release` local in
`create_vm`, `lifecycle.py:628`). The fail-closed span (`driver.py:159-211`) wraps the whole
sequence, so the attestation that is outside it today (`lifecycle.py:599-626`) is inside:

1. **Record.** `parse_tailnet_address(tailnet_address)`; mark provisioning `IN_PROGRESS`; record the
   address (`db.update_vm_tailscale`); mark provisioning `COMPLETE`. The order is today's
   (`driver.py:396-404`), so an interrupt leaves the VM `COMPLETE` and reinit-able exactly as an
   interrupt during today's verification does (a change for one case: below).
2. **First contact.** `access = enrollment_access(vm, config, log=oplog)`;
   `reachability.wait(access, wording=VERIFY)`. `False` raises `ProvisioningError` ("the VM did not
   become reachable over SSH at ADDRESS"), with the penalties sentence above. This replaces the five
   15-second attempts (`driver.py:426-435`) with the common bounded wait; a cold tailnet path can
   need longer than 90 seconds, and the cost of the longer bound is only a longer failure.
3. **Enrollment post-condition.** `trust.has_record` as today (`driver.py:438`).
4. **Contract.** `access = vm_access(vm, config, Admin(), log=oplog)`;
   `report = contract.check_base(access, expected_release=expected_release, agents=())`; record the
   observed release (`db.update_vm_debian_release`); `contract.enforce(report, vm_name)`. The
   release is therefore the first contract request, directly after the first connection, as R1 and
   D10 require; a release that fails raises before anything else is asked of the VM.
5. **Complete.** The `provisioning_complete` event, then the `on_tailscale_ready` hook, then
   `reachability.wait(access, wording=RECONNECT)` whose `False` warns "proceeding anyway" (today's
   behavior, `driver.py:213-227`), then the SSH-config sync (unchanged, non-fatal).
6. **Hand Phase B its legacy target**, `SSHTransport(..., trust=VM, logger=logger)`, as
   `_tailscale_ssh(TrustMode.VM)` does today (`driver.py:409-421`, `:445`).

Any `Exception` in steps 1 to 4 takes the existing arm: provisioning `FAILED`, the
`provisioning_failed` event, `platform.secure_failed_vm`, the log pointer, re-raise
(`driver.py:175-211`). Any `BaseException` still secures the VM and re-raises. The provisioning
event's detail was the native transport's description (`exec_target.describe()`, `driver.py:149`);
it becomes `ssh ADDRESS`.

**What changes for an interrupt.** Today a Ctrl-C during attestation marks the row `FAILED` and
points at `vm delete` (`lifecycle.py:611-617`), and one during Tailscale verification leaves it
`COMPLETE`. With attestation in the span the interrupt arm applies to both, leaving `COMPLETE` with
the release unrecorded. `vm reinit` then runs the same base checks (including the release
observation) and converges. That is reinit-able rather than delete-only, and not less safe: the VM
never reaches Phase B unverified, since reinit's first act is the contract. See Open questions.

### Create and reinit

`create_vm` (`lifecycle.py`): the attestation block and the `probe_debian_release` import go; the
`_mgr.bootstrap_vm(...)` call passes `platform_obj`, `platform_ctx`, `logger`,
`tailnet_address=result.tailnet_address` and `expected_release=creation_release`; its result's
release feeds `run_initialization(debian_release=...)` (`:684`).

`reinit_vm` has no Phase A and no enrollment. After the logger exists (`:949`) and before
`run_initialization`, it replaces the legacy `verified_vm_release` and `warn_pam_env_settings` calls
(`:963-965`) with:

```text
access = execution.vm_access(vm, config, Admin(), log=logger.operation_log)
report = contract.check_base(access, expected_release=None, agents=agent linux users)
release = release_module.reconcile_release(db, vm, report.release)   # refuses live drift, records
_warn_newly_observed_legacy(vm, release)
contract.enforce(report, name)
```

The agents come from `db.list_agents(vm_name=name)` (`db/database.py:642`). Release drift refuses
before other findings are shown (it names `vm confirm-release`, `release.py:30-36`), then legacy
release warning, then warnings and failures. A reinit that finds a failure stops before Phase B.

## The VM contract

`vms/contract.py` holds the table. A reader who has seen one row can add another.

```text
class Phase(StrEnum): BASE, INITIALIZED
class Severity(StrEnum): FAIL, WARN
class Remedy(StrEnum): REINIT_VM, REINIT_AGENT, RECREATE_VM

@dataclass(frozen=True)
class Check:
    id: str            # closed set, the only names the parser accepts
    phase: Phase
    severity: Severity
    remedy: Remedy     # what a FAIL names; WARN rows name none
    clause: str        # the R1 clause the row verifies, for the report
    probe: str | None  # a shell fragment that emits the row, or None for a row with its own request
```

### Rows

| Row            | Phase       | Severity | Remedy         | How it is checked                                                                            |
| -------------- | ----------- | -------- | -------------- | -------------------------------------------------------------------------------------------- |
| `release`      | base        | FAIL     | `RECREATE_VM`  | Own request: `Command(("cat", "/etc/os-release"))`, judged by `debian.verify_os_release`     |
| `systemd`      | base        | FAIL     | `RECREATE_VM`  | Script: `[ -d /run/systemd/system ]`, the test `sd_booted()` uses                            |
| `sudo`         | base        | FAIL     | `RECREATE_VM`  | Own request: `Command(("id", "-u"), elevated=True)`, stdout `0`                              |
| `login-shells` | base        | FAIL     | `REINIT_AGENT` | Script: for each agent account, the passwd shell's basename is `sh`, `dash`, `bash` or `zsh` |
| `base-tools`   | base        | FAIL     | `RECREATE_VM`  | Script: `command -v` of `install`, `flock`, `setsid`, `tar`                                  |
| `pam-env`      | base        | WARN     | none           | Script: the existing probe over `/etc/environment`, `pam_env.conf` and `~/.pam_environment`  |
| `init-tools`   | initialized | FAIL     | `REINIT_VM`    | Script: `command -v` of each value of `INIT_SYSTEM_TOOLS`                                    |

Not rows, and why:

- **python3 and agw-launch running.** The first request proves them: a missing python3 is
  `NotDelivered(PYTHON_MISSING)` and a python3 without its standard library is `STDLIB_MISSING`,
  both raised as `VMNotReady` by `require_completed` before any row is read. They are the contract's
  entry condition, not a row that could be reported beside others.
- **The admin account and its shell.** The connection authenticated as it, and stage 0 ran under its
  login shell, so that shell parses the constant line. `login-shells` is for the accounts the
  contract does not connect as, the agents; a new VM has none, so Phase A passes none.
- **Tailscale up at the recorded address.** D10 lists it. The request reached the VM at that address
  over the tailnet and verified the VM-bound host key, which a different node at a recycled address
  cannot satisfy, so the connection is the proof and a `tailscale ip -4` comparison would only
  restate it behind another `sudo`. See Amendments and Open questions.

`REINIT_AGENT` renders `agw agent reinit NAME` for each failing account; `agw agent reinit` applies
the template shell through the admin (`agents/initializer.py:160`), so the cure does not run through
the account whose shell is wrong. `login-shells` as a base row blocks a reinit on an agent whose
shell predates the `AccountShell` restriction (slice 2 did not re-check existing accounts, execution
LLD "Template `shell` fields"), by design: that agent's `agent exec` and `agent shell` would fail
anyway, and the fix is one command.

### Execution

`check_base(access, *, expected_release, agents) -> BaseReport` runs, in order:

1. `observe_release(access, expected=expected_release)`: one request, `cat /etc/os-release`, read as
   text, `debian.verify_os_release`. Unrecognized or mismatched releases raise the existing
   `StateError`s with their hints (`debian.py:146`, `:171-176`), unaggregated: with no Debian
   release the remaining rows mean nothing. `observe_release` is also what `vm confirm-release` and
   reinit use, so there is one observation function.
2. One unelevated `Script(shell=SH)` request assembled from the script-probed base rows, with
   `AGW_CONTRACT_AGENTS` in its environment (names are data, never interpolated into the source).
   Each fragment calls a prelude function `emit ID ok|fail|warn DETAIL`, so the script prints
   `AGW-CHECK ID STATUS DETAIL` lines. It never exits non-zero: each fragment is independent.
3. The elevated `sudo` request.

Everything after step 1 is read from the one script's stdout and the one elevated result.
`check_initialized(access)` is the same shape for the initialized rows: one script, no release or
sudo request.

**The parser is a boundary** (the output comes from the guest; D3): it accepts only lines starting
`AGW-CHECK` whose id is in the table for the phase, whose status is one of the three words, and
whose detail matches `[A-Za-z0-9_ ./:~$-]{0,80}`; anything else is dropped, never shown. A row with
no accepted line is reported as not checked, and takes its own severity: a `FAIL` row that did not
report fails closed, a `WARN` row warns "could not be checked", which keeps the existing behavior
for a pam_env probe that did not run to its end (`pam_env.py:64-68`). A non-zero script status
therefore fails every `FAIL` row it left unreported rather than raising.

`BaseReport` holds `release` and `findings`. `Finding` holds `check`, `severity`, `detail` and
`remedy`. `enforce(report, vm_name)` prints each `WARN` with `output.warn`, then raises `StateError`
listing every `FAIL` with its clause and detail, one hint line per distinct remedy (a `RECREATE_VM`
remedy first, since it supersedes reinit). It is the only function that prints.

The initialized phase runs at the end of `run_initialization` (`driver.py:315`), after
`_reconcile_authorized_keys` and inside the same `try`, so a failure takes the existing arm: the
init status goes `FAILED`, an `init_failed` event is recorded (sanitized by the logger), and the
error propagates to `_raise_init_failure` (`lifecycle.py:90`), which names `vm reinit`. It builds
its access with `execution.vm_access(vm, config, Admin(), log=logger.operation_log)`, called through
the package attribute so the slice 2 test fake reaches it. The base rows are not re-run there.

**`pam-env`.** `PAM_ENV_PROBE` and `parse_pam_env_probe` (`pam_env.py:26-52`) move into the row. Its
fragment emits one `warn` line naming the files, and the home-file condition (counted only where
`/etc/pam.d/sshd` loads `pam_env.so` with `user_readenv=1`) is unchanged. `/etc/default/locale`
stays out and agent homes are not read, as D10 states. The warning text is the existing sentence;
`vms/manager/pam_env.py` and its import in `lifecycle.py:47` are deleted, and
`tests/vms/test_pam_env.py` moves with the probe. It is never a failure at create or reinit.

## Release probing off the legacy stack

`vms/manager/release.py` splits observation from reconciliation:

- `observe_release` moves to `contract.py` (above).
- `verified_vm_release(db, vm, target)` (`release.py:19`) becomes
  `reconcile_release(db, vm, observed)`: the same drift refusal and persistence, with no transport.
  Reinit calls it with the release `check_base` observed.
- `confirm_vm_release` (`release.py:39`) builds `execution.vm_access(vm, config, Admin())` inside
  `gated_vm_boundary` and calls `observe_release(access, expected=None)`, so it runs only the
  release request, not the contract.
- `release.py` joins the static legacy-import check, and `debian.probe_debian_release`
  (`debian.py:149`) is deleted with its `ExecTransport` type import. `verify_os_release` and the
  rest of `debian.py` stay.

A VM without python3 can no longer be reinitialized or confirmed: both reach the VM only through
agw-launch and raise `VMNotReady`. HLA D10 anticipates this and assigns the way back to
`agw vm recover` (slice 4); the `VMNotReady` hint (`errors.py:77`, naming `agw vm reinit`) is
repointed there, and PR B must not split slices 3 and 4. See Open questions.

## The operation log

### `oplog.py`

`LOG_DIR = Path.home() / ".config" / "agentworks" / "logs"` (today `ssh.py:89`) and the file layout
(`<vm>-<UTC timestamp>-<stem>.log`, the header and footer, one record per write, `ssh.py:99-290`)
move into `execution/oplog.py`, which imports no legacy module. The class and functions:

```text
OperationLog(vm_name, stem, *, redactions=())   # opens the file, writes the header
    .path, .display_path
    .write(text)                     # the one sink: redact, then append
    .sanitize(text)                  # redact without writing
    .step(name), .output(text), .warning(msg), .log_error(msg), .close()
    .record(request, result)         # one request and its outcome
logs_for(vm_name, stem=None) -> list[Path]   # newest first
latest_log(vm_name, stem=None) -> Path | None
```

**Redaction at the sink.** `write` is the only way bytes reach the file and `sanitize` runs first on
every one, as `SSHLogger._write` does (`ssh.py:250`), with its two properties kept: the set is fixed
at construction and covers the raw and `shlex.quote`d form of each secret, longest first
(`ssh.py:127-137`); and the sanitized record and the transient file handler stay together in one
function (the CodeQL clear-text-storage note at `ssh.py:259-262`). Commit `a38e49489` ("redact SSH
logs before first write") made the "registered before the first write" rule; construction-time
registration is the same rule here, and per-request registration is deliberately absent. HLA D13
says retained streams have "the request's secret values" removed; with no secret flag on a request
yet, the set the creating command registers (the Tailscale key, git credentials, resolved setup
values, exactly as `lifecycle.py:465-473` and `:949-953` do) is that set. Per-request marking has no
consumer in this slice (see Amendments).

**What `record` writes, and never writes.** One block per request, after it completes:

```text
[12:00:01] $ command (elevated)  ->  completed, exit 0
<stdout>
STDERR: <stderr>
<ssh diagnostics tail>
```

The first line is the request's shape: for a `Command`, its argv; for a `Script`,
`script (SH|BASH| USER_DEFAULT, login)` with no source; then `elevated`, the `cwd`, the environment
_names_, and the deadline. Then the completion in the closed form (`completed`,
`not delivered (reason)`, `unknown (reason)`). The environment's values, the script source and stdin
are never passed to the sink: there is no code path that formats them. Argv and both streams pass
through the sink's redaction. The streams are retained as `Result` holds them, bytes decoded with
replacement. A sensitive stdin keeping the streams out of the log entirely arrives with
`Bytes(data, sensitive=)` in slice 5, its first consumer; this slice has no stdin to log.
`NotDelivered` and `Unknown` carry no stream text (execution LLD), so the block shows ssh's
diagnostics tail instead.

The cost of "never script source" is that an initialization log no longer shows a script's text
(`SSHLogger.log_command` prints the whole command today). The step headings and the repository's own
scripts identify what ran. R4's ruling would allow logging non-sensitive source; the HLA's stricter
rule is followed here. See Open questions.

### Wiring

`VMAccess` gains `log: OperationLog | None = None`, passed by `vm_access` and `enrollment_access`.
`run` calls `log.record(request, result)` after `_execute` and before it raises for a bad
completion, so failures are logged. `stream`, `attach` and `forward` take no log: their callers are
operator commands that have never logged, and a log for them is a decision for a consumer that needs
one. The log is a collaborator the creating command hands in; constructing it opens a file,
constructing `VMAccess` still does nothing (R9).

**One file per create or reinit.** Phase B and the initializers still write through `SSHLogger` for
five more slices, and the operator reads one log for the operation. So `SSHLogger` becomes a thin
legacy facade: it constructs an `OperationLog`, exposes it as `operation_log`, and its `_write`,
`sanitize` and `close` delegate to it; `step`, `output`, `warning`, `log_error` and the
legacy-shaped `log_command(command, SSHResult)` and `log_timeout` stay as formatting over
`OperationLog.write`. Legacy calls new, never the reverse. Phase A and the contract take
`logger.operation_log`; there is one sink, one redaction set and one path. Slice 8 deletes
`SSHLogger` and its legacy-shaped methods; `oplog.py` is already where it needs to be.

### Readers

The five readers of the old constant become two functions, which also removes five copies of the
exists-glob-sort sequence:

| Reader                                   | Today                                    | After                              |
| ---------------------------------------- | ---------------------------------------- | ---------------------------------- |
| `doctor_state.py:124,133-136`            | `LOG_DIR.glob("{vm}-*.log")`, newest     | `latest_log(vm_name)`              |
| `vms/manager/_helpers.py:205-210`        | the same                                 | `latest_log(vm_name)`              |
| `vms/manager/lifecycle.py:99-103`        | `"{vm}-*-vm-create.log"`, newest         | `latest_log(vm_name, "vm-create")` |
| `vms/manager/power.py:323-329` (delete)  | all of the VM's logs, unlinked           | `logs_for(vm_name)`                |
| `cli/commands/vm.py:441-455` (`vm logs`) | all, newest first, `--all` or the latest | `logs_for(name)`                   |

`logs_for` matches `<vm>-<timestamp>-<stem>.log` with the fixed timestamp shape (`\d{8}T\d{6}Z`),
not the `{vm}-*` glob. The glob matches another VM whose name begins with `<vm>-` (VM `dev` and VM
`dev-2`), so deleting `dev` removed `dev-2`'s logs and `vm logs dev` showed them. The helper is new
code on the reader path and the fix costs nothing there; it is called out as the small separable
cleanup (principle 9).

**Test isolation.** The session fixture (`tests/conftest.py:87-98`) and the nine sites that patch
`agentworks.ssh.LOG_DIR` (`test_ssh_logger.py:29,135,162,218`,
`test_operator_path_rendering.py:104`, `capabilities/test_native_setup_lifecycle.py:415`,
`vms/test_bootstrap_file_lifecycle.py:176,193,303`, `vms/test_lima_remote_create_interrupt.py:363`,
`vms/test_bootstrap_vm_hooks.py:65`, `vms/test_remaining_commands_orchestrated.py:674`) switch to
`agentworks.execution.oplog.LOG_DIR`. The readers and `OperationLog` read the module global at call
time, so one patch reaches both, and `agentworks.ssh.LOG_DIR` is deleted rather than re-exported
(principle 8: a second spelling is the half-migrated state). `tests/execution/test_independence.py`
covers the new modules automatically.

## Tests

None restates the implementation or our prose. The table-driven ones assert the table's invariants
once, so a new row is covered by being a row.

**Unit.**

- **Contract.** The parser over fabricated script output: each row's ok, fail and warn lines, a
  missing line (a `FAIL` row fails closed, a `WARN` row warns), a hostile detail and an unknown id
  dropped, a script that exited non-zero. `Finding`s from a `BaseReport`: a VM failing a base row is
  not ready (`enforce` raises), a warning-only report does not raise and warns, a `RECREATE_VM`
  finding supersedes a reinit remedy in the hint, and every row's `remedy` is one of the three (the
  plan's "each diagnosis names reinit or recreation", asserted on the data). The shell fragments run
  for real under a local `sh` where they depend only on the process (`login-shells` over `root` and
  a nonexistent user; `base-tools` and `init-tools` with a `PATH` of stubs; the pam_env cases moved
  from `test_pam_env.py` over temp files). `INIT_SYSTEM_PACKAGES` equals the keys of
  `INIT_SYSTEM_TOOLS`. `observe_release` accepts both recognized releases and refuses an
  unrecognized one and a mismatch with `expected`. A VM with pam_env settings passes the contract
  and warns naming the file.
- **Reachability** (recording fake): immediate success; success after failures is confirmed by a
  second probe and a failing confirmation continues polling; exhaustion returns `False` after
  `ATTEMPTS`; `HOST_KEY`, `AUTH_FAILED`, `PYTHON_MISSING`, a refusal and a non-zero `true` raise on
  the first attempt with no sleep; `LAUNCHER_FAILED`, `CONNECTION`, `TRUNCATED` and `DEADLINE` poll;
  a probe without `observation` raises `ValueError`; `TrustMissing` propagates unpolled.
- **Phase A** (fakes for the database, platform and access): the order of the sequence; an invalid
  address fails before any connection and secures the VM; each of a first-contact timeout, an
  enrollment that left no key, a release mismatch and a base failure marks `FAILED`, records
  `provisioning_failed`, calls `secure_failed_vm` and leaves the process raising; an interrupt
  secures the VM and does not mark `FAILED`; the post-ready wait returning `False` warns and
  proceeds; Phase B receives a VM-bound legacy target; the only `enrollment_access` call is the
  first contact. Reinit: no enrollment and no reachability wait, release drift refuses, a base
  failure stops before `run_initialization`, the agent users reach the script's environment.
- **Provisioning.** `settle_tailnet_address`: a first-read success, a success on the last read, a
  value that does not parse treated as a failed read, exhaustion calling `logout` exactly once and
  raising, a failed logout naming the node, and the join command having run exactly once in every
  case (a fake transport counting it). Each of `EphemeralTailscaleBootstrap`, Lima and the shell
  snippet end to end with a stub `tailscale` (fails twice then succeeds; always fails and records a
  `logout`; a logout that fails prints the node-naming warning). Each platform's `create` returns a
  parseable `tailnet_address` and none returns a transport.
- **Requests.** `PermissionDenied` for an agent-bound access with an elevated request, before
  `resolve_ssh` and with nothing spawned; `stage0_command` refuses `elevated` with a handover
  socket; `classify` and `outcome` for `AUTH_FAILED` (the row after `HOST_KEY`, and a 255 whose log
  holds neither token stays `CONNECTION`); the passphrase hint accompanies `AUTH_FAILED`.
- **Operation log.** The sink redacts every registered secret in argv, streams and diagnostics, in
  raw and quoted form, before the first byte reaches the file; environment values, script source and
  stdin appear nowhere in a record; `logs_for("dev")` does not return `dev-2`'s logs; `SSHLogger`
  and `OperationLog` write one file with one redaction set; a record for a failed request is written
  before the error is raised.
- **Existing.** `tests/conftest.py`'s `verified_debian_release` fixture, which patches
  `lifecycle.probe_debian_release` (`:78-84`), and its users
  (`tests/vms/test_create_vm_dispatch.py:111`, `:393`) move to faking `bootstrap_vm`'s result or
  `contract.observe_release`; `tests/test_vm_release.py` and `tests/test_debian.py` follow
  `release.py` and the deleted `probe_debian_release`; the `ProvisionResult` construction sites
  (`tests/transports/test_abc.py`, `tests/vms/test_create_vm_dispatch.py`,
  `tests/vms/test_create_reinit_orchestrated.py`) and the six platforms' create tests use
  `tailnet_address`.

**Conformance vectors added** to the execution LLD's list. The L lane runs them under a real
`sudo -n` on CI runners that grant it, skips locally without it and fails on CI without it (that
lane's rule); the S lane needs a real `sudo` and a passwordless user too, so elevation vectors are L
and V. `E5` uses a `sudo` stub on `PATH`.

| Vector                                                                                                                                                     | Lanes |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| E1 An elevated `Command(("id", "-u"))` completes with `0`, and `HOME`, `USER` are root's                                                                   | L V   |
| E2 Exact stdin positioning through `sudo`: the elevated payload reads the caller's first byte                                                              | L V   |
| E3 The elevated payload's environment is the rebuilt one: no `SUDO_*`, nothing from the account's startup files, the request's `env` applied               | L V   |
| E4 The stream check passes and the request completes under the distribution's default sudoers (`use_pty` as shipped), on a fresh VM; `sudo -V` is recorded | V     |
| E5 `sudo -n` that cannot run (the stub exits 1) is `NotDelivered(LAUNCHER_FAILED)` with the payload not run                                                | L     |
| E6 An elevated request over a background child that holds both streams completes at once with the right status                                             | L V   |
| A1 A wrong identity is `Unknown(AUTH_FAILED)` from the client log, on 8.5, 9.x and Win32-OpenSSH                                                           | S V   |

**Live evidence** (R11; cells per the plan: create and reinit on every platform from at least one
workstation each, Debian 12 and 13 guests, an OpenSSH 8.5 client on one workstation or the
operator's disposition):

- Create on every platform shows python3 installed at provisioning (the first `true` completes), the
  required `tailnet_address`, the first connection enrolling and the release attested first; a
  Windows workstation among the clouds. The elevated Phase A request on a fresh, never-initialized
  Debian 12 and Debian 13 VM, with E1 to E4 run against that VM before its reinit (a create stopped
  at Phase B's start provides it).
- A forced failure of the address read on one cloud, one Lima and one WSL2 or Proxmox: the node is
  logged out (or named when the logout also fails), create fails with the backend rolled back, and
  the auth key was sent once (the join command count).
- Lima on Apple virtualization: the address read before and after the sentinel restart is the same.
- **Penalties, on a Debian 13 guest:** (1) `reachability.wait` with a key the VM does not authorize
  raises after exactly one connection (the sshd journal shows one `authfail`), and the correct key
  connects at once afterwards; (2) as a control, a tight loop of failing connections reproduces the
  lockout and shows its threshold and duration, which settles the decay question above; (3) a
  stopped-then-started VM's wait completes without tripping it.
- Reinit on every platform, including a VM with pam_env settings (the warning, not a failure), a VM
  whose agent has an unsupported shell (blocked with the agent remedy), and a pre-python3 VM (typed
  `VMNotReady`).
- `vm confirm-release` from each workstation family, and the operator's `known_hosts` still
  byte-identical after a create.

## Incident audit

The entry points this slice moves are `create_vm`, `reinit_vm`, `bootstrap_vm`, release probing,
`tailscale_join.py`, `SSHLogger` and `pam_env.py`. The audit read their fix history (for example
`a38e49489` redact SSH logs before first write, `93aca2cfb` verify created release in core,
`eefaa2c5e` clean up provisioning artifacts, `3bff603ce` log cleanup boundaries, `7307e2f60` make
platform bootstrap complete or raise, `2841cb9fa` guarantee Python for native guest helpers), ADR
0025 and the bootstrap-fallback-removal and debian-release-transition SDDs. It adds these rows to
the plan's inventory (I22 onward; I21 is the last row today):

| Row | Behavior                                                                                                                                                                                                     | Re-proved in |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------ |
| I22 | An operator interrupt during Phase A still closes the kept VM's provisioning access before it propagates (`driver.py:193-211`)                                                                               | 3            |
| I23 | A first connection that left no key makes create fail where the cause is known (`driver.py:438-445`)                                                                                                         | 3            |
| I24 | The auth key is joined exactly once: a failed address read never causes a second join, and now logs the node out (`tailscale_join.py:65-71`, `lifecycle.py` bootstrap contract, `7307e2f60`)                 | 3            |
| I25 | The platform's availability hold spans Phase A and Phase B (WSL2 idle shutdown) (`lifecycle.py:643`)                                                                                                         | 3            |
| I26 | A release the guest reports is attested by core, never taken from the platform; an unattested or mismatched VM is retained failed with delete guidance, never recorded (`93aca2cfb`, `lifecycle.py:598-624`) | 3            |
| I27 | Ordinary operations refuse live release drift; adoption is the explicit `confirm-release` (`release.py:30-36`)                                                                                               | 3            |
| I28 | A log's redaction set is complete before its first write, and no secret is retained in raw or shell-quoted form (`a38e49489`, `ssh.py:127-137`)                                                              | 3            |
| I29 | A wait does not turn an authentication failure into a source lockout (Debian 13 sshd `PerSourcePenalties`)                                                                                                   | 3            |

Each is re-proved by a test above and, for I22, I25 and I29, a live cell: I25 is the WSL2 create
with the first contact now over SSH while the hold is the only thing keeping the distro up.
Re-proving I24 here does not re-prove I10 (the key out of command lines, logs and exceptions), which
stays slice 4's with the port off `ExecTransport`; this slice leaves the key-bearing code as it was.

## Collateral

- **ADR 0025.** An "Amended" paragraph in the status block, as ADR 0014 does for slice 2: core
  observes the release over SSH at the VM's tailnet address as the first check of the VM contract
  and persists only its own observation; it no longer probes a transport the platform returns (the
  Decision sentence at `0025-...md:28-29`). The decision itself, the registry, the recording and the
  absence of an operator bypass stand.
- **Platform author documentation** (`cli/agentworks/capabilities/vm_platform/README.md`): the
  `ProvisionResult` description (`:306-317`), the Phase A bullet (`:575-580`) and the platform
  authoring step (`:724-731`) describe `tailnet_address`, the retry and the logout on exhaustion;
  `base.py`'s `create` docstring (`:262-270`) and `secure_failed_vm` docstring (`:377`) match. The
  pointer that the attestation transport "is not a security sandbox" goes with the transport.
- **Command reference** (`cli/command-reference.md`): `vm reinit` (`:688-696`) states the contract's
  diagnoses, the warning (not failure) for pam_env, that a VM without python3 needs `agw vm recover`
  once slice 4 lands, and that `vm confirm-release` (`:705-712`) reads the release over SSH and so
  needs python3 as well. `vm create`'s failure behavior is added.
- **`docs/guides/upgrading-to-0.20.md`** (`:110`, `:138-142`): reinit and `confirm-release` of a VM
  that lacks python3; the `AUTH_FAILED` message; script text no longer appears in operation logs.
- **`cli/agentworks/execution/README.md`:** `enrollment_access`, `reachability`, `oplog`, the
  `elevated` and `observation` options.
- **Release notes:** python3 at provisioning; the contract at create and reinit; a reinit and a
  `confirm-release` that cannot reach a pre-python3 VM; the log layout (script source not logged).
- **The commit** carries `BREAKING CHANGE:` footers (`CONTRIBUTING.md:69`) for the changed
  `ProvisionResult` contract and for reinit's new refusals.
- **Completions and sample config:** no new command or option and no setting changes, so none.

## Open questions

Each is the lead's or the operator's, with a recommendation.

1. **Bump the vm-platform `contract_version`?** `ProvisionResult` changes shape in this slice and
   slice 4 changes the ABC again. Registration requires an exact match, so a bump refuses an
   out-of-tree platform written for the old shape at load instead of failing after the VM exists
   (`capabilities/README.md:566-570`). _Recommend_ bumping once, in this slice, because it is the
   first incompatible change, with slice 4 landing under the same version in the same PR. That edits
   the kind descriptor and the six bundled `contract_version` values.
2. **Drop D10's "Tailscale up at the recorded address" as its own row?** The connection proves it
   (above). _Recommend_ dropping it and amending D10, since the row would be a second `sudo` to
   restate the connection. The alternative is an elevated `tailscale ip -4` compared to the row.
3. **Interrupt during Phase A.** _Recommend_ leaving the row `COMPLETE` and the release unrecorded
   (reinit re-attests), as above, over marking `FAILED` and requiring delete. The alternative is one
   line in the interrupt arm and a delete-only outcome for a VM that merely was not verified yet.
4. **`SSHLogger` as a facade over `OperationLog`** (one file, one sink) versus leaving `SSHLogger`
   alone and giving Phase A its own `OperationLog` file or a second writer to the same file.
   _Recommend_ the facade: no duplicated redaction, one path, and `ssh.LOG_DIR` can be deleted
   instead of kept as an alias. It touches `ssh.py` and risks the logger's own tests, which pass
   unchanged only if the delegation is faithful.
5. **Script source in logs.** The HLA and this slice never log it, and initialization logs lose the
   text of scripts. R4 (operator, 2026-11-01) says only _sensitive_ script content stays out.
   _Recommend_ keeping the HLA's rule for now: no per-script sensitivity flag exists, and adding one
   is machinery with no consumer. If the debugging cost bites in slice 6, a `Script` field that opts
   a script into being logged is cheap to add then.
6. **Address-read budget:** six reads two seconds apart (10 seconds). _Recommend_ keeping it small:
   the join returns only once the node is running, and a longer window mostly delays a failure that
   needs the operator. The live cells should say whether Lima after its restart needs more.
7. **Lima: read before the sentinel restart.** _Recommend_ yes (above), subject to the live cell
   showing the address is unchanged by the restart. If it is not, keep today's order and rely on the
   retry.
8. **Address validation** as parse-only. _Recommend_ it; a Tailscale-range check is rejected above.
   The lead may want the stricter check as a guard against a platform returning its LAN address
   (Proxmox reads one, `platform.py:392`), which the host-key store would not catch since the VM is
   the same VM.
9. **`reachability.wait` returns `bool`.** _Recommend_ it (slice 4's `vm start` needs the `False`
   branch to name `agw vm recover`) while create raises on `False`. The alternative is a raising
   wait with a flag for the proceed-anyway caller, which the one such caller does not justify.
10. **Reinit blocked by an agent's unsupported shell.** _Recommend_ blocking (the fix is one command
    and the agent is unusable anyway). The alternative is a warning that lets reinit proceed.
11. **The circular `VMNotReady` hint between slices 3 and 4.** Until slice 4 repoints it, the hint
    says `agw vm reinit`, which slice 3 makes unable to run on that VM. _Recommend_ making the
    delivery rule explicit in the plan (slices 3 and 4 do not merge separately) and repointing the
    hint in slice 4, as the plan already lists; no stopgap wording.

## Amendments

The other SDD files are not edited by this slice's design. They need these changes before
implementation, for the owners to apply:

- **HLA D10.** Remove "Tailscale up at the recorded address" from the base list (the connection
  proves it); state that `login-shells` covers the admin and every agent; state that the initialized
  rows derive from `INIT_SYSTEM_TOOLS`; give the three remedies (`REINIT_VM`, `REINIT_AGENT`,
  `RECREATE_VM`). The ordering in D10 ("the release check is the first thing core does") holds: the
  release is the first contract request after the first connection.
- **HLA D4 and plan slice 3.** "Agent elevation and elevated attach refused before dispatch": the
  agent refusal is a runtime `PermissionDenied`; the elevated attach is excluded by the type
  (`elevated` is on `BoundedRequestOptions`, not `LoginShell`) and by the `stage0_command`
  invariant, with no runtime refusal and no test of the type.
- **HLA D5 (Retries).** Say that `VMAccess` never retries and that the one retry loop in this slice
  is `reachability.wait`, which requires `observation=True`.
- **HLA D8.** `reachability.wait` returns `bool`, polls only `LAUNCHER_FAILED`, `CONNECTION`,
  `TRUNCATED` and `DEADLINE`, raises everything else immediately, and owns neither the exhaustion
  warning nor the proceed-anyway decision. Add the `AUTH_FAILED` rule and the 5 second pause as the
  answer to `PerSourcePenalties`.
- **HLA D11 (`tailnet_address` row).** The validation happens in the platform's rollback window by
  the shared parser and again in `bootstrap_vm`, not in the value object; WSL2 and Proxmox settle
  and log out in the key-bearing script, the other platforms through `settle_tailnet_address`.
- **HLA D13.** "The request's secret values" are the set registered at the log's construction (as
  `SSHLogger` does); per-request secret marking has no consumer yet. `SSHLogger` delegates to
  `OperationLog`. The sensitive-stdin rule is implemented with `Bytes(sensitive=)` in slice 5.
- **Plan slice 3.** Add: `UnknownReason.AUTH_FAILED` and its classifier row; `enrollment_access`;
  the `logs_for` and `latest_log` readers; `SSHLogger` delegation; the `release.py` split and
  deleting `probe_debian_release`; `parse_tailnet_address` and `settle_tailnet_address`; the shell
  address loop; narrowing the provisioning trust file's lifetime; the `contract_version` decision;
  incident rows I22 to I29; the test and vector lists above; the penalties live cell. Note under
  slice 4 that the legacy `TailscaleWait` and `wait_for_reconnect` go with their last callers. Add
  to slice 5's scope the operation log's handling of a sensitive stdin.
- **execution-lld.md.** The classifier table gains `AUTH_FAILED` (a 255 with no start marker whose
  client log holds an authentication failure and no host-key failure); the error table's
  `ExecutionIncomplete` and the passphrase hint gain the new reason; `PermissionDenied` lands; "Left
  to the first consumer" is now resolved for `elevated`, `observation`, Enroll, `reachability` and
  the operation log; the use_pty note is answered above.
- **trust-lld.md.** "Clouds before enrollment" and the test that the provisioning file "survives
  into core's post-create use" are obsolete: the file is scoped to `create`. Phase A's enrollment
  step list is replaced by the sequence above, and "Enroll mode is reachable only from create" is
  now carried by `enrollment_access` being a distinct factory.
- **FRD.** None.
