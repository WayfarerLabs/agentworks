# Carrier I/O: Implementation Candidate

- Status: Byte-endpoint candidate implemented privately; joint SSH acceptance and terminal interface
  remain open. No production enablement.
- Baseline: `cea5e852`, with the accepted buffered SSH/QGA proof and #830 design.
- Governing contract:
  [carrier input and stream ownership](execution-contract.md#input-and-stream-ownership).

## Keep the buffered boundary stable while proving extensions

`Carrier.execute(PreparedInvocation, io=CarrierIO, deadline=Deadline)` remains the only delivery
primitive. Keep the current finite input/capture/discard path working while proving the live and
terminal extensions. Transport owns the reusable local subprocess pump, input/output and report
types, shared parsing and acceptance vectors. SSH owns its client environment policy, delivery
interpretation, call-site adaptation and borrowed-input terminal restoration. Neither lane silently
changes this boundary. Connection/trust and forwarding work need not wait for these extensions.

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
interruption no task still touches an endpoint. Live-source EOF closes only the carrier's outgoing
pipe; intentional guest-side early input closure retains the accepted proof's behavior.

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
a sink were captured or silently discarded. The byte-endpoint experiment implements `DELIVERED`; the
completion-frame grammar and joint SSH proof remain preparation acceptance gates. A sink failure
retains any independently observed completion plus incomplete output and `Failure.OUTPUT`; it cannot
become successful execution.

The raw sink is private adapter-author machinery. It is not a callback capability exposed through
RunContext file operations or a new plugin authority. The in-process trust limitation remains: a
malicious Python endpoint cannot be made non-blocking or secret-safe by its type annotation.

## Terminal choice stays distinct

The caller-facing terminal endpoint supplies explicit input and output. The proposed carrier shape
below pairs borrowed terminal input with trusted stream sinks; preparation selects terminal
presentation, not separate byte-exact guest stdout/stderr. There is no separate terminal field on
`PreparedInvocation` and no second stdin source. Joint proof must settle native handle admission and
console-mode restoration before enabling the mode; the byte protocol alone does not establish
terminal feasibility.

The [lifecycle design](execution-lifecycle-lld.md#lifecycle-waiting-and-attachment) owns public
start/attachment constraints. This extension adds neither an asynchronous pump handle nor a new
detached-job protocol.

### Proposed terminal input adapter

The next shared-type candidate uses the existing byte source for preparation, not a second carrier
parser or a callback that authorizes launch. Its proposed fields are:

```python
@dataclass(frozen=True)
class TerminalInput:
    input_fd: int
    term: str
    bootstrap: ByteSource
    sensitive: bool = False
```

`input_fd` is an explicitly borrowed Python file descriptor (a CRT descriptor on Windows), not a
process-global stdin lookup or an opaque Windows handle. `term` is the explicitly bound terminal
type. Construction does no descriptor or terminal I/O. Carrier admission verifies usable native
terminal input before dispatch; Windows and POSIX need their own implementations, not a claim that
non-blocking pipe support supplies terminal support.

This input requires trusted `SinkOutput` collectors. Preparation does not set `require_live` merely
because it selects a terminal: terminal and non-terminal live stdio are separate capabilities.
Terminal mode itself requires prompt delivery of setup and presentation bytes, not buffered
collection after exit. During preparation, the carrier reads only `bootstrap`: bytes are forwarded
unchanged, `None` withholds input, and `b""` permanently ends preparation and, after pending
bootstrap bytes have been written, permits keyboard forwarding from the borrowed descriptor. This
last transition is not EOF on SSH stdin. Preparation withholds payload until the remote
payload-ready acknowledgment, then withholds handoff until the interactive-ready acknowledgment.
Each acknowledgment is bound to the preparation-owned per-attempt nonce; the probe's static markers
are not a production protocol. SSH sees source behavior, never the acknowledgment grammar. Invalid
source results are input failures, not implicit handoff. The carrier never reads the bootstrap
source after handoff.

The carrier snapshots borrowed input, creates its owned input PTY, copies the original modes and
geometry to the slave, then makes borrowed input raw before launching its client. It propagates
resize and restores borrowed input on exit. It must not consume keyboard bytes before handoff or
flush queued input during acquisition or restoration. This does not reverse processing that happened
before acquisition or promise an unbounded terminal input queue. Keyboard EOF is distinct from
bootstrap EOF; its delivery and failure behavior still need proof against the actual client and
console mechanisms. Normal client exit after handoff stops keyboard borrowing without requiring
keyboard EOF or reporting unsent finite input. A raw Ctrl-D remains an ordinary input byte for the
remote terminal to interpret; local hangup does not imply remote cancellation.

The proposed responsibility split is:

| Owner                       | Responsibility                                                                                      |
| --------------------------- | --------------------------------------------------------------------------------------------------- |
| Shared preparation          | Parse readiness, release bootstrap bytes and handoff, filter raw streams, then select presentation. |
| SSH/native terminal adapter | Borrow input, own client-side terminal plumbing, propagate resize and restore input modes/flags.    |
| Shared presentation adapter | Borrow output, deliver explicitly selected display bytes and own emulator sanitation.               |

One caller-facing endpoint supplies both explicit input and output; shared preparation binds both
adapters from that endpoint. These are trusted internal collectors, not arbitrary plugin sinks whose
destination the carrier must infer. Only input reaches the carrier as a native terminal descriptor;
output reaches it through the existing trusted sinks. The presentation adapter must itself satisfy
the bounded sink and restoration contract. This moves common display policy above the carrier
instead of requiring SSH to interpret presentation phases. Terminal stdout is a combined guest
presentation stream; separate client stderr remains raw carrier diagnostics, not a second byte-exact
guest stream. Sensitive preparation is never consent to show raw setup output. Explicit live
presentation follows FRD R4's separate selection.

The preparation collector handles split or coalesced acknowledgments without displaying them. At
interactive readiness it passes subsequent bytes from that same read to the selected presentation
sink, respecting short writes, then stops interpreting presentation bytes as readiness records. The
explicitly bound terminal type affects client PTY metadata, not the application's independently
composed environment. Admission validates the terminal type and supplied handles before dispatch.

This is a concrete proposal for the joint proof, not an implemented or frozen terminal type. Prove
native handle admission, two-gate ordering, short writes, early keyboard preservation, bounded
presentation, restoration failure reporting and interruption before enabling it. The shared report
must preserve known remote facts while exposing local cleanup uncertainty; terminal cleanup cannot
be inferred from a successful remote exit. Stop all relay/client activity before input restoration
and before the shared presentation adapter sanitizes the selected output emulator. Cleanup needs its
own bounded allowance after observation expiry. Do not silently reuse the ordinary pipe pump for
this mode: its stdin construction and unfinished-input rules do not model an interactive keyboard.

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
carrier owns local input-terminal handles, client invocation, resize and input restoration; it must
not interpret application frames. The experiment must cover the remote bootstrap and the local
OpenSSH endpoint separately. Replacing OpenSSH stdin with a pipe is not a complete terminal adapter:
OpenSSH 8.5 reads terminal setup and window dimensions from its input descriptor. An owned local
PTY/console relay is one candidate, not an accepted dependency. No terminal type is frozen until
that ownership and supported-platform behavior are proved with the SSH lane.

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
