# Owned SSH Local Forwarding

- Status: Implementation design; retained cleanup adaptation and platform acceptance remain open
- Requirements: [FRD R2](frd.md#r2-isolated-connection-and-authentication-policy) and
  [R4](frd.md#r4-one-attempt-byte-safe-io-and-truthful-evidence)

## Current surface and ownership

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

## Retained cleanup adoption gap

Transport's retained cleanup source at `12dcb01b` separates the first immutable cleanup observation
from an explicitly bounded retry. The current forwarding close path still requests the former;
repeated public close cannot retry a failed native cleanup. Failed startup also propagates its error
before returning the forwarding resource. The caller's discovery custody holds only the version
probe and cannot settle that separate forwarding session.

A focused synthetic proof at SSH `91027e61` reproduces both branches using owned local Python
children. Discovery custody settles while forwarding cleanup remains retryable; repeated public
close requests no retry. A separately retained test-only reference can close the exact native owner
through the bounded API, reap the child and close its pipes. That rescue is proof safety, not an
existing production capability or native SSH acceptance.

The enclosing forwarding lifetime must retain the same native owner before admission, including when
startup fails before readiness. After stopping pipe use it must support serialized bounded cleanup
retries, retaining pending or lost ownership. Adding retries only to an already returned resource
leaves failed startup unresolved. This remains an implementation/design gate; no second native owner
or cleanup capability inside reports or exceptions is introduced.

### Proposed caller-held forwarding interface

This API revision awaits the operator's decision and is not implemented. Reuse `OwnedForwarding` as
the passive resource held before startup, with one explicit start operation:

```text
OwnedForwarding(connection: SSHConnection, forwards: Sequence[LocalForward])
OwnedForwarding.start(deadline: Deadline, custody: LocalDeliveryCustody) -> None
OwnedForwarding.wait() -> int
OwnedForwarding.close(deadline: Deadline) -> bool
```

The constructor validates and retains settings without trust-file admission, client discovery,
thread startup or listener creation. Startup uses the caller's discovery storage and the resource's
existing shared native owner; it never allocates a replacement forwarding owner. Successful start
establishes readiness. Failure or interruption leaves the same resource with its caller, including
when readiness was never reached. The separate discovery storage also remains caller-held until its
own settlement.

Closing permanently prevents new startup, bounds drainer shutdown and stops all pipe use before
asking that owner for bounded cleanup. Incomplete drainer or native settlement returns incomplete
cleanup without releasing ownership. An explicit later close may retry through the same native
owner. The startup deadline still does not limit a successfully held session's lifetime. This
replaces factory startup before the caller receives ownership without adding a second forwarding
coordinator or changing the raw carrier contract.

In this revised API, `close(deadline)` is the sole cleanup operation; there is no context-manager
cleanup. `wait()` observes the local client's natural exit or an observation failure, without
requesting cleanup or releasing ownership. It returns a known natural exit status or raises safe
forwarding evidence when that status cannot be established. Waiting has no implicit timeout;
interruption propagates while the caller retains the same resource. Another caller may explicitly
close the resource while waiting; a cleanup-produced status is not reported as natural exit.

The caller attempts close in its enclosing cleanup path with a fresh finite deadline and explicitly
handles `False` by retaining the resource for retry. If startup or wait already raised a control
exception, cleanup failure must not replace it: preserve that exception, record sanitized cleanup
failure or incompleteness separately, and keep custody held. Without an existing failure, a cleanup
error remains explicit. Reports, exception causes and diagnostic notes contain no live resource
capabilities. Neither natural exit nor a returned status proves completed cleanup.

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
