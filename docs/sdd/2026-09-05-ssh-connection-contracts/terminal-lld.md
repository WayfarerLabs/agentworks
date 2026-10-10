# SSH Terminal Delivery: Feasibility and Joint Interface

- Status: SSH-owned implementation candidate; shared input/preparation and private POSIX relay /
  Windows caller resource implemented, full composition and native acceptance open
- Inspected SSH code: `678e487d`; transport bootstrap probe: `a885ef5a`, 2026-09-20
- Requirements: [FRD R4/R5](frd.md#r4-one-attempt-byte-safe-io-and-truthful-evidence)
- Shared boundary:
  [input and stream ownership](../2026-09-12-transport-improv/execution-contract.md#input-and-stream-ownership)

## Preparation must remain deliverable

At the initial feasibility baseline, buffered `execution/preparation.py:113-118` placed the
invocation envelope in finite carrier stdin. It contains arguments, environment, script source and
application input. Replacing that input with a borrowed terminal would discard the envelope. Sending
it unmodified through an echoing remote PTY could expose sensitive bytes and change their
interpretation. This is a source analysis of the unimplemented combination, not a reported secret
disclosure by a supported mode.

Transport's #833 carrier I/O and preparation candidates leave terminal integration at a separate
gate. The joint design must settle how prepared payload reaches its consumer before interactive
input, what changes terminal modes and when, and how sensitive control/payload bytes stay out of
terminal presentation. SSH does not add a second envelope, put payload in client options or silently
substitute finite input for the terminal. A terminal-compatible prepared invocation and concrete
shared endpoint/report types precede feature enablement.

## Explicit ownership and POSIX candidate

The endpoint must identify the supplied native input/output handles and grant exclusive use for the
call. No process-global stdin/stdout lookup belongs in the carrier. Admission verifies usable
terminal handles, the agreed pairing and presentation policy, and the ordinary SSH connection/trust
policy before dispatch. The caller retains handle lifetime; the carrier never closes those handles.
Terminal presentation combines output rather than claiming separate byte-exact guest streams. The
[implementation evidence](terminal-results.md) records the current shared terminal input and
preparation, retained POSIX relay, Windows caller resource and their exact validation limits. Those
increments leave the public terminal feature disabled until full composition and native acceptance.

The POSIX resource layer treats input as readable native terminal input and output as an explicit
geometry source. Geometry queries do not require write access; presentation uses the supplied sink.
Preparation grants the endpoint pairing. No same-device check is inferred from descriptor device
numbers, which cannot establish identity across `/dev/tty` aliases and PTY masters.

For POSIX, snapshot the supplied terminal, then copy its original modes and geometry to an owned PTY
slave. Put the supplied terminal into raw mode before launching SSH and let the preparation source
withhold its keyboard bytes until the remote interactive handoff. SSH receives the owned slave as
stdin and owned pipes for stdout/stderr. The early-input experiment below explains why delaying
local raw mode until the handoff changes keystroke meaning.

OpenSSH changes its owned stdin terminal's settings and normally restores with `TCSADRAIN`, as shown
in the installed client family's
[OpenSSH 9.2 source](https://github.com/openssh/openssh-portable/blob/V_9_2_P1/sshtty.c#L57). A
killed client cannot perform that restoration. The parent separately owns restoration of the
borrowed terminal after ending the relay and bounded client cleanup on every exit path, including
interruption. It must preserve the original control-flow exception and make cleanup/restoration
uncertainty observable. If a relay task may still touch the borrowed terminal, the operation cannot
claim the snapshot is stably restored.

A dedicated client session is a candidate for local process ownership without changing the caller's
foreground process group. It needs an explicit resize mechanism: poll the supplied endpoint's window
dimensions and notify the owned client, or agree an equivalent bounded owner-supplied notification.
Do not replace a process-global signal handler as an incidental library side effect. OpenSSH's
[client loop](https://github.com/openssh/openssh-portable/blob/V_9_2_P1/clientloop.c#L203) handles
resize signals, but actual SSH propagation and supported-platform behavior remain proof obligations.
No global resize registry is proposed.

Preserve borrowed file-status and inheritable flags. `dup` shares file-status flags with its source;
changing a duplicate to non-blocking does not isolate that change. The terminal design must
establish bounded I/O deliberately rather than repurpose the byte pump's owned-pipe setup on
borrowed handles. Snapshot restoration with `TCSANOW` avoids requesting output drainage or
queued-input flushing; input discard, if ever desired, requires an explicit agreed policy. Local
timings do not establish a deadline guarantee for arbitrary terminal/device syscalls.

## Kernel modes and emulator presentation

The
[shared ownership split](../2026-09-12-transport-improv/carrier-io-lld.md#private-terminal-input-adapter)
assigns emulator sanitation to the shared presentation adapter. SSH restores borrowed native input
and settles client/relay custody; it does not inject display-policy bytes into a readiness
collector. At transport `d9315847`, collector `finish()` only finalizes readiness evidence. The
shared execution wrapper and bounded native presentation cleanup remain implementation gates.

A `termios` or Windows console-mode snapshot does not capture the terminal emulator's alternate
screen, mouse reporting or other escape-sequence state. Emulator sanitation applies a selected
presentation policy; it is not restoration of a complete prior emulator snapshot. Its output must
have a bounded delivery mechanism and observable failure. Ordinary blocking writes or a silently
failed reset do not meet the carrier's cleanup contract.

The retained `terminal.guarded_terminal()` is unsuitable unchanged: it selects global stdio,
swallows restoration errors and writes presentation resets through ordinary streams. Its constants
may inform the selected cleanup policy after dependency review; do not copy its implicit ownership
or claim its best-effort behavior meets this contract.

Windows requires its own explicit native handle/mode design, interruption and child-cleanup proof.
The existing Windows anonymous-pipe support and passing trust CI are not evidence for console
handles, terminal emulators or ConPTY. Preserve the supported workstation scope; an unproved
mechanism stays an implementation gate rather than silently dropping Windows or macOS support.

## Initial local experiment

An isolated Linux PTY experiment used an owned `openpty` pair and synthetic child with explicitly
wired stdio and a dedicated session. The child set raw mode. The parent compared the entire
`termios` snapshot, file-status flags, inheritable flags, descriptor liveness and child status after
clean exit, handled SIGTERM, SIGKILL and a parent `KeyboardInterrupt`. Parent restoration used
`TCSANOW` after reaping. The lead independently repeated the four cases.

All four cases restored the exact snapshot and preserved borrowed descriptors/flags. SIGKILL and
parent interruption left raw mode behind until the parent restored it. Direct children were reaped,
and the process descriptor count returned from four to four. The lead observed cleanup of 1.65-4.58
ms and restoration of 0.003-0.014 ms; these measurements describe this fixture only. Changing size
from 31 by 97 to 42 by 113 was visible when the synthetic child read it after input and an explicit
`SIGWINCH`. That observation does not prove signal-driven SSH resize propagation. A separate
descriptor-duplication experiment observed shared blocking-mode changes.

The initial owned loopback sshd authenticated but could not establish a remote PTY in the sandbox:
its attempt to change the PTY ownership failed with `Invalid argument`. Explicit server PTY refusal
also made `ssh -tt` return 255 before command execution. No successful real SSH terminal session,
remote resize or SSH terminal-restoration acceptance was established. Fixture servers/clients were
reaped and temporary credentials/directories removed; no operator terminal or infrastructure was
used. Synthetic mechanics are feasibility evidence, not product acceptance. The later host
experiment below removed that sandbox limitation without changing host configuration.

## Real SSH client relay experiment

Transport's published
[bootstrap probe](https://github.com/WayfarerLabs/agentworks/blob/a885ef5af256782abb827d6165cefd72a74e4e58/cli/tests/execution/terminal_bootstrap_probe.py)
switches the remote terminal to raw/no-echo before accepting its bounded frame, then restores modes
before acknowledging interactive readiness. The SSH experiment used that probe unchanged. Its fixed
bootstrap code was the remote command; application code, binary source and environment traveled only
inside its frame. This is an experimental protocol, not an accepted production interface.

An isolated host run used OpenSSH 9.2p1 Debian-2+deb12u10 and Python 3.11.2 with fresh loopback sshd
credentials, strict known-host verification and simulated borrowed terminals. Three runs each
compared piped stdin, owned PTY stdin with clean exit, and owned PTY stdin with client SIGKILL. The
lead independently performed the third run. All nine host trials succeeded; the three earlier
sandbox trials failed PTY allocation. No operator terminal, SSH state, VM or cloud resource was
used.

For PTY input, the parent copied the supplied terminal's modes and geometry to a separate owned PTY
slave and connected only that slave to OpenSSH stdin. Stdout/stderr remained separate owned pipes,
so the preparation collector could inspect raw output before presentation. Payload was sent through
the owned master only after payload readiness; keyboard bytes were relayed only after interactive
readiness. Two runs used nondefault borrowed settings to distinguish preservation from defaults.

| Observation                                            | Piped SSH stdin  | Owned PTY as SSH stdin |
| ------------------------------------------------------ | ---------------- | ---------------------- |
| Binary source/environment hashes match; no canary echo | Yes              | Yes                    |
| Initial remote geometry                                | 0 by 0           | 31 by 97               |
| Borrowed echo disabled and interrupt byte `0x07`       | Lost to defaults | Preserved              |
| Remote size after explicit resize notification         | 0 by 0           | 42 by 113              |
| Keyboard carriage return becomes newline after handoff | Yes              | Yes                    |

Resize set the owned slave's dimensions and sent `SIGWINCH` to the exact client PID. The remote
handler observed the new geometry before the next keyboard write. Clean clients exited zero; killed
clients returned -9. Parent restoration preserved borrowed modes, file-status and inheritable flags,
and handle liveness. Descriptor counts returned from four to four; independently checked client,
server and remote-child PIDs and fixture directories were absent after cleanup.

This supports an owned stdin PTY as the POSIX candidate. The pipe shortcut loses facts the terminal
contract needs. OpenSSH 8.5 source explains the observations:
[session setup passes stdin](https://github.com/openssh/openssh-portable/blob/V_8_5_P1/ssh.c#L2051)
to the [PTY request](https://github.com/openssh/openssh-portable/blob/V_8_5_P1/clientloop.c#L2468),
and [raw-mode handling](https://github.com/openssh/openssh-portable/blob/V_8_5_P1/sshtty.c#L69) also
uses stdin. Runtime evidence here is from 9.2p1, not an 8.5 runtime test.

The shared input design must compose preparation-owned payload and keyboard sequencing with explicit
borrowed terminal handles. SSH owns its local PTY, resize relay and terminal restoration; it must
not parse readiness or application frames. Transport owns that parser and the concrete shared shape.
The initial comparison changed borrowed modes only after readiness and did not exercise early
keyboard input. The follow-up below corrects that candidate timing.

## Early keyboard input

Two further real SSH trials queued identical keystrokes before sending the preparation frame. Both
withheld keyboard forwarding until interactive readiness. The borrowed terminal used canonical
input, carriage-return translation, interrupt byte `0x07` and literal-next byte `0x16`.

| Borrowed terminal timing | Queued keys | Bytes relayed after readiness | Remote observation                 |
| ------------------------ | ----------- | ----------------------------- | ---------------------------------- |
| Raw before launch        | `16 07 0d`  | `16 07 0d`                    | Reads literal `07 0a`, exits zero  |
| Cooked until readiness   | `16 07 0d`  | `07 0a`                       | Interrupt handler fires, exits 130 |

In the second case, the local terminal consumed literal-next and translated the carriage return. The
remote terminal then interpreted the already-processed interrupt byte. Early raw mode preserves the
original keys for one remote interpretation; waiting to forward alone does not preserve them. Both
trials restored borrowed modes, flags, inheritable state and liveness, returned descriptor counts
from four to four, and removed observed processes and fixture directories.

These cases cover keys arriving after terminal acquisition. Existing queued bytes may already have
been transformed before that boundary; no input flushing or promise to reverse prior processing is
selected here. Production acquisition/queued-input policy, control-flow interruption and supported
platform behavior still need joint acceptance.

## Published preparation joint proof, 2026-09-20

Transport `84ac8caf` now supplies the private `prepare_terminal_handoff()` implementation in
`execution/_terminal_handoff.py` and its Linux guest bootstrap. Two local real-SSH runs, including
an independent lead repetition, composed these unchanged modules at combined tree `704d658a` with
the owned stdin PTY candidate. The environment was Linux 6.1.0-52-arm64, Python 3.11.2 and OpenSSH
9.2p1 Debian-2+deb12u10. Fresh loopback credentials and owned PTYs replaced operator SSH state and
terminals; no VM or cloud resource was used.

The experimental relay polled the preparation source, drained each returned chunk, and treated its
eventual EOF as the permanent keyboard handoff. It did not parse readiness records. Each run sent
the bootstrap in 1,003 writes of at most five bytes. Only after EOF with no pending payload did it
read the early keys `16 07 0d`; the remote terminal interpreted them once as `07 0a`. The supplied
preparation sink filtered readiness and delivered presentation through three deliberate stalls and
45 writes of at most five bytes.

The remote application verified a 2,077-byte binary source and binary environment value by SHA-256.
Distinct payload canaries were absent from fixed argv, observed raw stdout/stderr, presentation and
retained result JSON. The original modes and 31 by 97 geometry reached the remote terminal, and an
explicit size update plus `SIGWINCH` to the owned client propagated 42 by 113. Both clients exited
zero. Exact borrowed modes, file-status and inheritable flags, and handle liveness were preserved;
descriptor counts returned from four to four. Independent checks found no recorded server, client or
remote-child PID and no fixture directory after cleanup.

The reproducible scratch probe is `terminal_handoff_joint_probe.py`, SHA-256
`9024dc082349eed7bdf0d7a601859d90b94a220374e2e71d2dc994fda5cdc622`, in the session-owned terminal
handoff proof worktree. This establishes Linux clean-exit composition of the published preparation,
not a production terminal carrier or shared-type acceptance. Native workstations, interruption,
restoration failure, emulator sanitation and production completion reporting remain open. The
restricted namespace could not complete server PTY setup; both successful runs used the approved
host fixture without changing host configuration.

## POSIX resource implementation

The private `_terminal_posix.PosixTerminal` implements native descriptor admission, owned PTY setup,
mode copying, raw borrowed input, output-driven geometry refresh and one-shot resource release. It
starts no process or relay and introduces no substitute shared terminal type. One retained non-main
worker owns the complete native lifetime. Acquisition refuses main-thread use, and resize/release
require the original `Thread` object before effects. Release requires all descriptor users to have
stopped, restores with `TCSANOW` and returns native cleanup-call failures, including control-flow
exceptions. Acquisition preserves the primary exception and prior cause/context when cleanup also
fails. An uncertain close is never retried against a possibly reused descriptor number.

The isolated implementation at `2307b75ed41b36db4b28a4b4537fb4b6cee9e847` passes 28 local Linux PTY
cases and the non-integration SSH suite (299 passed, 5 skipped), plus Ruff, format, relevant mypy
and file quality checks. These cases cover original nondefault modes, geometry from a distinct
read-only endpoint, queued input, borrowed flags/lifetime, resize, admission refusal, interrupted
effects and restoration/close failure. They establish resource behavior after known acquisition, not
actual client delivery or native macOS/Windows acceptance.

Private project/correctness review then reproduced main-thread SIGINT gaps between release
bookkeeping and restoration/close. Together with the identified allocation-custody boundary, this
motivated the retained-worker correction at `c02faf60e884292ac7b782eccc5a43bafcf6afb7`, which
excludes normal Python main-thread signal delivery from those transitions. Its 31 resource cases
include main-thread acquisition refusal, wrong-owner resize/release refusal and an owned subprocess
that interrupts its main thread while the retained worker remains able to restore modes and close
descriptors. The non-integration SSH selection,
`pytest tests/execution/carriers/ssh -m 'not integration'`, passes 302 cases with 5 skips. Ruff,
format, relevant mypy and file checks pass at that isolated correction.

Production composition must retain the native worker before acquisition and through interrupted
caller waits, client launch and cleanup settlement. Moving acquisition into an anonymous thread does
not establish that custody. Asynchronous thread injection, fatal signals and allocation exhaustion
are outside this interruption guarantee. Transport `d9315847` now supplies borrowed native stdin on
the existing shared owner; the [implementation record](terminal-results.md) scopes its adoption.
Owner-mediated resize, actual relay, cleanup reduction, presentation sanitation and native
acceptance remain open.

Use explicit worker completion facts when a caller wait is interrupted. The current signal case
interrupts before joining and leaves interrupted-wait settlement unproved. The test helper retains
its fixtures until worker settlement; an owning test-process/job timeout supplies the hang bound.

The retained POSIX consumer binds the exact shared process owner, passive terminal and cleanup
coordinator before thread admission. Its original worker acquires, pumps, requests bounded native
cleanup and restores modes. Finite caller return preserves pending native and terminal resources in
the same shared custody; later finite close requests reach that worker. Cancellation and possible
native admission share a guard, so a possible late native effect cannot follow a NOT_SENT result.
Borrowed terminal descriptors remain valid through settlement, including pending acquisition or
construction after caller return. Pump borrowing stops before native cleanup. A permanent native
loss finishes the worker without terminal restoration, retaining unsettled custody and refusing
reuse or unsafe cleanup retries. Pending construction and retryable cleanup are observed at the
existing bounded poll interval. A fresh caller request authorizes another cleanup attempt; observed
natural native settlement also permits restoration without another close or signal. Keep the
caller's original result unchanged.

Preserve a non-daemon terminal worker. Owned subprocess measurements show finite construction and
retryable-cleanup returns can still hold ordinary interpreter shutdown open until the retained work
settles. The finite API contract does not establish bounded interpreter shutdown. Production
composition must prove its caller's retained-custody lifecycle and interrupted exit behavior before
activation; this correction adds no global exit handler or daemon abandonment policy.

The round 1 private relay's progress/stall drain accounting regresses finite output: an exited
client's already-written backlog can be discarded while a responsive sink accepts partial writes.
The [round 2 finding](phase2-results.md#round-2-collected-feedback-and-dependency-adoption)
supersedes the prior acceptance of that consequence. Truthful incomplete reporting does not satisfy
finite backlog preservation. Full terminal delivery remains disabled pending retained cleanup
composition and native acceptance.

The selected correction fixes collection at 100 ms after the first observed natural client exit,
independently of sink progress. At that cutoff, observe each unfinished pipe's queued byte count
once and retain that finite prefix separately from already-collected pending bytes. Deliver pending
and the frozen prefix under the original operation deadline; never renew the quota or infer pipe
capacity. Quota exhaustion does not prove EOF. One final nonblocking one-byte probe may establish
EOF; a returned byte is retained and delivered before reporting incomplete output, and no further
collection occurs. An unavailable native count preserves already-collected pending bytes under the
original deadline, disables further reads on that pipe and reports incomplete output. The other
stream can still drain its known prefix. Separate stream observations do not imply an atomic pair.
Owned Linux implementation fixtures now establish finite and enlarged-pipe preservation, inherited
writers, query failure and deadline/control behavior. Native macOS counting and installed-client
acceptance remain required.

## Windows caller resource and record feasibility

The private `WindowsTerminal` resource borrows explicit input/output descriptors through one
retained non-main worker. Read-only native queries establish console-input kind and output viewport
geometry. Acquisition snapshots input mode and establishes raw VT input before client launch;
release restores that exact mode once and retains cleanup uncertainty. It neither changes output
mode/code pages nor closes caller handles. Every borrower must stop before release, and resource
access remains confined to the acquiring worker.

The [native primitive record](terminal-results.md#native-owned-console-primitive-acceptance) now
establishes resource admission/restoration and `ReadConsoleInputExW` record polling with
`CONSOLE_READ_NOWAIT` on a fresh owned Windows Server 2025 console. Injected records preserve
fixed-width UTF-16 units under original and custom settings. This does not select a production
keyboard decoder: physical key translation, partial surrogate/repeat state and numeric keypad input
using Alt under custom code pages still need applicable source and native evidence. The carrier must
not change borrowed code pages or add a blocked reader thread to hide those semantics. Windows
Server 2022 and the transport-owned child-launch/preparation boundary remain separate acceptance
gates. Public terminal delivery remains disabled.

The explicitly selected Windows integration comparison fixture reads byte-identical vectors
separately through record polling and `ReadConsoleW`, across distinct input/output code pages and VT
input states. Ordinary CI retains resource/polling coverage and excludes this research fixture. The
[pinned Microsoft host source](https://github.com/microsoft/terminal/blob/dc8ae365847d41f62d1ebca93815bb00959bc12f/src/host/stream.cpp#L170-L198)
handles private-flagged Alt key releases through packed-byte conversion; its
[conversion boundary](https://github.com/microsoft/terminal/blob/dc8ae365847d41f62d1ebca93815bb00959bc12f/src/host/misc.cpp#L19-L28)
uses the output code page and requires an applicable lead byte for two-byte input. The fixture
restricts its packed two-byte vector to code page 932 and records native conversion references for
both pages. An ordinary character sentinel supplies queued input when a release is ignored, while
the retained parent timeout remains the backstop for a blocked fixture read. Passing comparison data
requires the scoped `-rP` report; ordinary captured CI success alone does not establish those
values. Native comparison values remain unobserved, and injected records cannot prove physical
numeric keypad input or the behavior of a different Windows binary. This selects no production
decoder.

## Remaining proof

The host experiment used bounded small writes and captured output, not a production relay. The
integrated proof still needs supported workstations and the actual shared API; clean exit, deadline,
killed client and parent interruption; resize; closed/invalid handles; blocked presentation and
restoration failure; unchanged borrowed resources; sensitive preparation without reflection; and
correct combined-output/completion reporting. Native Windows/macOS and emulator observations require
the operator's integration beds. These gates remain in
[Phase 2](plan.md#phase-2-full-ssh-implementation-in-the-second-pr).
