# Runnable status inspection

VMs, sessions, and named consoles share one inspection grammar while keeping status meanings owned
by each resource. Their list commands are local inventory reads by default:

```console
agw vm list
agw session list
agw console list
```

Add `--status` when current runtime state is needed. Agentworks selects and validates rows first,
prints one progress line in human mode, and then observes only those rows. The status request cannot
be combined with `--names-only`, so completion remains a local one-name-per-line operation.

Describe is a focused inspection and includes live status by default:

```console
agw vm describe build-vm
agw session describe review
agw console describe development
```

These observations do not start a VM, repair a session, create or destroy tmux state, or persist an
observed result. An expected provider, credential, identity, or transport failure keeps the local
facts and reports status as `unknown`. One failed VM or provider boundary does not remove successful
rows from a list.

Corrupt or unsupported persisted applied-state is different from an unavailable or mismatched SSH
identity: it remains a typed error because Agentworks cannot trust the structural record.

## Status meanings

| Resource | States                                                | Authority                                                                  |
| -------- | ----------------------------------------------------- | -------------------------------------------------------------------------- |
| VM       | `running`, `stopped`, `deallocated`, `unknown`        | The selected VM platform's status operation                                |
| Session  | `running`, `stopped`, `residual`, `broken`, `unknown` | Exact managed tmux session, dedicated server, then stored process identity |
| Console  | `running`, `stopped`, `residual`, `unknown`           | Exact canonical and reserved staging names from one tmux enumeration       |

A stopped or deallocated VM also shows `manual` when `agw vm stop` recorded operator intent, or
`idle` when Agentworks may start it on demand. Session `residual` means the dedicated tmux server is
present without the canonical managed session. Session `broken` means same-boot process evidence is
live while tmux is unreachable. Console `residual` means its reserved staging session exists,
whether or not the canonical console also exists.

The status describes Agentworks-owned runtime state. It does not claim that an agent harness has
useful work in progress.

## Bounds and failure isolation

Session and console observers create the canonical SSH transport directly, with no VM activation or
provider credential work. They use `tty=False`, one attempt, and a 10-second timeout. Probes that do
not send fact data explicitly close stdin so non-interactive Windows SSH clients do not retain the
operator's console handle. Sessions use one compound probe per selected VM; consoles use one exact
tmux session-name enumeration per selected VM. Independent VMs run with finite concurrency.

VM observation uses the existing version-1 platform status operation. It may run that operation's
provider preflight and resolve its declared credentials, but it does not run authenticated runup
tests or use a guest transport. Status calls are serial within a provider site and may run in
parallel across at most eight sites.

The bundled WSL2 and local or remote Lima status calls have a 10-second process or transport bound.
GCP passes a 10-second timeout with retries disabled to its instance read. Proxmox passes a
10-second timeout to its status HTTP request. The AWS and Azure SDK status calls retain their client
defaults because their shared clients do not expose a safe status-only total deadline; those calls
may exceed 10 seconds after the progress line has been printed.

## Machine output

JSON keeps the same v1 envelope and stable fields whether status is requested or not. Plain session
and console list records carry `"status":"unavailable"`; requested inconclusive observations carry
`"status":"unknown"`. Plain VM list records carry null `observed_status` and `status_disposition`;
requested inconclusive observations carry `"observed_status":"unknown"`. Describe never uses a
not-requested sentinel because it always observes.

The 0.18 producer emits the VM and console status fields on every applicable record. They remain
additive JSON v1 fields: consumers must tolerate their absence when reading output from an older v1
producer.

The exact JSON record shapes and ordering are documented in the
[CLI command reference](../../cli/command-reference.md#machine-readable-output).
