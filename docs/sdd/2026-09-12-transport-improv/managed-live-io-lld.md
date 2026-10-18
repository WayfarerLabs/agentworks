# Managed foreground pipe I/O

<!-- cspell:ignore AGWL AGWE -->

This is the response design for `run(..., profile=MANAGED, lifetime=OPERATION)` with borrowed live
input, explicit live output, or both. It extends the existing managed controller, not the carrier
contract or public API. Implementation and native acceptance remain open. The
[lifecycle design](execution-lifecycle-lld.md) continues to own launch, leases, stop and resource
closure.

## Keep launch separate from observation

Retain the existing bounded buffered start, protected request, exact launch acknowledgment, settled
local start delivery and dedicated lease keeper. Only after that acknowledgment does the caller open
a separate foreground relay through the same carrier interface. The existing controller owns one
private per-run Unix socket, not another daemon. The socket is an internal implementation object,
not an addition to public FileAccess.

The alternative `systemd-run --pipe` would keep the start carrier alive for the entire foreground
call. The existing keeper deliberately requires settled start delivery before renewal. Supporting
that alternative would therefore change launch-custody and renewal admission, not merely connect
descriptors. The private socket retains those guarantees and needs no early launch acknowledgment,
additional owner, reconnect registry or second supervisor.

`start` and independent lifetime continue to refuse borrowed live endpoints before dispatch.
Finite-input/live-output and live-input/capture-or-discard are separate valid combinations; live
mode never implies both directions. Unsupported live carrier routes refuse before reservation,
activation or endpoint borrowing.

## Protected request and retention

Add a frozen primitive `ManagedLiveIO(nonce, input, output)` to the protected request, with a
keyword-only optional `live_io` field. The nonce is exactly 32 lowercase hexadecimal characters,
both direction values are exact booleans, and at least one direction is selected. This is
correlation data, not a bearer grant. No source, sink, callback or application bytes occur in this
value or JobRef.

The canonical request-control object includes `live_io` only when present, with exactly those three
fields. A live input has an empty stored stdin asset, selected explicitly by metadata rather than
inferred from empty bytes. Every live request requires OPERATION lifetime and the ordinary actual
operation lease at dispatch. Body-only preparation retains its existing ability to precede that
lease sample.

Persisted `ManagedOutputPolicy` describes durable retention. Explicit live output uses the existing
DISCARD policy, no capture ceiling and no output spool, plus `live_io.output=True` to preserve the
distinct presentation request. This does not substitute discard for the requested presentation. The
foreground result must also prove presentation independently. Durable discarded stream-end facts
mean the workload pipe ended with no retained bytes; they do not assert host delivery. Later
`read_output` cannot reconstruct presentation, and later `wait` may honestly report discarded
retention without repairing an earlier failed live result.

Effective sensitivity still reaches preparation and CarrierIO. Explicit live output permits raw
presentation, not logging or retention, even for sensitive work. Sensitive calls without explicit
live output remain suppressed. Live input alone does not authorize output presentation. No new
durable mode, SQL enum or delivered-output fact is needed.

## One-use I/O admission

Before publishing launch and notifying readiness, the same controller binds one fixed `live-io.sock`
leaf through its held protected run directory. Bind is create-only; do not unlink an existing object
to retry. Close the listener and accepted connection on the workload side of fork, before applying
its identity. The workload still waits behind the existing placement, identity and release gates.
The controller may publish launch while that final gate remains closed: launch acknowledgment is not
proof that application code entered.

Enter the existing observation loop immediately after notification. It must observe stop, current
lease, exact child wait and cleanup while accepting input; do not add a blocking accept or handshake
before that loop. Admission has a fixed 30-second window measured on the guest's monotonic clock,
independent of renewal. On expiry or stop, permanently close admission and the release gate, then
perform the normal bounded setup-child cleanup. A healthy keeper cannot keep an unattached gated
child indefinitely. The window is a setup bound, not a new public timeout option or an extension of
the caller's deadline.

The fixed foreground relay revalidates root/helper identity, guest incarnation, the exact protected
launch and request directions before connecting. Its own bounded control envelope is read exactly
before any live stdin suffix. The envelope also carries the original managed call's remaining finite
observation allowance as a positive bounded integer number of nanoseconds. Preparation samples it
after settled start, not from a fresh timeout. The guest applies one monotonic expiry through
connection, handshake and pumping, including every selector wait. This prevents a root relay with
blocked stdout from surviving indefinitely after controller closure. It neither cancels the job nor
synchronizes workstation and guest clocks: the host retains its original deadline and any uncertain
helper custody until positive closure. Unbounded DIRECT execution is unchanged.

The connection sends exactly the canonical 43-byte handshake:

```text
AGWL1 <32-lowercase-hex-nonce> <0-or-1-input> <0-or-1-output>\n
```

The controller accepts only this exact run-bound handshake. It sends the binding frame completely,
rechecks stop and current lease, and releases the child once. Accept one connection attempt only;
invalid, partial, expired or interrupted admission never authorizes reconnect or input replay.
Retire the listener after the attempt. A lost I/O reply does not erase the already acknowledged
launch.

## Framing and bounded progress

Use existing nonce-bound AGWE1 framing and its 4,096-byte application records. The first managed-I/O
`LAUNCHING` frame contains the exact canonical launch receipt. In this separate grammar it binds the
existing job, not a second launch or an application-entry fact. Live output then carries separate
STDOUT/STDERR records and one STREAM_END per stream, with represented-byte counts and rolling
digests. FINISHED closes this I/O exchange. Input-only live mode has no application output records.
Normal wait and boundary facts remain in the existing durable protocol; the I/O grammar does not
infer them from relay exit.

The controller encodes frames once. The relay copies socket output as bytes, without
decoding/re-encoding application frames or combining application stderr with its own diagnostics.
The host consumes the existing bounded decoder and presentation mechanics with a managed-specific
grammar, not InlineObserver's application-launch/wait grammar.

Every socket, workload pipe and framing operation is non-blocking. Retain at most one bounded input
chunk and one unfinished output frame. A full presentation queue stops further output reads, not
lease checks, exact child observation or cgroup cleanup. Control frames also need non-blocking
writes; the existing blocking `FrameWriter.emit` cannot simply be inserted into the controller loop.
Alternate the preferred ready output stream after each queued data frame rather than depending on
descriptor registration or kernel readiness ordering. A continuously readable stdout must not starve
a stderr producer needed for exit. Retired descriptors leave the selector so its completion
condition remains valid.

Input EOF half-closes only that direction. Finite-input/live-output work does not stop when relay
input ends. Workload input closure or observed child exit stops offering input and propagates
consumer closure without aborting remaining output. The root relay closes its owned input descriptor
on consumer closure, preserving the shared pump's ordinary broken-pipe semantics. Supplied host
endpoints remain caller-owned.

After attachment loss, retire the connection and pending presentation, close live workload stdin
where appropriate, and continue draining output without retention. Do not reopen the socket, spool
sensitive bytes or let a blocked sink delay supervision indefinitely. Existing bounded post-exit
cleanup still applies. Missing final presentation evidence remains missing; successful durable
cleanup cannot turn it into delivered output.

## Lifetime, results and disposal

Ending live observation does not end the owning operation or implicitly stop an entered job. The
keeper continues until explicit stop, operation closure or lease expiry. The caller receives its
safe job reference and actual relay/presentation failure; original control exceptions keep their
identity and custody facts. Loss before I/O admission is different: the fixed pre-entry window
eventually refuses release of the gated setup child. No consumed input is retried.

Compose a foreground result from two independent sources: durable application status/resource
closure, and the actual live request's presentation outcome. Live output uses DELIVERED only with
matching stream evidence and fully drained caller sinks. A failed sink remains a failed/incomplete
live output even if later durable polling succeeds. Apply `check()` only after this composition.
Never return the ordinary discarded-output wait result unchanged for a live-output request.

Extend exact-run disposal to this one fixed socket leaf, validating socket type, owner and identity
through the held run directory before unlink. Existing disposal rejects unknown names and assumes
regular files; do not broaden it to arbitrary special objects. Controller exit, workload boundary
emptiness, pipe EOF, relay custody and presentation completeness remain independent facts.

## Implementation and proof order

1. Add the protected primitive request metadata and canonical codec tests without changing ordinary
   request bytes, DB modes or public support claims.
2. Add the controller-owned endpoint, delayed gate admission, non-blocking flow and exact socket
   disposal. Exercise real local sockets/fork/exec with injected boundary/identity seams, including
   stop and lease expiry during stalls. These are not native root/cgroup proof.
3. Add the fixed bound relay, envelope and bundle; compose the host's one ordinary borrowed attempt
   after settled start/keeper admission. Reuse shared framing and endpoint mechanics without
   borrowing SSH-owned implementation.
4. Compose request policy, source/sink lifetime and truthful foreground results; keep finite
   `start`, independent refusal and later durable reads unchanged.
5. Run private reviews and whole regressions, then native Linux guest/systemd, privilege, SSH
   client/workstation and unsupported-channel proof. Test missing, partial and expired attachment;
   open/prefix input; half-close; large separate binary output; sink pressure/failure; lost replies;
   lease/stop during pressure; exact descendant cleanup; no output artifacts; and
   endpoint/descriptor cleanup.

The implementation remains transport-owned. SSH still supplies only delivery, connection/trust and
its native terminal relay. Terminal launch/attach, active durable capture reads, production
staging/spooling and complete RunContext retain their existing gates; this design does not replace
them with the pipe subset.
