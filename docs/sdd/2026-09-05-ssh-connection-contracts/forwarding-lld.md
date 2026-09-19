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

The returned `OwnedForwarding` is a context manager with `wait()` and idempotent bounded `close()`.
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
resource lifetime so backpressure cannot stop the client. Do not retain payload-bearing exception
text or unbounded logs.

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
