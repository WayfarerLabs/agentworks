# Owned SSH Local Forwarding

- Status: Implementation design; installed-server/platform acceptance remains open
- Requirements: [FRD R2](frd.md#r2-isolated-connection-and-authentication-policy) and
  [R4](frd.md#r4-one-attempt-byte-safe-io-and-truthful-evidence)

## Surface and ownership

`open_local_forwards(connection, forwards, deadline=...)` opens explicitly requested local TCP
forwards using one owned foreground client process. A `LocalForward` contains a numeric local bind
address, local port, literal destination host and destination port. Non-loopback binding requires
that explicit address, never an inherited config option. Ports are 1-65535; there is no automatic
port allocation, SOCKS proxy, remote forwarding or arbitrary option passthrough in this surface.

The returned `OwnedForwarding` is a context manager with `wait()` and idempotent `close()`.
Composition owns it separately from execution and closes it within its operation lifetime. Passive
connection/target access does not create forwards. The startup deadline covers local validation,
installed-client version checking, authentication and setup acknowledgment; it does not silently
become a lifetime limit after successful startup. Wait interruption closes owned resources and
propagates. Later remote workload cancellation remains transport-owned.

## Positive setup evidence

Use the same isolated connection/trust policy as command delivery, with no PTY, mux, background fork
or local command. The forwarding operation selects `ClearAllForwardings=no` for its explicit `-L`
operands and `ExitOnForwardFailure=yes`; all inherited config remains disabled. Requesting both IPv4
and IPv6 means separate numeric-address specifications. `localhost` is not a bind selector: OpenSSH
can succeed on one resolved address while another fails, which would hide partial setup.

A running `ssh -N` process supplies no positive ready acknowledgment. Instead, use the supported
POSIX account shell to emit one generated hexadecimal nonce and wait on its owned input:

```sh
printf '%s\n' 'agw-forward-ready-<nonce>'; IFS= read -r _; exit 0
```

Keep the client stdin pipe open. Require exactly the expected line on stdout before returning the
resource. Bound startup output and drain both pipes fairly; noise, wrong acknowledgment, early
exit/EOF, source/sink error or deadline expiration fails setup with bounded local cleanup. No
arbitrary server diagnostic text is parsed. Continue draining/discarding diagnostics during the
resource lifetime so full output pipes cannot stop the client. Do not retain payload-bearing
exception text or unbounded logs.

OpenSSH initializes local forwarding and checks listener failures before opening the remote session.
Its acknowledgment therefore establishes local listener setup and an authenticated remote session,
not destination-service health or permission for every later forwarded connection. The remote
account must permit the held command and have the documented compatible POSIX shell; forwarding-only
accounts that prohibit execution refuse. Current VM forwarding uses a shell-capable admin account.
No guest file, daemon or job record is created by this acknowledgment.

Closing ends owned stdin, then bounds local process termination/reaping and closes its handles.
Partial listener setup must also be cleaned up on failure. Reaping the local client establishes
local cleanup, never guaranteed prompt remote-shell termination after a network partition.
Keepalives default to the existing interactive policy (15 seconds, four unanswered probes), with
explicit validated connection settings. They detect lost peers; they neither renew the startup
deadline nor promise guest cancellation.

Do not use `LocalCommand` for acknowledgment: despite its useful ordering, it invokes a workstation
shell. Windows command-processor startup can execute ambient policy before even a fixed marker,
which defeats the intended local configuration isolation.

## Launch ownership integration

At SSH `6efaffce` on transport `5b570442`, forwarding still constructs its client on the caller
before creating `OwnedForwarding`. A disposable synthetic constructor probe created an owned local
child and then raised `KeyboardInterrupt` before returning its handle. The interruption propagated
while that child remained live; the probe then killed/reaped the exact child and closed its pipes.
This establishes the ownership gap, not native signal or installed-SSH acceptance.

Transport now supplies `LocalProcessOwner` in `_process.py`, shared with `run_owned_process`. SSH
retains the owner before any launch can occur. A forwarding drain worker starts inert;
`LocalProcessOwner.start()` performs default-deny process admission, and SSH allows pipe borrowing
only after `start()` returns successfully. This ordering matters because an interrupted shared
admission can settle its process internally before returning to SSH. No SSH worker may borrow pipes
during that internal settlement.

The drain worker observes immutable snapshots and handles pipe readiness, marker validation and
diagnostic drainage. SSH stops every pipe user before releasing the owner. The owner retains exact
client construction, status observation and cleanup responsibilities. Natural exit is distinct from
the local status produced by cleanup. Serialized, idempotent forwarding close coordinates with wait;
interruption must settle ownership before propagating the original control exception.

The local kill/reap allowance remains bounded once the process is available. Process construction
itself has no proven hard time bound, so total startup/settlement time cannot inherit that cleanup
bound. The startup deadline does not become a held-resource lifetime limit. Local cleanup is never
remote cancellation.

The operator authorized a scoped SSH contribution to this extraction on 2026-09-21. The extraction
was already published in transport, so SSH adopts it rather than introducing a second owner. Any
integration-required shared correction remains a separable contribution. Transport retains terminal
and RunContext ownership. The startup, repeated-interruption, natural-exit, cleanup-uncertainty and
native-platform proof gates remain open until their measured results are recorded.

## Evidence

Focused tests cover split acknowledgment reads, missing/wrong/noisy acknowledgment, auth/trust and
exec refusal, process exit, bounded deadline/interruption cleanup, concurrent diagnostic drainage,
occupied first or later listeners, IPv4/IPv6 partial failure and prompt port rebinding after close.
Installed-client tests must demonstrate actual forwarding and failure, not only inspect argv.
Windows and macOS need their own handle/lifetime evidence. Destination failure after readiness is
reported honestly without retroactively claiming setup failed.

Source grounding:
[OpenSSH 8.5 forwarding setup](https://github.com/openssh/openssh-portable/blob/V_8_5_P1/ssh.c),
[listener creation](https://github.com/openssh/openssh-portable/blob/V_8_5_P1/channels.c), and
[ExitOnForwardFailure](https://man.openbsd.org/ssh_config#ExitOnForwardFailure). These establish the
mechanism to test, not acceptance of our implementation or platform coverage.

The readiness marker must be the first stdout bytes. Once it is complete, later stdout is discarded,
including a suffix delivered in the same pipe read. Pipe chunk boundaries do not alter acceptance;
wrong markers and preceding output still refuse.
