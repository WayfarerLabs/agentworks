# Transport Take 2: Contract Low-Level Design (Slice 3)

- Status: Draft, revised after the private project and complexity reviews of `5a01d27f6`.
- Date: 2026-11-01
- Architecture: [hla.md](./hla.md) D4 (elevation), D8 (reachability), D10 (the contract), D11 (first
  half), D13 (the operation log); requirements: R1, R4, R7, R10, R11
- Plan: [plan.md](./plan.md), slice 3, delivered in PR B with slice 4
- Source baseline: `f3499ea81` (the PR A head, with slices 1 and 2 implemented). Every `path:line`
  below is at that commit unless it says otherwise.

## Scope

This LLD settles the details the HLA leaves open for first contact with a new VM and the contract
every VM must satisfy:

- the provisioning additions: python3, and the required, validated `tailnet_address`;
- Phase A on `VMAccess`: the first connection in Enroll mode, release attestation inside the
  fail-closed span, and `vm confirm-release` and reinit's release probe off the legacy stack;
- elevation for the contract's sudo check, and `reachability`;
- the contract's check table and diagnoses, including the pam_env warning moved from reinit;
- the operation log record for the new requests;
- the `contract_version` bump, tests, the incident audit, collateral and live evidence.

The operator's direction is not to take on extra complexity (FRD, 2026-11-01). Each mechanism below
is the smallest one that meets a requirement, and what the reviews cut is not listed back.
Amendments the other SDD files need are at the end, as [execution-lld.md](./execution-lld.md) lists
its own.

**Out of this slice.** Phase B and the initializers stay on the legacy stack until slice 6 (they
receive a legacy `Transport`, `driver.py:248`). `native_transport` the platform method, rejoin,
rekey, logout, `vm start`'s wait and the activation gate stay until slice 4, so the legacy
`wait_for_reconnect` and `TailscaleWait` (`transports/__init__.py:248`, `:279`) keep their callers
(`vms/manager/tailscale.py:127`, `:149`, `vms/manager/power.py:550`) and are deleted there. The
auth-key transport of the first join (the stdin command in `tailscale_join.py`, WSL2's staged
script, Proxmox's guest-agent script) is not touched except where the address read and a logout are
added after the join succeeded. `LOG_DIR`, its readers and `SSHLogger` stay until slice 8.

**Slices 3 and 4 ship together.** Between them a VM without python3 cannot be reinitialized and the
`VMNotReady` hint (`errors.py:77`, naming `agw vm reinit`) is circular. PR B does not merge without
slice 4, which repoints the hint at `agw vm recover`; there is no stopgap wording in between.

## Module layout

| Module                                                                                         | Contents                                                                                                                            |
| ---------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `agentworks/vms/contract.py` (new)                                                             | The check table, `observe_release`, `check_base`, `BaseReport`, `Finding`, `enforce`                                                |
| `agentworks/vms/initializer/phase_a.py` (new)                                                  | Phase A: the fail-closed span and the first-contact sequence, with no legacy import                                                 |
| `agentworks/execution/reachability.py` (new)                                                   | `Wording`, `wait`                                                                                                                   |
| `agentworks/execution/oplog.py` (new)                                                          | `format_record(request, result, *, label)`, one function                                                                            |
| `agentworks/execution/access.py`, `execution/__init__.py`                                      | `enrollment_access`; `VMAccess` gains `identity`, `trust_mode` and `log`; `vm_access` gains `log` (wrapper `:24`)                   |
| `agentworks/execution/requests.py`, `ssh_process.py`, `envelope.py`, `results.py`, `errors.py` | `elevated` on `BoundedRequestOptions`; `stage0_command(elevated=)`; the `AUTH_FAILED` classification; `PermissionDenied`            |
| `agentworks/capabilities/vm_platform/base.py`, `tailscale_join.py`, `bootstrap_script.py`      | `ProvisionResult.tailnet_address`, `parse_tailnet_address`, `settle_tailnet_address`, the bootstrap parser without an address       |
| `agentworks/capabilities/vm_platform/cloud_init.py`                                            | python3 in `PROVISIONING_PACKAGES`                                                                                                  |
| The six platforms' `create`                                                                    | Return `tailnet_address`, settled by the shared helper                                                                              |
| `agentworks/vms/initializer/driver.py`, `vms/manager/lifecycle.py`, `vms/manager/release.py`   | `bootstrap_vm` becomes a thin caller of `phase_a`; create and reinit run the contract; release observation and reconciliation split |
| `agentworks/ssh.py`                                                                            | `SSHLogger.record(request, result)`, a step label, nothing else                                                                     |
| `agentworks/debian.py`                                                                         | `probe_debian_release` deleted (no caller remains); `verify_os_release` stays                                                       |
| `agentworks/vms/manager/pam_env.py`                                                            | Deleted; its probe becomes the contract's pam-env row                                                                               |
| The vm-platform kind, six platforms and their docs                                             | `contract_version` 1 to 2                                                                                                           |

**Independence.** `contract.py`, `reachability.py` and `oplog.py` import nothing from the six legacy
modules (R10). The fresh-interpreter guard walks only `agentworks.execution`
(`tests/execution/test_independence.py`), so it gains an explicit import of
`agentworks.vms.contract`; `agentworks/vms/__init__.py` is empty, so that import pulls in nothing
else. The static legacy-import check works per file
(`tests/test_execution_import_boundary.py:24-29`), and these files join `MIGRATED_FILES`:
`vms/contract.py`, `vms/initializer/phase_a.py` and `vms/manager/release.py`. Not joining:
`vms/initializer/driver.py` (it builds Phase B's legacy `SSHTransport`) and
`vms/manager/lifecycle.py` (it constructs `SSHLogger`). `phase_a.py` therefore types its log as a
small `Protocol` rather than importing `SSHLogger`, even under `TYPE_CHECKING`, which the check also
rejects.

## Provisioning

### python3

`PROVISIONING_PACKAGES` (`capabilities/vm_platform/cloud_init.py:16`) gains `python3`. Every
platform's bootstrap installs the list in one `apt-get install` (`bootstrap_script.py:117`), and the
six consumers already pass it (`lima.py:391`, `wsl2_bootstrap.py:65`, Proxmox `platform.py:404`,
Azure `:420`, AWS `:424`, GCP `:307`). Debian's `python3` package depends on the full standard
library, so the `python3-minimal` case of D4 cannot arise from a provisioned VM. Init keeps
installing it (`INIT_SYSTEM_PACKAGES`, `:27`), as the operator ruled on 2026-11-01. The list is
unchanged otherwise: an `apt-get install` that fails already fails Phase B
(`vms/initializer/packages.py:179-183`), so no check restates it.

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
return it (Lima's `self._transport_for` at `lima.py:456`, the `assert prov_transport is not None` at
`aws/platform.py:599`). Each platform keeps the provisioning transport it already holds as a local.

**Validation.** `parse_tailnet_address(text) -> str` in `base.py` strips, parses with
`ipaddress.ip_address` and returns the canonical text, or raises `ValueError`. It is called at two
boundaries:

1. By the shared settle helper (below), on each read, inside the platform's rollback window, so a
   bad read fails create cleanly.
2. By `phase_a` before it writes the address to the row, because `ProvisionResult` is the platform
   plugin boundary (D3: arguments from a caller our type checker does not check). It is not in
   `ProvisionResult.__post_init__`: every platform builds the value as its last statement
   (`lima.py:465`), after its rollback window closed, so a raise there would leave a backend VM with
   only its row unwound. In `phase_a` a bad value is an ordinary Phase A failure: the VM is retained
   as failed with delete guidance, after `secure_failed_vm`.

The check is "parses as an IP address". Nothing checks that it is a tailnet address, and a range
check would reject a valid self-hosted tailnet with another prefix. The connection that follows is
verified against the VM-bound host key whatever the address, and Agentworks is a controlled
environment, so this is accepted.

**One mechanism for all six platforms.** After the join succeeds, every platform reads the address
over the transport it already holds, retrying a transient failure, and when the reads are exhausted
logs the node out over that same transport and fails create. Reading the address sends no key, so
nothing here sends the key twice. `tailscale_join.py` gets:

```text
settle_tailnet_address(read: Callable[[], str], logout: Callable[[], None], *, node: str) -> str
settle_on_transport(target: ExecTransport, *, node: str) -> str
```

`settle_tailnet_address` calls `read` up to `ADDRESS_READS = 6` times, `ADDRESS_PAUSE_SECONDS = 2`
apart, accepting the first value `parse_tailnet_address` accepts and treating `SSHError` and
`ValueError` as a failed read. `tailscale up --auth-key` returns when the node is running, so a
failed read right after it is rare and short. When the reads are exhausted it calls `logout` once,
then raises `ProvisioningError`: if the logout succeeded, the message says so and that create can be
run again; if it failed or raised, the message names `node` (the VM's hostname, `request.hostname`)
for removal in the Tailscale admin console. `settle_on_transport` is the five-line wrapper for the
platforms that hold an `ExecTransport`, so the two command strings exist once: `tailscale ip -4`
(`sudo=True`, `check=True`, `timeout=15`, the command at `tailscale_join.py:106`) and
`tailscale logout` (`sudo=True`, `check=True`, `timeout=30`).

| Platform        | Join                                                   | Settled with                                                                                 |
| --------------- | ------------------------------------------------------ | -------------------------------------------------------------------------------------------- |
| Lima, Remote    | `limactl shell` stdin command (`lima.py:425`)          | `settle_tailnet_address` with `_run_lima` callbacks (`lima.py:449`)                          |
| WSL2            | The key-bearing script (`bootstrap_script.py:238-241`) | `settle_on_transport(native_transport)`; the `WSL2Transport` exists at `wsl2.py:764`         |
| Proxmox         | The same script via the guest agent                    | `settle_on_transport(target)`, with `target` built before it (`platform.py:414`)             |
| AWS, Azure, GCP | `join_tailscale_ephemerally` (`tailscale_join.py:22`)  | `EphemeralTailscaleBootstrap.complete` (`:65`) calls `settle_on_transport` and returns `str` |

The exception is raised from inside the platform's existing rollback `try`, so the existing arms
(`lima.py:457-463` and the matching arms of the other five) delete the partial backend as for any
other create failure; `RetainedProvisioningError` is still raised only by a rollback that cannot be
confirmed (`base.py:265-273`). Neither the read nor the logout carries the key, so no message,
`repr` or chained exception holds it. A `KeyboardInterrupt` during the window takes the platform's
interrupt arm as today and leaves the node, as it does for an interrupt anywhere after the join;
that is not new behavior. The logout matters because core never enrolled the host key, so delete's
recovery (VM trust) could not log the node out later (HLA D11).

**What changes in the bootstrap script.** The key-bearing script only joins. Step 7
(`bootstrap_script.py:238-241`) drops its `TS_IP=$(tailscale ip -4)` and its `tailscale-ip=` marker
and prints `##SUCCESS## tailscale joined`. The lead's brief had the script read the address best
effort with `|| true`; nothing would consume that output, since Python reads the address, so the
read is dropped rather than kept as dead output. `parse_bootstrap_output` must stop requiring an
address: `BootstrapResult.ok` is `exit_code == 0 and self.tailscale_ip is not None`
(`bootstrap_script.py:319`), and the parser fills `tailscale_ip` from the marker (`:335-341`). The
`tailscale_ip` field, its parsing and the `ipaddress` import go; `ok` is `exit_code == 0`.
`run_wsl2_bootstrap` stops asserting and returning it (`wsl2_bootstrap.py:182-183`) and returns
`None`; Proxmox's `_run_bootstrap_via_agent` stops returning `parsed.tailscale_ip`
(`platform.py:633-634`) and the caller's "did not return a Tailscale IP" raise goes (`:410-412`).
The "Phase A will retry IP discovery without the auth key" warnings (`tailscale_join.py:111`,
`lima.py:454`) go with the fallback they promised.

**Lima keeps today's order.** The read follows the restart-sentinel `limactl restart`
(`lima.py:434-454`) as now, and relies on the retry.

**WSL2 and Proxmox transports with `sudo=True`.** `WSL2Transport` already runs `sudo=True` commands
after the bootstrap (`wsl2_bootstrap.py:79`, `:140`, which `transports/wsl2.py:69-70` wraps as
`sudo -n bash -c`); the one `setsid sudo -n ... </dev/null` workaround is specific to the long
bootstrap script (`:131`). Proxmox's guest agent runs commands as root, so `sudo=True` costs nothing
there. (`driver.py:399-401`'s fallback never ran on these two platforms, which return an address
from the script, so it is no evidence.) The WSL2 and Proxmox live cells cover it.

**Interaction with create.** Core no longer reads the address at all. The attestation block
(`lifecycle.py:599-626`) and the `tailscale ip -4` fallback (`driver.py:399-401`) are deleted.

### The provisioning trust file's lifetime

`ProvisionRequest.provisioning_trust` is entered on `init_stack` so the Unverified file outlives
`create` for core's post-create use of the returned transport (`lifecycle.py:515`;
[trust-lld.md](./trust-lld.md), "Clouds before enrollment"). With no returned transport that use is
gone, so the file is scoped to the platform's `create` call: entered in a `with` that closes when
`create` returns or raises. The binding is still passed through `ProvisionRequest`. This is a
consequence of deleting `native_transport`, not an extra change.

## Requests: elevation

`elevated: bool = False` goes on `BoundedRequestOptions` (`requests.py:61`), beside `deadline`,
which `LoginShell` already lacks. `elevated=True` runs the whole operation under `sudo -n`. It is
not in the envelope: it is the prefix of the stage 0 command (D4). `stage0_command`
(`ssh_process.py:64`) gains `elevated: bool = False`, producing
`sudo -n /usr/bin/python3 -I -S -c '<stage 0>'`. The root-owned launcher then rebuilds its
environment from the passwd entry of uid 0 (`agw_launch.py`, step 4 of the execution LLD), so
nothing has to survive `sudo`'s `env_reset`. `attach` takes `LoginShell`, which inherits
`RequestOptions` and not `BoundedRequestOptions`, so an elevated attach cannot be expressed, and
neither can one that combines `sudo -n` with a handover socket (the attach path never passes
`elevated`). The plan's "elevated attach refused" is satisfied by the types, and no check or test
restates them.

**Agent elevation.** R3: an agent-bound operation never gains administrative authority. `VMAccess`
takes the `Identity` it was built for (`Admin` or `Agent`, `access.py`), as a required keyword with
no default, so a directly constructed access chooses. `run` and `stream` check it first, before
`resolve_ssh()`: `elevated=True` for an `Agent` raises `PermissionDenied` (new in `errors.py`,
subclassing `AuthorizationError`, naming the VM and the account, never the request). The guest's
`sudo -n` remains the second layer. The recording fake (`tests/vm_access_support.py`) takes the
identity too.

**`use_pty`.** sudoers(5) says "If the sudo process is not attached to a terminal, use_pty has no
effect", and every elevated request is `ssh -T` with no controlling terminal (`ssh_process.py:172`).
Debian's default sudoers could not be read here, so vector E4 on a fresh VM decides.

**Failure shape.** `sudo -n` without passwordless sudo exits 1 with no start marker, which
classifies as `NotDelivered(LAUNCHER_FAILED)`. If `sudo` is missing, the account shell exits 127
with no start marker, which classifies as `NotDelivered(PYTHON_MISSING)` and would wrongly read as
"python3 is not installed". The contract therefore maps the elevated request's outcome itself
(below), and it always runs after the unelevated request that proved python3.

## Authentication failures

Ending the first-contact wait at the first authentication failure avoids a 240 second wait for a
failure that cannot clear and avoids repeated failed authentications against the VM's sshd (Debian
13's `PerSourcePenalties` penalizes them).

`Permission denied (publickey` in the client log (the constant `AUTH_FAILURE`, read the way
`Host key verification failed` is, `ssh_process.py:58`) classifies a 255 with no start marker as
`Unknown(AUTH_FAILED)`, a new `UnknownReason` beside `HOST_KEY`. `classify` (`envelope.py:277`)
gains `auth_failed`; its last line becomes: host-key failure, else authentication failure, else
`CONNECTION`. `ssh_process.outcome` (`ssh_process.py:305`) passes `AUTH_FAILURE in text`.

- `Result.require_completed` (`results.py:83`) falls through to `ExecutionIncomplete`. Its wording
  for this reason says the VM did not accept the configured SSH key and that nothing ran.
- `_passphrase_hint_on_connection_failure` (`access.py:196`) adds the passphrase hint for
  `Unknown(CONNECTION)` today. An encrypted key the agent does not hold fails as
  `Permission denied (publickey)`, so that is now `AUTH_FAILED`, and the condition becomes
  `CONNECTION` or `AUTH_FAILED`.
- Vector A1 (below) confirms the client log captures the token on 8.5, 9.x and Win32-OpenSSH.

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
def wait(access: VMAccess, *, wording: Wording) -> bool
```

The probe is fixed: `Command(("true",), deadline=10)`. Each attempt is one `access.run(probe)` and
the outcome is classified from a whitelist:

| Outcome of the attempt                                                                     | Action                                                             |
| ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------ |
| `Completed(0)`                                                                             | The VM answered                                                    |
| `Unknown` with `CONNECTION`, `TRUNCATED` or `DEADLINE`                                     | Not yet: keep polling                                              |
| Anything else (`HOST_KEY`, `AUTH_FAILED`, `NotDelivered` of any reason, a non-zero `true`) | Raise at once through `Result.require_completed` / `CommandFailed` |

A failure that polling cannot fix is raised rather than waited out: the legacy wait spent 240
seconds on a changed host key. `NotDelivered(LAUNCHER_FAILED)` raises too: every known cause (a
startup file that reads stdin or redirects a stream, a login shell that cannot run the constant
line) is persistent. Errors raised before dispatch (`TrustMissing`, `ClientUnsupported`) propagate
from `access.run` on the first attempt, as `wait_for_reconnect` already did for `TrustMissing`
(`transports/__init__.py:292`). The function returns `True` when the VM answered and held, and
`False` when the attempts ran out. Callers decide: create's first contact raises, its post-ready
wait warns and proceeds as today, and slice 4's `vm start` hints at `agw vm recover`. The `Wording`
members each have a slice 3 caller: first contact says `VERIFY`, the post-ready wait `RECONNECT`.

Tests patch `agentworks.execution.reachability.time.sleep` and use the recording fake `VMAccess`
(`tests/vm_access_support.py`) with a scripted sequence of results.

## First contact: Phase A on `VMAccess`

### Entry

`vm_access(vm, config, identity)` builds a VM-mode `VMAccess` (`access.py:312`; `_policy` is
hard-wired to `TrustMode.VM` at `:207`). The first connection to a new VM must be Enroll mode. Phase
A records the address on the row before it connects, so the row-based construction needs only a
different trust mode, not a different source for the host. `VMAccess.__init__` gains
`trust_mode: TrustMode = TrustMode.VM` (`_policy()` uses it), and the package gains a second,
deliberately differently named factory:

```text
execution.enrollment_access(vm, config, *, log=None) -> VMAccess   # admin, TrustMode.ENROLL
```

It is a separate function rather than a parameter of `vm_access`, so the one call that may accept a
new host key is easy to find by search and does not look like the ordinary factory (principle 2; HLA
D9 "the code that builds a connection chooses one explicitly"). Its docstring says create's first
contact is its only caller. `vm_access` copies `vm.tailscale_host` at construction
(`access.py:312`), so the row passed in must be re-read after the address is recorded: the `vm_row`
read at `driver.py:151` predates it and would give a host of `None`.

### The sequence

`phase_a.bootstrap` holds the fail-closed span (`driver.py:159-211`) and the steps below. The
existing `driver.bootstrap_vm` (`driver.py:94`) loses `exec_target` and `tailscale_ip`, gains
`tailnet_address: str` and `expected_release: DebianRelease`, and returns
`(Transport, home, DebianRelease)`; it calls `phase_a.bootstrap`, then does what stays legacy: the
SSH-config sync (non-fatal, as today) and Phase B's `SSHTransport(..., trust=VM, logger=logger)`
(`driver.py:409-421`, `:447`). The returned release replaces the `observed_release` local in
`create_vm` (`lifecycle.py:628`). The attestation, which is outside the span today
(`lifecycle.py:599-626`), is inside it:

1. **Record.** `parse_tailnet_address(tailnet_address)`; mark provisioning `IN_PROGRESS`; record the
   address (`db.update_vm_tailscale`); re-read the row.
2. **First contact.** `access = enrollment_access(vm, config, log=logger.record)`;
   `reachability.wait(access, wording=VERIFY)`. `False` raises `ProvisioningError` ("the VM did not
   become reachable over SSH at ADDRESS"). This replaces the five 15-second attempts
   (`driver.py:426-435`) with the common bounded wait; a cold tailnet path can need longer than 90
   seconds, and the cost of the longer bound is only a longer failure.
3. **Enrollment post-condition.** `trust.has_record` as today (`driver.py:438`).
4. **Contract.** `access = vm_access(vm, config, Admin(), log=logger.record)`;
   `report = contract.check_base(access, expected_release=expected_release, admin=admin_username, agents=())`;
   record the observed release (`db.update_vm_debian_release`); `contract.enforce(report, vm_name)`.
   The release is therefore the first contract request, directly after the first connection, as R1
   and D10 require; a release that fails raises before anything else is asked of the VM.
5. **Complete.** Mark provisioning `COMPLETE` and record the `provisioning_complete` event with the
   address, both inside the span as the event is today (`driver.py:172`). Then, outside the span and
   still conditional on `on_tailscale_ready` as today (`driver.py:213-227`; create always passes
   it): the hook, then `reachability.wait(access, wording=RECONNECT)`, whose `False` warns
   "proceeding anyway".

Any `Exception` in steps 1 to 5's span takes the existing arm: provisioning `FAILED`, the
`provisioning_failed` event, `platform.secure_failed_vm`, the log pointer, re-raise
(`driver.py:175-192`). The provisioning event's detail was the native transport's description
(`exec_target.describe()`, `driver.py:149`); it becomes `ssh ADDRESS`.

**Status and interrupts.** `COMPLETE` is now written after the contract passes, not before the first
connection (`driver.py:396-404`). An interrupt that lands before then finds the row `IN_PROGRESS`;
the `except BaseException` arm (`driver.py:193-211`) secures the VM as today and now also marks the
row `FAILED` unless `COMPLETE` has been written (it is entered only after step 1 sets `IN_PROGRESS`,
so a `PENDING` row cannot reach it). A VM that was never attested is retained as failed, as D9 and
incident row I26 require, and the ordinary-command gates block only a `FAILED` row
(`vms/manager/_helpers.py:229-236`), so an unattested VM stays out of them. `lifecycle.py` prints
the delete wording whenever provisioning is not `COMPLETE`, not only when it is `FAILED`, because
reinit refuses any such row (`lifecycle.py:813`) and the `PENDING` window includes the hold entered
before `bootstrap_vm` (`lifecycle.py:643`). That cancel handler (`lifecycle.py:657-663`) only warns
today; it now also marks a row that is not `COMPLETE` as `FAILED`, so the gates cover the `PENDING`
window too. The wording is the attestation interrupt arm's today (`lifecycle.py:611-617`: the VM
exists but was not verified; run `agw vm delete`), not `_warn_init_cancel`'s reinit advice. An
interrupt after step 5 leaves `COMPLETE` and takes `_warn_init_cancel`. A post-ready wait that
raises (`HOST_KEY`, `AUTH_FAILED`, anything else the table raises) is outside the span like the wait
itself and propagates out of `bootstrap_vm` to `_raise_init_failure`: the row stays `COMPLETE`, the
VM was attested seconds earlier, and the operator is told to `vm reinit`.

**Enroll covers more than one connection.** The first-contact wait can make up to 17 connections
(sixteen attempts and the confirming probe), all in Enroll mode. `accept-new` records a key only
when none is known, so after the first connection that reaches sshd later ones verify against it.
HLA D9 says Enroll is the first connection; see Amendments.

### Create and reinit

`create_vm` (`lifecycle.py`): the attestation block and the `probe_debian_release` import go; the
`_mgr.bootstrap_vm(...)` call passes `platform_obj`, `platform_ctx`, `logger`,
`tailnet_address=result.tailnet_address` and `expected_release=creation_release`; its result's
release feeds `run_initialization(debian_release=...)` (`:684`).

`reinit_vm` has no Phase A and no enrollment. After the logger exists (`:949`) and before
`run_initialization`, it replaces the legacy `verified_vm_release` and `warn_pam_env_settings` calls
(`:963-965`) with:

```text
access = execution.vm_access(vm, config, Admin(), log=logger.record)
report = contract.check_base(access, expected_release=None, admin=vm.admin_username,
                             agents=[Agent(a.name, a.linux_user) for a in db.list_agents(vm_name=name)])
release = _mgr.reconcile_release(db, vm, report.release)   # refuses live drift, records
_warn_newly_observed_legacy(vm, release)
contract.enforce(report, name)
```

(`db.list_agents` is `db/database.py:642`.) Release drift refuses before other findings are shown
(it names `vm confirm-release`, `release.py:30-36`), then the legacy release warning, then warnings
and failures. A reinit that finds a failure stops before Phase B.

## The VM contract

`vms/contract.py` holds one table. A `Check` is `(id, severity, probe)`.

| Row            | Severity | How it is checked                                                                                  |
| -------------- | -------- | -------------------------------------------------------------------------------------------------- |
| `release`      | FAIL     | Own request: `Command(("cat", "/etc/os-release"))`, judged by `debian.verify_os_release`           |
| `systemd`      | FAIL     | Script: `[ -d /run/systemd/system ]`, the test `sd_booted()` uses                                  |
| `sudo`         | FAIL     | Own request: `Command(("id", "-u"), elevated=True)`, stdout `0`                                    |
| `base-tools`   | FAIL     | Script: `command -v` of `install`, `flock`, `nohup`, `setsid` and `tar`                            |
| `login-shells` | WARN     | Script: for the admin and each agent, the passwd shell's basename is `sh`, `dash`, `bash` or `zsh` |
| `pam-env`      | WARN     | Script: the existing probe over `/etc/environment`, `pam_env.conf` and `~/.pam_environment`        |

Every FAIL row means the VM cannot be repaired by initialization, so a failure names recreation
(`agw vm delete NAME`, then create again) and the table carries no remedy field. The warnings carry
their own commands. The `base-tools` list is what the transport and slices 5 to 7 call: `install`
(`vms/hardening.py:182`), `tar` (`vms/backup.py:342`), `setsid` and `nohup` (the detached wrapper,
HLA D7) and `flock` (the file lock, HLA D6).

Not rows, and why:

- **python3 and agw-launch running.** The first request proves them: a missing python3 is
  `NotDelivered(PYTHON_MISSING)` and a python3 without its standard library is `STDLIB_MISSING`,
  both raised as `VMNotReady` by `require_completed` before any row is read.
- **The admin account.** The connection authenticated as it.
- **Tailscale up at the recorded address.** D10 lists it. The request reached the VM at that address
  over the tailnet and verified the VM-bound host key, so the connection is the proof, and a
  `tailscale ip -4` comparison would only restate it behind another `sudo`.
- **Initialized checks.** D10's end-of-Phase-B checks are dropped: a failed `apt-get install`
  already fails Phase B (`packages.py:179-183`), and a second list of expected commands would only
  restate `INIT_SYSTEM_PACKAGES`.

### Execution

`check_base(access, *, expected_release, admin, agents) -> BaseReport` runs, in order:

1. `observe_release(access, expected=expected_release)`: one request, `cat /etc/os-release`, read as
   text, `debian.verify_os_release`. Unrecognized or mismatched releases raise the existing
   `StateError`s with their hints (`debian.py:146`, `:171-176`), unaggregated: with no Debian
   release the remaining rows mean nothing. `observe_release` is also what `vm confirm-release` and
   reinit use, so there is one observation function.
2. One unelevated `Script(shell=SH)` request assembled from the script-probed rows, with
   `AGW_CONTRACT_ACCOUNTS` (the admin's and the agents' Linux user names, space separated) in its
   environment: data, never interpolated into the source.
3. The elevated `sudo` request, mapped explicitly: `Completed(0)` with stdout `0` passes; any other
   `Completed`, `NotDelivered(LAUNCHER_FAILED)` and `NotDelivered(PYTHON_MISSING)` (an elevated 127,
   above) are the `sudo` row's FAIL with the detail "sudo -n did not run as root"; every other
   outcome, `Unknown` included, raises through `require_completed`.

**The script protocol.** The script prints a line only for a problem, then one closing line:

```text
AGW-CHECK <id> <detail>
AGW-CHECK-END
```

Severity comes from the table, never from the guest. The prelude is one function,
`problem ID DETAIL`, and each fragment calls it; the script never exits non-zero. A row that names
several things in one line (`base-tools`, `pam-env`) separates them with a space, because the detail
allowlist has no comma. A row that names accounts (`login-shells`) prints one line per account, and
repeated ids aggregate into one row: Agent Linux users are `agt-<name>` and can be 32 characters
(`agents/manager/_common.py:37-42`), so three of them would overflow one 80-character detail and
degrade to "(detail not shown)". **The parser is a boundary** (the output comes from the guest; D3):
it accepts only `AGW-CHECK` lines whose id is a script-probed row. A known id with a detail outside
`[A-Za-z0-9_ ./:~$-]{0,80}` still reports its row, with the detail replaced by "(detail not shown)",
so a malformed line can never hide a problem. For `login-shells` each line's detail is one account,
and the parser keeps only accounts from the set it passed in; a name the guest invents is dropped,
and an account with no passwd entry is reported by name like any other problem. A missing
`AGW-CHECK-END` means the probe did not complete: the report is one FAIL for the probe instead of
one per row. It names the outcome and no stream text: the script's exit status (the request
completed, so it has one), and points at the operation log and `agw vm verify-connection NAME`. No
error holds a stream (D13).

`BaseReport` holds `release` and `findings`; `Finding` holds `check` and `detail`.
`enforce(report, vm_name)` prints each WARN with `output.warn` and raises `StateError` listing every
FAIL, with the recreation hint (the probe-incomplete FAIL carries its own pointer, the operation log
and `verify-connection`, instead, because recreation is not what it asks for). It is the only
function that prints. For each account the `login-shells` row names, `enforce` renders a separate
warning: `agw agent reinit NAME` for an agent, looked up from the `Agent(name, linux_user)` it was
given (`access.py`; `agw agent reinit` applies the template shell through the admin,
`agents/initializer.py:160`), and `agw vm reinit NAME` for the admin, whose shell the admin step
sets (`driver.py:675`). Both are warnings, so neither blocks a reinit that is the cure; a tcsh admin
whose stage 0 happens to parse is still alerted.

**`pam-env`.** `PAM_ENV_PROBE` and `parse_pam_env_probe` (`pam_env.py:26-52`) move into the row. The
fragment collects the file names and calls `problem pam-env` once with them. The home-file condition
(counted only where `/etc/pam.d/sshd` loads `pam_env.so` with `user_readenv=1`) is unchanged,
`/etc/default/locale` stays out and agent homes are not read, as D10 states. The warning text is the
existing sentence; `vms/manager/pam_env.py` and its import (`lifecycle.py:47`) are deleted and
`tests/vms/test_pam_env.py` moves with the probe. The old "could not be checked" warning for a probe
that did not run to its end (`pam_env.py:64-68`) becomes the missing-END FAIL, since one script now
carries every row.

## Release probing off the legacy stack

`vms/manager/release.py` splits observation from reconciliation:

- `observe_release` moves to `contract.py` (above).
- `verified_vm_release(db, vm, target)` (`release.py:19`) becomes
  `reconcile_release(db, vm, observed)`: the same drift refusal and persistence, with no transport.
  Reinit calls it with the release `check_base` observed, through the `_mgr.` package attribute as
  the other manager calls do (the note at `lifecycle.py:58`), so `vms/manager/__init__.py:65` and
  `:118` export the new name in place of the old.
- `confirm_vm_release` (`release.py:39`) builds `execution.vm_access(vm, config, Admin())` inside
  `gated_vm_boundary` and calls `observe_release(access, expected=None)`, so it runs only the
  release request, not the contract.
- `release.py` joins the static legacy-import check, and `debian.probe_debian_release`
  (`debian.py:149`) is deleted with its `ExecTransport` type import. `verify_os_release` and the
  rest of `debian.py` stay.

A VM without python3 can no longer be reinitialized or confirmed: both reach the VM only through
agw-launch and raise `VMNotReady`. HLA D10 anticipates this and assigns the way back to
`agw vm recover` (slice 4), which ships in the same PR (Scope).

## The operation log

`execution/oplog.py` holds one function and nothing else:

```text
format_record(request: RunRequest, result: Result, *, label: str) -> str
```

It returns one block: the label, the request's shape, the completion in the closed form
(`completed, exit 0`, `not delivered (reason)`, `unknown (reason)`), the two streams, and ssh's
diagnostics tail. The shape is the argv for a `Command`; for a `Script`, `script (SH, login)` naming
the shell and the login flag, with no source; then `elevated`, the `cwd`, the environment _names_
and the deadline. Environment values, script source and stdin are never passed to the formatter, so
no code path can write them. D13 says the log records a label: it is the caller's step heading, the
name of the `SSHLogger` step in effect (`logger.step(...)`) when the request ran. Streams are
retained as `Result` holds them, bytes decoded with replacement. A sensitive stdin keeping the
streams out of the log arrives with `Bytes(data, sensitive=)` in slice 5, its first consumer; this
slice has no stdin to log.

**Wiring.** `VMAccess` takes an optional `log: Callable[[RunRequest, Result], None]`. `run` calls it
after `_execute` and before it raises for a bad completion, so failures are logged. `stream`,
`attach` and `forward` take no log: their callers are operator commands that have never logged.
Constructing the access still does nothing (R9). `SSHLogger` gains `record(request, result)` (the
callable the commands pass, `logger.record`): it remembers the heading of the latest `step()` as the
label, calls `format_record`, and writes the block through its existing `_write`. Redaction
therefore stays at its one sink, with the property that the set is fixed at construction and covers
the raw and `shlex.quote`d form of each secret, and with the sanitized record and the file handler
kept in one function (the CodeQL clear-text-storage note, `ssh.py:250-262`). Commit `a38e49489`
("redact SSH logs before first write") made the "registered before the first write" rule; it holds
here unchanged. Per-request secret marking has no consumer yet (Amendments). Phase A and the
contract use the same logger as Phase B, so a create or reinit still writes one file.

The cost of "never script source" is that an initialization log does not show a script's text;
`SSHLogger.log_command` prints the whole command today, and keeps doing so for the legacy paths. The
step headings and the repository's scripts identify what ran.

**What does not move.** `LOG_DIR`, its five readers (`doctor_state.py:124`,
`vms/manager/_helpers.py:205`, `vms/manager/lifecycle.py:99`, `vms/manager/power.py:323`,
`cli/commands/vm.py:441`), the `agentworks.ssh.LOG_DIR` patch sites in tests, and the reader
consolidation go to slice 8, where `SSHLogger` is deleted and the log's home is chosen once. A
separate small bug sits at `power.py:325`: `LOG_DIR.glob(f"{name}-*.log")` also matches another VM
whose name starts with `{name}-` (VM `dev` and VM `dev-2`), so deleting `dev` removes `dev-2`'s
logs, and `vm logs dev` shows them. It is outside this slice's design and wants its own fix.

## `contract_version`

`ProvisionResult` changes shape here and slice 4 changes the ABC again. Registration requires an
exact match of the descriptor's version (`capabilities/README.md:566-570`), so a platform written
for the old shape is refused at load rather than failing after the VM exists. The vm-platform
`contract_version` is bumped once, in this slice, from 1 to 2, and slice 4 lands under the same
number in the same PR. The sites: the kind descriptor (`vm_platform/kinds.py:135`), the six
implementations (`lima.py:169`, `wsl2.py:498`, and `contract_version` in the AWS, Azure, GCP and
Proxmox platforms, `plugins/aws/platform.py:104`, `azure/platform.py:83`, `gcp/platform.py:182`,
`proxmox/platform.py:124`), the tests that assert it (`tests/test_wsl2_create_interrupt.py:140`,
`tests/plugins/test_gcp.py:89`) and any vm-platform fake that declares it. Every "version 1" mention
for this kind becomes "version 2": `kinds.py:62`, `capabilities/vm_platform/README.md:200` and
`:301`, `capabilities/README.md:732`, `plugins/README.md:206`, and ADR 0025's "internal version-1
vm-platform capability" (`:27`, amended in its status block, below).

## Tests

None restates the implementation or our prose.

**Unit.**

- **Contract.** The parser over fabricated script output: a problem line for each script-probed row,
  an unknown id dropped, a hostile or malformed detail still reporting its row, a missing END as one
  FAIL naming the exit status, a guest-invented account name dropped. The elevated request's outcome
  mapping, one case per outcome. `enforce`: a FAIL raises, a WARN-only report warns and does not
  raise. The shell fragments run for real under a local `sh` where they depend only on the process
  (`login-shells` over `root` and a nonexistent account; `base-tools` with a `PATH` of stubs; the
  pam_env cases moved from `test_pam_env.py` over temp files). `observe_release` accepts both
  recognized releases and refuses an unrecognized one and a mismatch with `expected`.
- **Reachability** (recording fake): immediate success; success after failures is confirmed by a
  second probe and a failing confirmation continues polling; exhaustion returns `False` after
  `ATTEMPTS`; `HOST_KEY`, `AUTH_FAILED`, any `NotDelivered` and a non-zero `true` raise on the first
  attempt with no sleep; `CONNECTION`, `TRUNCATED` and `DEADLINE` poll; `TrustMissing` propagates
  unpolled.
- **Phase A** (fakes for the database, platform and access): the order of the sequence, with the row
  re-read after the address is recorded; an invalid address fails before any connection and secures
  the VM; each of a first-contact timeout, an enrollment that left no key, a release mismatch and a
  base failure marks `FAILED`, records `provisioning_failed`, calls `secure_failed_vm` and leaves
  the process raising; **an interrupt before the contract passes secures the VM and marks it
  `FAILED`, and one after `COMPLETE` leaves it `COMPLETE`**; the post-ready wait returning `False`
  warns and proceeds; Phase B receives a VM-bound legacy target; the only `enrollment_access` call
  is the first contact. Reinit: no enrollment and no reachability wait, release drift refuses, a
  base failure stops before `run_initialization`, the agents reach the script's environment.
- **Provisioning.** `settle_tailnet_address`: a first-read success, a success on the last read, a
  value that does not parse treated as a failed read, exhaustion calling `logout` exactly once and
  raising, a failed logout naming the node, and the join having run exactly once in every case (a
  fake transport counting it). `parse_bootstrap_output` accepts a script that joined and printed no
  address. Each platform's `create` returns a parseable `tailnet_address` and none returns a
  transport.
- **Requests.** `PermissionDenied` for an agent-bound access with an elevated request, before
  `resolve_ssh` and with nothing spawned; `classify` and `outcome` for `AUTH_FAILED` (the row after
  `HOST_KEY`, and a 255 whose log holds neither token stays `CONNECTION`); the passphrase hint
  accompanies `AUTH_FAILED`.
- **Operation log.** `format_record` never contains an environment value, the script source or
  stdin, and shows environment names; `SSHLogger.record` redacts a registered secret that appears in
  argv, a stream or the diagnostics, in raw and quoted form; a record for a failed request is
  written before the error is raised; the label is the latest step.
- **Existing tests that change.**
  - `tests/conftest.py`'s `verified_debian_release` fixture (`:78-84`) patches
    `lifecycle.probe_debian_release`; its two module-level `pytestmark` users
    (`tests/vms/test_create_vm_dispatch.py:35`, `tests/vms/test_create_reinit_orchestrated.py:34`)
    move to faking `bootstrap_vm`'s result, and `test_create_vm_dispatch.py:111` and `:393` patch
    `lifecycle.probe_debian_release` directly and do the same.
  - `tests/vms/test_phase_a_platform_bootstrap.py` (`:68` rediscovery, `:108` returned address,
    `:147` enrollment, `:160` no key) is rewritten against `phase_a` and the required address.
  - The Phase A tests in `tests/vms/test_bootstrap_vm_hooks.py` (`:85`, `:109`, `:144`, `:161`,
    `:180`, `:200`, `:234`) take the new signature.
  - `tests/test_vm_release.py` and `tests/test_debian.py` follow `release.py` and the deleted
    `probe_debian_release`.
  - The `ProvisionResult` construction sites (`tests/transports/test_abc.py`,
    `tests/vms/test_create_vm_dispatch.py`, `tests/vms/test_create_reinit_orchestrated.py`) and the
    six platforms' create tests use `tailnet_address`.
  - The recording fake takes an identity.

**Conformance vectors added** to the execution LLD's list. The L lane runs under a real `sudo -n` on
CI runners that grant it, skips locally without it and fails on CI without it (that lane's rule);
the S lane needs a real `sudo` and a passwordless user too, so the elevation vectors are L and V.

| Vector                                                                                                                                                                         | Lanes |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----- |
| E1 An elevated `Command(("id", "-u"))` completes with `0`, and the payload's environment is the rebuilt one: root's `HOME` and `USER`, no `SUDO_*`, nothing from startup files | L V   |
| E2 Exact stdin positioning through `sudo`: the elevated payload reads the caller's first byte                                                                                  | L V   |
| E4 The stream check passes and the request completes under the distribution's default sudoers (`use_pty` as shipped), on a fresh VM; `sudo -V` is recorded                     | V     |
| E6 An elevated request over a background child that holds both streams completes at once with the right status                                                                 | L V   |
| A1 A wrong identity is `Unknown(AUTH_FAILED)` from the client log, on 8.5, 9.x and Win32-OpenSSH                                                                               | S V   |

**Live evidence** (R11; cells per the plan: create and reinit on every platform from at least one
workstation each, Debian 12 and 13 guests, an OpenSSH 8.5 client on one workstation or the
operator's disposition):

- Create on every platform shows python3 installed at provisioning (the first `true` completes), the
  required `tailnet_address`, the first connection enrolling and the release attested first; a
  Windows workstation among the clouds.
- **A fresh, never-initialized VM for E1, E2, E4 and E6**, on Debian 12 and Debian 13: the tester
  sends `SIGINT` to `agw vm create` as soon as "Provisioning complete." prints and before Phase B's
  first step. The row is `COMPLETE` with initialization pending (the interrupt is after step 5), so
  the VM has run only Phase A. The vectors then run with `AGW_CONFORMANCE_VM` before
  `agw vm reinit`.
- **A forced failure of the address read** on one cloud, one Lima and one WSL2 or Proxmox, from a
  throwaway branch whose `parse_tailnet_address` rejects every value (not shipped): the node is
  logged out (or named when the logout also fails), create fails with the backend rolled back, and
  the auth key was used once. The cell uses a single-use Tailscale auth key and the admin console's
  node and key-use views, so a second join would fail visibly.
- On a Debian 13 guest, `reachability.wait` with a key the VM does not authorize raises after
  exactly one connection (the sshd journal shows one authentication failure), and the correct key
  connects at once afterwards.
- Reinit on every platform, including a VM with pam_env settings (the warning, not a failure), a VM
  whose agent has an unsupported shell (the warning naming `agw agent reinit`), and a pre-python3 VM
  (typed `VMNotReady`).
- `vm confirm-release` from each workstation family, and the operator's `known_hosts` still
  byte-identical after a create.

## Incident audit

The entry points this slice moves are `create_vm`, `reinit_vm`, `bootstrap_vm`, release probing,
`tailscale_join.py` and `pam_env.py`. The audit read their fix history (for example `a38e49489`
redact SSH logs before first write, `93aca2cfb` verify created release in core, `eefaa2c5e` clean up
provisioning artifacts, `3bff603ce` log cleanup boundaries, `7307e2f60` make platform bootstrap
complete or raise, `2841cb9fa` guarantee Python for native guest helpers), ADR 0025 and the
bootstrap-fallback-removal and debian-release-transition SDDs. It adds these rows to the HLA's
incident inventory (`hla.md:748-778`; I21 is the last row today):

| Row | Behavior                                                                                                                                                                                                     | Re-proved in |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------ |
| I22 | An operator interrupt during Phase A still closes the kept VM's provisioning access before it propagates (`driver.py:193-211`)                                                                               | 3            |
| I23 | A first connection that left no key makes create fail where the cause is known (`driver.py:438-445`)                                                                                                         | 3            |
| I24 | The auth key is joined exactly once: a failed address read never causes a second join, and now logs the node out (`tailscale_join.py:65-71`, `7307e2f60`)                                                    | 3            |
| I25 | The platform's availability hold spans Phase A and Phase B (WSL2 idle shutdown) (`lifecycle.py:643`)                                                                                                         | 3            |
| I26 | A release the guest reports is attested by core, never taken from the platform; an unattested or mismatched VM is retained failed with delete guidance, never recorded (`93aca2cfb`, `lifecycle.py:599-626`) | 3            |
| I27 | Ordinary operations refuse live release drift; adoption is the explicit `confirm-release` (`release.py:30-36`)                                                                                               | 3            |
| I28 | A log's redaction set is complete before its first write, and no secret is retained in raw or shell-quoted form (`a38e49489`, `ssh.py:127-137`)                                                              | 3            |

I22 and I26 are re-proved by the Phase A interrupt and failure tests; I25 is the WSL2 create with
the first contact now over SSH while the hold is the only thing keeping the distro up (a live cell).
Re-proving I24 here does not re-prove I10 (the key out of command lines, logs and exceptions), which
stays slice 4's with the port off `ExecTransport`; this slice leaves the key-bearing code as it was.

## Collateral

- **ADR 0025.** An "Amended" paragraph in the status block, as ADR 0014 does for slice 2: core
  observes the release over SSH at the VM's tailnet address as the first check of the VM contract
  and persists only its own observation, instead of probing a transport the platform returns (the
  Decision sentence at `0025-...md:28-29`); and the vm-platform capability it names as "version-1"
  (`:27`) is now version 2. The decision, the registry, the recording and the absence of an operator
  bypass stand.
- **Platform author documentation:**
  - `cli/agentworks/capabilities/vm_platform/README.md`: the `ProvisionResult` description
    (`:306-317`), the Phase A bullet (`:575-580`) and the authoring step (`:724-731`) describe
    `tailnet_address`, the retry and the logout on exhaustion; `:200` and `:301` say version 2.
  - `cli/agentworks/capabilities/README.md:732-736`: "Core probes the returned transport" becomes
    core observing the release over SSH.
  - `cli/agentworks/plugins/README.md:206-221`: the same paragraph, including "return a transport
    through which core probes" and "may omit the Tailscale IP", which become the required address.
  - `base.py`'s `create` docstring (`:262-270`) and `secure_failed_vm` docstring (`:377`).
- **Command reference** (`cli/command-reference.md`): `vm reinit` (`:688-696`) states the contract's
  diagnoses, that pam_env and unsupported login shells are warnings, and that a VM without python3
  needs `agw vm recover` once slice 4 lands; `vm confirm-release` (`:705-712`) reads the release
  over SSH and so needs python3 as well; `vm create` gains its failure behavior.
- **`docs/guides/upgrading-to-0.20.md`:** `:110` (reinit and `confirm-release` of a VM that lacks
  python3); `:116`, whose "Accounts that already exist are not checked yet" becomes false, since
  reinit now warns about an existing account's shell; the `AUTH_FAILED` message; script text no
  longer appears in the operation log's records of the new requests.
- **`cli/agentworks/transports/ssh.py:8`:** the docstring lists `LOG_DIR` among
  `agentworks/ssh.py`'s primitives; it stays true until slice 8, so no edit now. Slice 8 edits it
  with the move.
- **`cli/agentworks/execution/README.md`:** `enrollment_access`, `reachability`, `oplog`, the
  `elevated` option.
- **Release notes:** python3 at provisioning; the contract at create and reinit; a reinit and a
  `confirm-release` that cannot reach a pre-python3 VM; the vm-platform contract version 2.
- **The commit** carries `BREAKING CHANGE:` footers (`CONTRIBUTING.md:69`) for the changed
  `ProvisionResult` contract and for reinit's new refusals.
- **Completions and sample config:** no new command or option and no setting changes, so none.

## Open questions

None.

## Amendments

The other SDD files are not edited by this slice's design. They need these changes before
implementation, for the owners to apply:

- **HLA D3.** The envelope does not hold the elevation choice; elevation is the `sudo -n` prefix of
  the stage 0 command (D4 already says so). Remove it from the envelope's field list.
- **HLA D4 and plan slice 3.** "Agent elevation and elevated attach refused before dispatch": the
  agent refusal is a runtime `PermissionDenied` derived from the access's identity; the elevated
  attach and an elevated handover are excluded by the types (`elevated` is on
  `BoundedRequestOptions`, not `LoginShell`), with no runtime refusal and no test of the types.
- **HLA D5.** `observation` is not in the vocabulary in slice 3; the "Common options" row and the
  "Retries" paragraph describe it as landing with its first caller. `VMAccess` never retries, and
  the one retry loop in slice 3 is `reachability.wait`, polling a fixed probe. Slice 5 adds the
  option when `detached.observe` needs it.
- **HLA D8.** `reachability.wait(access, *, wording)` returns `bool`, polls a fixed probe, polls
  only `CONNECTION`, `TRUNCATED` and `DEADLINE`, raises everything else at once (including
  `AUTH_FAILED`), and owns neither the exhaustion warning nor the proceed-anyway decision.
- **HLA D9.** Enroll mode covers a polled first contact, up to 17 connections (sixteen attempts and
  the confirming probe), not one connection. `accept-new` still records a key only when none is
  known. Enrollment is reached through the distinct factory `execution.enrollment_access`. The
  "first connection runs the base contract check" sentence (`hla.md:489-490`) is withdrawn: the
  first connection is the polled `true` probe in Enroll mode, and the contract runs on the VM-mode
  connections after it.
- **HLA D10.**
  - Remove "Tailscale up at the recorded address" from the base list (the connection proves it);
    `login-shells` is a warning for the admin and every agent, not a failure.
  - Drop the initialized checks and "Initialized checks run at the end of Phase B"; a failed install
    already fails Phase B.
  - The table is `(id, severity, probe)`; every FAIL names recreation. The release remains the first
    contract request, but "base checks run on core's first connection" (`hla.md:535`) becomes "on
    the VM-mode connections that follow the enrolling first connection".
- **HLA D11 (`tailnet_address` row).** The validation happens in the shared settle helper inside the
  platform's rollback window and again in Phase A, not in the value object. All six platforms read
  the address and log out over the transport they hold through `settle_tailnet_address`; the
  key-bearing script only joins and `parse_bootstrap_output` no longer requires an address.
- **HLA D13** (`hla.md:674-684`; the brief's `:658-659` is the error table, not this).
  - `oplog.py` only formats: `format_record`, with `SSHLogger.record` writing it. It no longer
    "lands with its `LOG_DIR` constant" and is not "the per-operation log" itself.
  - The record's label is the caller's `SSHLogger` step heading.
  - `LOG_DIR`, its readers and the test patch sites do not move in slice 3 (slice 8, with
    `SSHLogger`'s deletion); "the readers move to the new constant in slice 3" is withdrawn.
  - "The request's secret values" are the set registered at the log's construction, as `SSHLogger`
    does; per-request marking has no consumer yet.
  - The sensitive-stdin rule is implemented with `Bytes(sensitive=)` in slice 5.
- **HLA Migration and Incident inventory.**
  - Slice 3 (`hla.md:719-726`): remove "the initialized checks land too"; replace "`oplog.py` lands
    with its `LOG_DIR` constant and the readers that move to it" with `format_record` and
    `SSHLogger.record`, with `LOG_DIR` and its readers moving in slice 8; note `ProvisionResult` and
    the `contract_version` bump (1 to 2) in this slice.
  - The incident inventory (`hla.md:748-778`, not the plan) gains rows I22 to I28 as the Incident
    audit above lists them, each re-proved in slice 3.
- **Plan slice 3.**
  - Remove the `observation` option, the initialized checks and "the old `LOG_DIR` readers
    (`doctor_state.py`, `lifecycle.py`, `_helpers.py`, `power.py`, `vm logs`) and their test patch
    sites"; `oplog.py` is `format_record` and `SSHLogger.record`.
  - Add `UnknownReason.AUTH_FAILED`; `enrollment_access`; the new `phase_a.py` and the three files
    joining the static check; the `release.py` split and deleting `probe_debian_release`;
    `parse_tailnet_address`, `settle_tailnet_address` and the `parse_bootstrap_output` change;
    narrowing the provisioning trust file's lifetime; the `contract_version` bump to 2; `COMPLETE`
    written after the contract passes and `FAILED` on a Phase A interrupt; the test and vector lists
    above. Withdraw the test "each diagnosis names reinit or recreation" (`plan.md:221`): every FAIL
    names recreation, so it asserts nothing.
  - Add: slice 3 does not merge without slice 4, with no stopgap hint wording in between.
  - Slice 4 deletes the legacy `TailscaleWait` and `wait_for_reconnect` with their last callers.
  - Slice 5 adds the operation log's handling of a sensitive stdin and the `observation` option.
  - Slice 8 moves `LOG_DIR` and the reader consolidation, and edits the `transports/ssh.py:8`
    docstring.
- **execution-lld.md.** The classifier table gains `AUTH_FAILED` (a 255 with no start marker whose
  client log holds an authentication failure and no host-key failure); the error table's
  `ExecutionIncomplete` and the passphrase hint gain the new reason; `PermissionDenied` lands;
  `VMAccess` takes an identity, a trust mode and a log (the access-object section,
  `execution-lld.md:160-165`, says its trust is always VM and Enroll "arrives with slice 3"); "Left
  to the first consumer" is resolved for `elevated`, Enroll, `reachability` and the operation log,
  and `observation` moves to slice 5; the use_pty note is answered above.
- **trust-lld.md.** "Clouds before enrollment" and the test that the provisioning file "survives
  into core's post-create use" are obsolete: the file is scoped to `create`. Phase A's enrollment
  step list is replaced by the sequence above, and "Enroll mode is reachable only from create" is
  now carried by `enrollment_access` being a distinct factory.
- **FRD.** None.
