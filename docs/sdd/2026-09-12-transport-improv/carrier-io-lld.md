# Carrier I/O: Implementation Candidate

- Status: Transport-owned candidate for joint SSH review and proof, not an implemented interface.
- Baseline: `cea5e852`, with the accepted buffered SSH/QGA proof and #830 design.
- Governing contract:
  [carrier input and stream ownership](execution-contract.md#input-and-stream-ownership).

## Keep the buffered boundary stable while proving extensions

`Carrier.execute(PreparedInvocation, io=CarrierIO, deadline=Deadline)` remains the only delivery
primitive. Keep the current finite input/capture/discard path working while proving the live and
terminal extensions. Transport owns the reusable local subprocess pump, input/output and report
types, shared parsing and acceptance vectors. SSH owns its client environment policy, delivery
interpretation, call-site adaptation and terminal restoration. Neither lane silently changes this
boundary. Connection/trust and forwarding work need not wait for these extensions.

This document selects a candidate for an experiment, not permission to advertise live I/O or enable
production execution. Any change to the types in `carrier.py` is coordinated with the SSH owner
before integration. The existing proof's sensitive-output suppression remains intact until the
replacement mechanism passes its reflection tests.

## Bounded borrowed byte endpoints

The candidate live byte interfaces have one operation each:

```python
class ByteSource(Protocol):
    def try_read(self, limit: int) -> bytes | None: ...

class ByteSink(Protocol):
    def try_write(self, data: memoryview) -> int | None: ...
```

These operations must return without waiting on external I/O. `None` means no progress is currently
possible; `b""` means source EOF. A read returns at most `limit` bytes. A write acknowledges between
one and the supplied byte count; the carrier retains the unwritten suffix. Exceptions, invalid
counts or invalid result types are local input/output failures, not successful EOF or discard.

The carrier fairly alternates bounded source reads, pipe writes and output draining under the same
deadline. It does not wrap an arbitrary `BinaryIO.read` or callback in a thread it cannot cancel. It
owns its bounded pending buffers and subprocess pipes, never the supplied endpoints. On return or
interruption no task still touches an endpoint. Source EOF closes only the carrier's outgoing pipe;
intentional guest-side early input closure retains the accepted proof's behavior.

Concrete core endpoint adapters, not arbitrary blocking file objects, establish this contract for
CLI streams. Their owning composition is responsible for exclusive use and any mode restoration. The
carrier cannot change a borrowed descriptor's blocking mode as an incidental side effect. Python's
Windows support for `os.set_blocking` is limited to pipes; it is not proof that console or file
handles support the same mechanism. The
[Python reference](https://docs.python.org/3.12/library/os.html#os.set_blocking) and
[Windows I/O cancellation rules](https://learn.microsoft.com/en-us/windows/win32/fileio/synchronous-and-asynchronous-i-o)
are constraints for the endpoint proof, not an assertion that cancellation or terminal support is
already solved. A core adapter that cannot establish bounded use must refuse before dispatch.

## Transient collection and sensitive control evidence

A carrier stream sink receives raw carrier bytes, not supposedly separated application streams.
Shared preparation supplies the bounded incremental decoder. Caller-facing streams receive only
decoded application bytes; raw mixed carrier stderr does not become application stderr. Parsing and
report conversion remain above the carrier.

This is also the candidate for the sensitive completion-evidence gap. The existing buffered proof
suppresses all retained carrier bytes, so a new bootstrap completion marker alone cannot establish a
public result. A trusted transport collector must consume transient carrier output, retain only
validated control fields when output is sensitive/discarded, and discard unknown bytes without
including them in errors, exception chains or logs. Arbitrary account hooks may reflect input before
the bootstrap; switching ordinary capture back on is not an acceptable solution.

The candidate output choice is `SinkOutput(stdout: ByteSink, stderr: ByteSink, require_live=False)`,
alongside `Capture` and `Discard`, all within `CarrierIO`. The destination and its delivery timing
are different facts: a buffered QGA response can feed the common collector without advertising live
stdio. `require_live=True` demands the channel's live feature and refuses before dispatch when it is
absent. Public direct-streaming requests set it; internal control collection does not.

Sensitivity still prohibits retained raw carrier bytes. An explicit public live-presentation request
is a separate preparation decision; a control collector is not consent to show secret-bearing
output. Reports need an explicit delivered retention value rather than claiming that bytes passed to
a sink were captured or silently discarded. The exact report amendment and completion-frame grammar
are settled with the preparation LLD and SSH proof before implementation. A sink failure retains any
independently observed completion plus incomplete output and `Failure.OUTPUT`; it cannot become
successful execution.

The raw sink is private adapter-author machinery. It is not a callback capability exposed through
RunContext file operations or a new plugin authority. The in-process trust limitation remains: a
malicious Python endpoint cannot be made non-blocking or secret-safe by its type annotation.

## Terminal choice stays distinct

Terminal input selects one borrowed endpoint containing the terminal's input/output handles. Output
selects terminal presentation, not separate byte-exact stdout/stderr. There is no separate terminal
field on `PreparedInvocation` and no second stdin source. The terminal LLD must settle the concrete
handle shape and console-mode restoration with SSH's platform implementation before adding it to
`CarrierIO`; the byte protocol above is not proof of terminal feasibility.

The [lifecycle design](execution-lifecycle-lld.md#lifecycle-waiting-and-attachment) owns public
start/attachment constraints. This extension adds neither an asynchronous pump handle nor a new
detached-job protocol.

### Same-terminal preparation experiment

The next proof starts with a single terminal session, not mandatory remote temporary-file staging.
The proposed sequence is remote terminal setup with echo and input transformations disabled, an
acknowledgment that the payload channel is ready, an exact bounded payload read, restoration of the
application terminal settings, and a separate acknowledgment permitting interactive input. The
sender must not release payload before the first acknowledgment or user input before the second.
Payload completion is length-delimited, not EOF, and the reader must not consume bytes belonging to
interactive use. Source and application input remain separate. These are experimental acceptance
conditions, not a production wire format or a claimed implementation.

Shared preparation owns the handshake, framing and transition to application presentation. The SSH
carrier owns local terminal handles, client invocation, resize and restoration; it must not
interpret application frames. The experiment must cover the remote bootstrap and the local OpenSSH
endpoint separately. Replacing OpenSSH stdin with a pipe is not a complete terminal adapter: OpenSSH
8.5 reads terminal setup and window dimensions from its input descriptor. An owned local PTY/console
relay is one candidate, not an accepted dependency. No terminal type is frozen until that ownership
and supported-platform behavior are proved with the SSH lane.

The [prior-art investigation](prior-art-research.md#terminal-bootstrap-prior-art) records the
relevant implementations and their limitations. A local synthetic PTY proves only that fixture; it
does not establish actual SSH delivery, native Windows/macOS behavior or application-start evidence.
The buffered and live-byte increments can proceed independently of this terminal gate.

## Joint proof and disposition

Before integrating these extensions, demonstrate each applicable case on supported workstation
platforms, with fake endpoint failures supplementing actual pipe/console evidence:

- Byte-exact duplex traffic with bounded buffers, source EOF, short writes and output flow control.
- A stalled source or sink respects the operation deadline without a surviving pump or closed
  borrowed endpoint; an endpoint contract violation is reported honestly, not claimed cancelled.
- Source/sink faults and interruption preserve known completion but never manufacture success, guest
  termination or replay. Terminal modes are restored on each terminal exit path.
- Ordinary output is separately decoded; sensitive/reflected output leaves no canary in retained raw
  bytes, results, exceptions or artifacts, while valid control evidence can establish a result.
- Buffered QGA can feed the same bounded collector after an observation without advertising live
  stdio; provider truncation or a malformed/incomplete frame remains explicit uncertainty.
- Existing finite-input proof cases still pass with unchanged carrier dispatch and trust behavior.

The transport lead accepts the shared shape and records the observed pins; SSH supplies delivery and
endpoint feasibility evidence. Missing console or cancellation mechanics are unresolved proof work,
not a reason to narrow the supported platform matrix or silently accept a blocking endpoint.
