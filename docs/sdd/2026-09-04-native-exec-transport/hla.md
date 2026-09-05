# Native Execution Transport: High-Level Architecture

- Status: Design
- Date: 2026-09-04
- Requirements: [frd.md](./frd.md)
- Code basis: `origin/main` at `c962f52043e9ea239197ad96d5a383f98db9164d`

## Summary

Split the current transport abstraction at its natural seam. `ExecTransport` owns buffered,
noninteractive command execution. The existing `Transport` extends it with interactive shell,
streaming, copy, and file helpers. Every VM platform returns at least `ExecTransport` from one
required native hook. Platforms with the richer behavior continue returning `Transport`.

```text
                         ExecTransport
                    describe, logger, timeout
                               run
                                |
                    +-----------+-----------+
                    |                       |
                Transport          ProxmoxExecTransport
          interactive, stream,          QGA exec/status
             copy and files             no interaction
                    |
         SSH, Lima, remote Lima,
             WSL2 and cloud SSH
```

Core recovery sees only `ExecTransport`. The single operator-facing native shell path narrows to
`Transport` before calling `interactive`. There is no fallback, adapter registry, duplicate platform
hook, database state, or configuration switch.

## Current architecture and correction

The current `Transport` requires `run`, interaction, streaming, and copy behavior. Most native
consumers call only `run`: Debian release attestation, Phase A initialization, start-time Tailscale
repair, rekey, logout, and the native reachability probe. Only `vm shell --platform` calls
`interactive` on a native transport.

`VMPlatform.native_transport` nevertheless returns `Transport | None`, and the default `None` exists
solely for Proxmox. Core then converts `None` into a typed error. Proxmox creation works around the
same gap by returning a Tailscale-addressed `SSHTransport` after QGA has already joined the machine.

The correction makes the type match core's real dependency:

1. extract `ExecTransport` from the current base;
2. make full `Transport` extend it;
3. make `VMPlatform.native_transport` abstract and nonoptional, returning `ExecTransport`;
4. narrow `ProvisionResult.native_transport` and the native factory to `ExecTransport`;
5. keep current full returns on all platforms except Proxmox;
6. add a QGA-backed Proxmox execution transport; and
7. require the full subtype only at `vm shell --platform`.

## Transport roles

### Canonical transport

The canonical admin and agent transports remain Tailscale SSH and remain full `Transport` objects.
Normal shell, exec, session, console, workspace, agent, and file operations continue to use them.
Failure never triggers a native fallback.

### Native execution transport

The native transport is a platform-owned recovery and bootstrap channel. It can run a bounded
command independently of the VM's Tailscale state. Core uses it only where that independence is
structurally required.

The existing native factory remains the one composition root. It owns the transient-route context,
asks the platform for its transport, rejects an empty SSH target, and runs the reachability probe.
Its return type narrows from `Transport` to `ExecTransport`; it no longer handles `None` because an
omitted native implementation is a registration-time contract failure.

### Full native transport

A platform may return a full `Transport` from the same covariant hook. Existing platforms keep doing
so. `vm shell --platform` checks whether the returned object implements the full class and fails
with platform guidance if it does not. A second method would make every full platform repeat the
same native transport construction and route lifetime for one caller, so this design does not add
one.

## Execution contract

`ExecTransport` lifts the existing noninteractive contract unchanged:

- `default_timeout` and mutable `logger` attributes;
- `_resolve_timeout` and `describe`;
- `run(command, ...)` with the existing `sudo`, `tty`, `check`, `timeout`, environment, stdin,
  discard, retry, and callback parameters; and
- the existing result with return code, stdout, stderr, and `ok`.

Keeping `tty` on `run` preserves substitutability for existing full transports. An execution-only
implementation rejects `tty=True`; `None` and `False` both mean noninteractive execution. The
sensitive-input contract already forbids a forced TTY.

`Transport` retains `interactive`, `_interactive`, `call_streaming`, `copy_to`, `copy_from`,
`copy_dir_to`, and `write_file`. The two concrete file helpers stay on the full type because they
compose copy behavior that `ExecTransport` does not promise.

The current generic-in-practice `SSHResult`, `SSHError`, and `SSHLogger` names are preserved. A
cross-codebase naming migration does not improve the capability boundary and would dominate the
review surface. The LLD records them as compatibility types, not evidence that native execution is
SSH.

## Core ownership

The required execution type owns mechanism, not orchestration. Core still decides:

- when native access is justified;
- which command to run;
- when to switch to canonical Tailscale SSH;
- how long the transient route is held;
- whether a failed Tailscale repair stops an operation; and
- which secret values may be delivered through sensitive stdin.

The following paths narrow to `ExecTransport` without behavioral redesign:

| Path                        | Native operation                                      |
| --------------------------- | ----------------------------------------------------- |
| VM create                   | release attestation and Phase A initialization        |
| VM start                    | Tailscale reachability probe and repair               |
| Tailscale rekey             | authenticate through sensitive stdin                  |
| VM delete                   | best-effort Tailscale logout before provider deletion |
| `vm shell --platform`       | narrow to full `Transport`, then interact             |
| native reachability factory | bounded `echo ok`                                     |

This inventory is also a permanent testing seam: an execution-only fake must cross every row except
the shell row.

## Proxmox execution

### Construction

`ProxmoxExecTransport` receives the already-bound API client, node, VMID, VM admin username, logger,
and default timeout. No live config lookup or new persisted value is required. The existing site
context supplies the API token just as other Proxmox operations do.

VM creation constructs the same transport after QGA availability has been established and returns it
in `ProvisionResult`. Existing VMs construct it from their persisted `node` and `vmid` metadata.

### Dispatch and identity

QGA starts commands with root authority and cannot select a guest user. The adapter renders one
argument array:

```text
sudo=false: runuser to <admin username>, then bash -lc <command>
sudo=true:  bash -lc <command> as root
```

Environment values are scoped inside that shell invocation using the same quoting policy as the
other local native transports. The admin username comes from the validated VM row and remains an
argument, not interpolated shell syntax. One shared renderer produces the argv and has focused tests
for identity, environment, and quoting.

### Input and output

QGA's REST operation accepts an `input-data` string with a 65,536-character API-field limit. Both
Agentworks stdin modes use that field. The adapter rejects a larger request before dispatch. No
payload omits the field, which gives the guest command EOF.

`input_data` preserves ordinary stdout and stderr. `input_text` uses the established sensitive mode:
captured output is discarded, logger output contains no command result, and any provider exception
is replaced without retaining an unsafe cause. The input is never placed in the command array.

Status polling yields exit code, optional signal, stdout, stderr, and truncation flags. Complete
normal output maps into the existing result. A signal maps to a subprocess-style negative return
code when `check=False` and raises the normal checked failure when `check=True`. Either truncation
flag or an invalid provider shape raises a typed transport failure because Agentworks cannot honor
the complete captured-output contract.

### Deadlines and ambiguous execution

Dispatch and every status poll use the remaining overall deadline. Once Proxmox returns a PID,
Agentworks polls until exit or deadline. Proxmox exposes no cancellation endpoint. On timeout the
adapter stops polling, includes the provider PID in safe diagnostic context, and states that the
guest command may still be running.

The transport does not redispatch after an ambiguous dispatch failure or timeout. Doing so could
apply a mutation twice. The shared `retries` argument remains accepted, but Proxmox performs one
dispatch and does not call `on_retry`. Core callers that need idempotent convergence already own it
at the operation level.

### Existing bootstrap staging

Proxmox creation currently stages the private bootstrap through QGA file-write, executes it, and
removes it in a `finally` path. This remains unchanged. It solves a larger script-delivery problem
and already has interruption and cleanup coverage. The new transport uses direct `input-data` for
the small stdin payloads required by ordinary `run` calls.

## Error model and operator guidance

The new boundary reuses existing typed transport errors. It distinguishes:

- contract absence, which registration and conformance tests prevent;
- native route or reachability failure, which retains the platform probe hint;
- QGA unavailable or invalid provider responses;
- remote nonzero exit under `check=True`;
- deadline exceeded with possibly continuing execution;
- output truncation; and
- interactive native shell unsupported.

`vm shell --platform` reports that Proxmox supports native administrative execution but not an
interactive native shell, then points to the Proxmox console. It does not mention a fallback or
present recovery execution as an operator-selectable shell.

## Capability and release compatibility

The capability API is internal and all bundled implementations ship together. Contract version 1
changes atomically. A version bump or compatibility adapter would describe consumers that do not
exist.

There is no database, config, CLI grammar, machine-output, or completion change. The runtime work is
scheduled after 0.18.0. Its implementation branch must start from the post-0.18 release baseline or
otherwise be explicitly excluded from that release before merge intent.

The repository currently promises Proxmox VE 8, whose QGA exec operations use the already-granted
`VM.Monitor` permission. Current Proxmox VE documentation describes newer guest-agent permissions.
This effort preserves the declared VE 8 scope rather than silently expanding provider support.

## pyinfra direction

pyinfra confirms that command execution is a useful lower seam, but its connector is coupled to
pyinfra host, state, inventory, argument, fact, and operation models. Adopting it here would not
replace the QGA carrier and would force a much larger architecture decision into a recovery fix.

Agentworks therefore keeps `ExecTransport` small and owned. If a later effort evaluates pyinfra for
Phase B initialization, it should adapt a full canonical `Transport` behind a disposable connector
prototype and measure the value of operations and facts. It should not change #727's required native
recovery contract.

## Risks and safeguards

### The split grows into a transport hierarchy

Safeguard: introduce exactly one narrower base and one existing richer subtype. Reuse the single
native hook and factory. No capability registry, mixin set, protocol matrix, or adapter layer.

### Core accidentally depends on rich behavior

Safeguard: annotate every recovery and create-time path with `ExecTransport` and prove them with an
execution-only fake. Restrict the one full-type narrowing to `vm shell --platform`.

### QGA runs ordinary commands as root

Safeguard: render the non-sudo case through the validated VM admin username and test both
identities.

### A timeout is mistaken for cancellation

Safeguard: preserve the PID, never redispatch ambiguously, and report that execution may continue.

### Sensitive input leaks through provider diagnostics

Safeguard: never place it in argv, discard result streams, sanitize provider failures without unsafe
exception chaining, and inspect logs, results, exceptions, and mocked HTTP traces in tests.

### A provider change expands the permission scope

Safeguard: implement and document the currently promised VE 8 contract only. Treat VE 9 permission
setup as its own compatibility change with provider-version validation.

## Rollback

The implementation has no persisted migration. Before release, one git revert restores the old types
and the documented Proxmox limitation. After release, a rollback would again make Proxmox unable to
recover Tailscale and would make its required hook non-compliant, so prefer a forward fix. No
database or configuration restore is required.
