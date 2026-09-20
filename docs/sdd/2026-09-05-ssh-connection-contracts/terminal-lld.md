# SSH Terminal Delivery: Feasibility and Joint Interface

- Status: SSH-owned implementation candidate; shared types and terminal preparation remain open
- Inspected SSH code: `678e487d`; transport bootstrap probe: `a885ef5a`, 2026-09-20
- Requirements: [FRD R4/R5](frd.md#r4-one-attempt-byte-safe-io-and-truthful-evidence)
- Shared boundary:
  [input and stream ownership](../2026-09-12-transport-improv/execution-contract.md#input-and-stream-ownership)

## Preparation must remain deliverable

The current buffered `execution/preparation.py:149-155` places the invocation envelope in finite
carrier stdin. It contains arguments, environment, script source and application input. Replacing
that input with a borrowed terminal would discard the envelope. Sending it unmodified through an
echoing remote PTY could expose sensitive bytes and change their interpretation. This is a source
analysis of the unimplemented combination, not a reported secret disclosure by a supported mode.

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
Terminal presentation combines output rather than claiming separate byte-exact guest streams.

For POSIX, snapshot the supplied input terminal before starting the owned client. OpenSSH changes
its stdin's terminal settings and normally restores with `TCSADRAIN`, as shown in the installed
client family's
[OpenSSH 9.2 source](https://github.com/openssh/openssh-portable/blob/V_9_2_P1/sshtty.c#L57). A
killed client cannot perform that restoration. The parent therefore retains the snapshot and
restores after bounded child cleanup on every exit path, including interruption. It must preserve
the original control-flow exception and make cleanup/restoration uncertainty observable. If an owned
child may still mutate the terminal, the operation cannot claim the snapshot is stably restored.

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
The experiment changed borrowed modes only after readiness and did not exercise early keyboard
input; that timing and queued-input preservation need joint proof before selecting the mechanism.

## Remaining proof

The host experiment used bounded small writes and captured output, not a production relay. The
integrated proof still needs supported workstations and the actual shared API; clean exit, deadline,
killed client and parent interruption; resize; closed/invalid handles; blocked presentation and
restoration failure; unchanged borrowed resources; sensitive preparation without reflection; and
correct combined-output/completion reporting. Native Windows/macOS and emulator observations require
the operator's integration beds. These gates remain in
[Phase 2](plan.md#phase-2-full-ssh-implementation-in-the-second-pr).
