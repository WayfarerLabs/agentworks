# Native Execution Transport: Prior-Art Research

- Status: Design
- Date: 2026-09-04
- Research basis: official documentation and upstream source inspected on 2026-09-04

## Executive summary

Two prior-art families inform this design and neither replaces the work.

pyinfra separates connector mechanics from higher-level operations and protects hidden command
values. Those ideas support a small execution boundary and centralized command rendering. Its
connector is nevertheless coupled to pyinfra's host, state, inventory, fact, and operation models.
Adopting or copying it for #727 would create a configuration-management decision without supplying
the missing QGA carrier.

Proxmox and QEMU already expose the exact noninteractive primitive Agentworks needs: start a guest
process with argv and optional stdin, poll by PID, then receive exit, signal, output, and truncation
facts. The provider does not offer cancellation or terminal semantics. The truthful design is an
execution-only adapter with explicit timeout uncertainty, not a simulated full transport.

## pyinfra

### Connector boundary

pyinfra's `BaseConnector` exposes shell command execution and file transfer, plus lifecycle methods
such as connect and disconnect. `run_shell_command` is the closest analogue to Agentworks
`ExecTransport`. Its interface is not standalone, however: calls receive pyinfra `State` and `Host`
objects and participate in pyinfra's deploy execution model.

Adopted lesson: keep the lowest carrier interface small enough that higher orchestration can depend
on execution rather than interactive terminal behavior.

Rejected conclusion: use a pyinfra connector as the native vm-platform API. Agentworks would still
need to write QGA delivery, and recovery commands would acquire unrelated inventory and deploy
dependencies.

### Commands and hidden values

pyinfra represents commands as values and provides hidden command fragments so logs can differ from
executed argv. It also centralizes shell, environment, privilege, and directory wrapping.

Adopted lesson: keep QGA identity and environment rendering in one pure helper, and keep sensitive
stdin outside command argv and diagnostics.

Rejected conclusion: import or copy pyinfra's command object graph. Agentworks already has a stable
string command contract, validated environment values, quoting helpers, sensitive stdin, and
redacting logs. A second command model would widen every caller for no #727 requirement.

### Facts and operations

pyinfra facts cache observed host information, while operations express convergent desired state.
Both depend on pyinfra inventory, host, and state lifecycles and on its prepare and execute phases.

Potential later use: evaluate pyinfra behind a full canonical Agentworks transport for Phase B
initialization, where idempotent operations and reusable facts might replace bespoke scripts.

Boundary for #727: native recovery must remain usable before canonical Tailscale connectivity and
must not depend on a configuration-management engine. No pyinfra dependency, vendoring, adapter, or
facts cache belongs in this effort.

## Proxmox and QEMU Guest Agent

### API shape

The Proxmox VE 8 guest exec endpoint accepts a command array and an optional `input-data` string
with a 65,536-character API-field limit, then returns a PID. The status endpoint reports whether
execution ended and may report exit code, signal, stdout, stderr, and explicit output-truncation
flags. This maps directly to the buffered `run` contract.

QEMU's guest-exec model likewise accepts an argument list, optional base64 input, and captured
output, with a separate guest-exec-status call. It is process execution, not a PTY or file-transfer
transport.

Adopted lesson: use direct stdin for ordinary `run` payloads, poll by PID, and treat provider
truncation as a contract failure rather than silently returning partial output.

Rejected conclusion: retain temporary-file staging for every stdin payload. Staging remains useful
for the existing large private bootstrap, but adds cleanup and leak risk to small Tailscale keys
that the provider can deliver directly.

### Timeout and retry

Neither the Proxmox guest-agent API nor QGA exposes cancellation for a dispatched guest-exec PID.
Proxmox's own CLI can stop waiting after its timeout but cannot prove the process stopped.

Adopted lesson: one deadline covers dispatch and polling; timeout reports possible continued
execution; no ambiguous dispatch is blindly retried.

Rejected conclusion: apply the SSH connection retry policy to QGA. A second dispatch can duplicate
an administrative mutation.

### Identity and permissions

QGA executes with the guest agent's root authority and does not accept a guest username. Agentworks
must deliberately run ordinary commands as the configured admin user and reserve root for
`sudo=True`.

The repository currently supports Proxmox VE 8. Its guest-exec endpoints require `VM.Monitor`, which
the existing setup role grants. Current VE documentation shows finer-grained guest-agent
permissions. That difference is evidence against using unversioned current docs to claim VE 8 setup
is wrong, and against silently adding VE 9 support here.

## Existing Agentworks prior art

The locked polymorphic-transports SDD chose one transport object per delivery mechanism and
explicitly rejected automatic native fallback. That decision remains load-bearing.

Current Lima, remote Lima, WSL2, and SSH transports already share one `run` contract, including
finite stdin and sensitive input. Core uses only that method for release attestation, Tailscale
repair, rekey, and logout. The only native interaction consumer is `vm shell --platform`.

Proxmox provisioning already enables QGA, waits for it, stages bootstrap input, executes it, and
cleans up the temporary file. The new adapter should reuse that authenticated API seam and leave the
specialized bootstrap lifecycle alone.

## Refuted assumptions

| Assumption                                     | Evidence and ruling                                     |
| ---------------------------------------------- | ------------------------------------------------------- |
| pyinfra supplies the Proxmox carrier           | It supplies connector abstractions, not a QGA connector |
| a native transport must be interactive         | Every core recovery consumer uses only buffered `run`   |
| QGA can emulate `vm shell --platform` safely   | Guest exec has no PTY or interactive stream contract    |
| provider timeout cancels the guest process     | No guest-exec cancellation endpoint exists              |
| all QGA input requires file staging            | Proxmox exposes bounded direct `input-data`             |
| QGA ordinary execution naturally matches admin | QGA starts with root authority and needs demotion       |
| current Proxmox permissions describe VE 8      | VE 8 and current VE permissions differ                  |
| a capability-version bump protects consumers   | The contract is internal and all bundled code is atomic |

## Sources

| Source                                                                                                                                  | Quality                       | Angle used                            |
| --------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------- | ------------------------------------- |
| [pyinfra connector API](https://docs.pyinfra.com/en/3.x/api/connectors.html)                                                            | Primary project documentation | Connector scope and lifecycle         |
| [pyinfra BaseConnector source](https://github.com/pyinfra-dev/pyinfra/blob/3.x/src/pyinfra/connectors/base.py)                          | Primary upstream source       | Host and state coupling               |
| [pyinfra command source](https://github.com/pyinfra-dev/pyinfra/blob/3.x/src/pyinfra/api/command.py)                                    | Primary upstream source       | Command values and hidden fragments   |
| [pyinfra connector utilities](https://github.com/pyinfra-dev/pyinfra/blob/3.x/src/pyinfra/connectors/util.py)                           | Primary upstream source       | Central command wrapping              |
| [pyinfra facts](https://docs.pyinfra.com/en/3.x/facts.html)                                                                             | Primary project documentation | Fact lifecycle and caching            |
| [pyinfra operations](https://docs.pyinfra.com/en/3.x/using-operations.html)                                                             | Primary project documentation | Desired-state execution model         |
| [pyinfra package metadata](https://github.com/pyinfra-dev/pyinfra/blob/3.x/pyproject.toml)                                              | Primary upstream source       | Dependency and framework weight       |
| [Proxmox VE 8 guest exec](https://pve.proxmox.com/pve-docs-8/api-viewer/#/nodes/{node}/qemu/{vmid}/agent/exec)                          | Primary provider API          | Command, stdin limit, permission, PID |
| [Proxmox VE 8 guest exec status](https://pve.proxmox.com/pve-docs-8/api-viewer/#/nodes/{node}/qemu/{vmid}/agent/exec-status)            | Primary provider API          | Exit, signal, output, truncation      |
| [Current Proxmox guest exec](https://pve.proxmox.com/pve-docs/api-viewer/#/nodes/{node}/qemu/{vmid}/agent/exec)                         | Primary provider API          | Permission evolution                  |
| [QEMU guest-exec reference](https://www.qemu.org/docs/master/interop/qemu-ga-ref.html#command-QGA-qapi-schema.guest-exec)               | Primary upstream API          | Guest process and input model         |
| [QEMU guest-exec-status reference](https://www.qemu.org/docs/master/interop/qemu-ga-ref.html#command-QGA-qapi-schema.guest-exec-status) | Primary upstream API          | Asynchronous result model             |
