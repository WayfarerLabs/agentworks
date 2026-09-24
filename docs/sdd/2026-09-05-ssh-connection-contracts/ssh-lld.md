# Independent SSH Carrier

Status: Buffered PoC merged. Phase 2 has independent trust maintenance, live byte delivery and owned
forwarding; terminal delivery and production composition remain open. Final old SSH deletion is
Phase 3.

## Boundary

`agentworks.execution.carriers.ssh.SSHCarrier` consumes the actual transport-owned types in
`execution/carrier.py`. The merged PoC dependency is pinned in the
[proof record](poc-results.md#revisions-and-delivery); subsequent implementation dependencies and
their evidence are pinned in the [Phase 2 record](phase2-results.md). Transport owns the carrier
contract, shared preparation and process lifecycle. This SSH implementation adds no contract types,
framing, application-shell policy or outcome decoder. Production composition remains Phase 2.

`SSHConnection` is immutable resolved input. Construction and `features` perform no I/O. `execute`
validates explicit files, checks the selected installed executable, then makes at most one command
attempt. No failed observation or uncertain dispatch causes replay.

## Connection policy

The carrier takes a literal host, port, POSIX account, native absolute identity and known-host
paths, optional trust lookup alias, optional revocation file, optional explicit Unix-domain agent
socket, and executable selection. Trust is mandatory and strict for existing targets, whether
supplied as owned files or admitted from a managed bundle. OpenSSH owns file parsing and permission
enforcement; local checks establish availability, not atomic ownership of later opens. The carrier
never discovers credentials, enrolls a host, or writes trust. Explicit trust import, refresh and
creation-only enrollment are separate maintenance operations described in the
[trust LLD](trust-lld.md) and [enrollment LLD](enrollment-lld.md); production creation-flow binding
remains open.

Paths containing expansion tokens, control characters or ambiguous ssh_config quoting are refused.
Spaces are supported with explicit option-value quoting. Native Windows paths are serialized with
forward slashes. Windows named-pipe agent selection is unsupported; default agent use is disabled
everywhere. The [configuration LLD](configuration-lld.md) and
[migration strategy](migration-strategy.md) record the policy conversion and production acceptance
that remain open.

The current command path uses `-F none` and disables PTY allocation, ambient agent selection,
sibling certificate discovery, password, interactive and host-based authentication,
PKCS11/security-key provider discovery, proxying, inherited forwarding, multiplexing and local
commands. Only the configured identity is offered. Known-host lookup uses the supplied store and
optional alias; global stores, DNS verification and host-key updates are disabled. Installed OpenSSH
chooses algorithms. There is no arbitrary option dictionary or weaker retry path.

At the minimum client version, `CertificateFile=none` still loads a literal filename. An explicit
certificate entry suppresses sibling discovery, so the builder points it beneath the validated
regular identity file, where both the certificate and its `.pub` fallback fail as non-directory
lookups. This needs no temporary artifact. The rationale follows the
[OpenSSH 8.5 loading path](https://github.com/openssh/openssh-portable/blob/V_8_5_P1/ssh.c#L2280-L2320)
and its
[public-file fallback](https://github.com/openssh/openssh-portable/blob/V_8_5_P1/authfile.c#L263).
This deliberately refuses certificate authentication in the current carrier.

The client floor is OpenSSH 8.5. A bounded `ssh -V` probe accepts the upstream and Windows version
forms on stderr; unknown, failed or older probes refuse before command dispatch. The probe and
command spend the same original monotonic deadline. An executable name uses the caller's process
search path; an explicit native executable path can pin selection. No executable is fetched or
installed.

Prepared argv is serialized into one remote command string, quoting every argument, including empty
arguments, command-position assignments and reserved words. Local spawning never uses a shell. A
compatible POSIX account shell on the destination remains a prerequisite. SSH cannot undo sshd hooks
or account-shell startup behavior. Transport's current Linux Bash/base64 preparation requires
neither guest Python nor filesystem staging. The shared bootstrap requires Bash 5.1 or newer;
measured images and outstanding locations are recorded in [poc-results.md](poc-results.md).

## Phase 2 identity validation

Operation-time checks now verify a private identity's sibling public file against the public
identity embedded in the configured key. OpenSSH tries the `.pub` companion before deriving that
identity; `IdentitiesOnly=yes` alone cannot prevent a stale companion from selecting another agent
key. A mismatch or unverifiable companion refuses before client dispatch. An explicitly configured
public identity is supported only without its own sibling. The lightweight public fingerprint reader
does not prove that OpenSSH will accept the complete direct encoding before suffix lookup; ambiguous
public-file/sibling pairs refuse rather than selecting another identity. The retained `ssh_identity`
leaf performs bounded public-only parsing without decrypting private material. These new checks need
their own authentication-offer evidence; the old PoC record is unchanged.

## Process and evidence

`client.py` owns version gating and mapping into the shared report. SSH's `_io.py` supplies client
environment policy and output provenance to transport's `carriers/_subprocess.py`, which adapts the
shared `_process.py` core. EOF input uses the null device; finite bytes and a borrowed `LiveInput`
source use an owned pipe. The single-thread pump performs bounded, fair reads and short writes on
non-blocking pipes, closing stdin after all finite bytes or observed source EOF. Python 3.12 is the
project minimum and adds Windows pipe support to
[`os.set_blocking`](https://docs.python.org/3.12/library/os.html#os.set_blocking).

Windows client spawning removes two OpenSSH-private variables from a copy of the child environment:
`OPENSSH_STDIO_MODE` and `c28fc6f98a2c44abbbd89d6a3037d0d9_POSIX_FD_STATE`. They describe inherited
OpenSSH handles; Python supplies new pipes with different semantics. The Windows implementation
itself warns about
[stale grandparent descriptor state](https://github.com/PowerShell/openssh-portable/blob/v9.5.0.0/contrib/win32/win32compat/w32fd.c#L115-L128).
The filter is case-insensitive, preserves ordinary variables and the parent environment, and applies
to both version probing and dispatch. POSIX spawning retains its normal inherited environment. An
installed-client Windows regression exercises clean and contaminated state against an owned local
peer. The later authenticated retest passed in both the original SSH-parent context and a clean
launch, as recorded in [poc-results.md](poc-results.md); that is separate evidence from the local
pre-authentication regression.

Capture is bounded separately per stream and overflow reports incomplete retained output. Discard
and sensitive suppression drain without retaining payload. `SinkOutput` instead delivers transient
bytes to trusted transport collectors and reports delivered retention with empty report data,
including for sensitive input. It does not itself authorize public presentation. Partial writes and
stalls retain a bounded pending suffix; supplied sources and sinks remain borrowed. Raw stdout has
carrier provenance; stderr remains mixed client/remote evidence. Only transport's decoder can
recover the separate guest streams from its shared framing. Errors retain closed failure codes
rather than exception text, commands or payload-bearing diagnostics.

One deadline covers local preparation, client startup and I/O. Expiry stops local observation and
closes owned handles. Local kill/reap has an additional bounded 0.5-second cleanup allowance; it
does not renew execution or establish guest cancellation. Interruption inside the guarded I/O loop
propagates after cleanup. After observed client exit, pipe collection has a 0.1-second budget;
pending sink delivery pauses that collection budget while the operation's original deadline still
applies. Completeness requires actual EOF and delivery of pending output. An explicitly unbounded
operation can therefore still wait on a stalled sink; inherited descendant pipe collection alone
cannot extend it indefinitely.

Transport's shared core now constructs the client in a private launch owner, publishing pipe and
status observations through a condition lock. The caller alone pumps borrowed byte endpoints; return
waits for the owner's terminal cleanup observation. This addresses the measured Linux interruption
during client construction without moving borrowed endpoint access to a background task. It does not
establish complete interruption safety: transport still records a reproduced asynchronous
interruption at cleanup-loop entry that can leave a child and pipes live. Its
[startup evidence](../2026-09-12-transport-improv/prior-art-research.md#local-process-startup-and-interruption)
and [lifecycle design](../2026-09-12-transport-improv/execution-lifecycle-lld.md) retain the current
limits. Concurrent external reaping can also make exact local ownership uncertain. Native platform
proof and correction of the cleanup gap remain acceptance work. Forwarding adopts the same
`LocalProcessOwner` with a separately gated drain worker; its
[ownership design](forwarding-lld.md#launch-ownership-integration) and measured evidence distinguish
this adoption from complete native acceptance.

Live measurements confirm that guest workloads and bootstrap descendants can survive local
observation expiry. The shared
[lifetime contract](../2026-09-12-transport-improv/execution-contract.md#carrier-contract) records
the accepted PoC limitation and the production cancellation gate. SSH does not add its own reaper,
cancellation protocol or retry. Bounded guest work may finish later; local return is not proof that
it did.

A natural local exit from 0 through 254 supplies the remote command evaluation status exposed by
OpenSSH, including destination account-shell startup. The account shell can refuse before the
bootstrap starts; this raw status alone proves neither bootstrap nor application execution. Captured
output requires transport's framing checks, and suppressed output supplies no such proof. Transport
owns public result interpretation and the stronger evidence it requires; SSH remains
framing-agnostic. Status 255, local signals and Windows native crash codes leave remote completion
unknown. Dispatch is unknown once a client starts without stronger channel evidence, and not-sent
only when local checks or spawning establish that no command process started. Output failure and
partial capture remain visible even when a raw remote status is known. See OpenSSH's
[exit-status definition](https://man.openbsd.org/ssh#EXIT_STATUS).

An induced trust or authentication refusal can be known to the tester while the carrier still sees
only status 255. The carrier cannot distinguish that status from lost observation using a stable
OpenSSH result field. It therefore preserves unknown dispatch and mixed stderr rather than parsing
diagnostic prose or adding another probe. Strict trust never becomes implicit enrollment. On
Windows, local status 1 after a deadline may be the result of killing the client; only the natural
status observed before cleanup can establish completion.

## Shared I/O integration checkpoint, 2026-09-20

Transport implementation [PR #833](https://github.com/WayfarerLabs/agentworks/pull/833) at
`84ac8cafee8c6ac97587bcc98e8785b9be62de8a` supplies the shared process core, live byte endpoints and
canonical invocation models. SSH adopts that core while retaining environment filtering and
carrier-specific evidence, and imports `Command` from `execution.models` in its independence
fixture. This is an actual implementation dependency; #832 stacks on #833.

The adapter accepts every current `CarrierIO` choice: EOF/finite/live byte input and
capture/discard/sink output, and advertises live stdio. The temporary buffered-mode guard is retired
after that adoption; `CarrierIO` validates its supported shapes at construction. New shared modes
still require coordinated implementation and proof at the shared boundary. Terminal support remains
disabled until its distinct handle and lifetime contract is implemented and proved.

The shared carrier interface now includes `LiveInput`, `SinkOutput` and delivered-output retention.
SSH adoption must prove the actual types and preserve raw stream provenance, sensitivity and
completion uncertainty. Transport's two-gate terminal preparation is implemented separately; the
terminal endpoint type remains a joint proof candidate. SSH does not create substitute common types.
The operator confirmed that terminal/PTY work is proceeding in parallel with #833; the SSH terminal
notes are input to that joint work.

The [terminal LLD](terminal-lld.md) records local feasibility evidence and the unresolved
prepared-input interface. Terminal integration additionally needs explicit borrowed handles and
restoration ownership. The retained `terminal.guarded_terminal()` acts on process-global
stdin/stdout and silently tolerates restoration failure, so it cannot be used unchanged as proof of
restoring an arbitrary borrowed endpoint. Keep terminal mechanics separate from byte-stream pumping
and resolve the native POSIX/Windows handle contract with transport before enabling the feature.
Existing terminal constants/utilities remain reusable only with their dependency closure and
behavior audited.

Additive RunContext composition, platform endpoint fields and the genuine creation-flow provenance
binding also remain transport-owned dependencies. Independent trust, identity and forwarding tests
do not prove those integration steps. The implementation stays draft until the agreed combined head
and supported-platform evidence satisfy the Phase 2 gate.

## Proof and remaining work

The [configuration LLD](configuration-lld.md) specifies additive settings and composition. The
[trust LLD](trust-lld.md) specifies Phase 2 import, refresh and operation admission. The
[forwarding LLD](forwarding-lld.md) defines positive listener setup and owned resource lifetime.
These are implementation designs, not evidence that those mechanisms are shipped or accepted.

The [SSH test handoff](../../../cli/tests/execution/carriers/ssh/README.md) gives local commands and
explicit live construction using the shared harness. Synthetic local executables test process I/O,
quoting and shared framing. Installed-client tests include authenticated Linux loopback live-byte
delivery, option interpretation and pre-authentication refusal. A fresh isolated Python process
blocks transport's retirement modules while executing the new binding. These local tests do not
establish supported workstation or provider coverage.

The operator's integration tester combines pinned transport and SSH branches locally, records both
inputs and the integrated revision, and exercises the shared acceptance cases using their authorized
resources. Contract-changing conflicts return to transport and the operator. No design-only main
merge is needed to perform that work. The [proof record](poc-results.md) separates local results,
measured live cells and carried-forward evidence. Transport's
[acceptance disposition](../2026-09-12-transport-improv/proof-lld.md#joint-buffered-proof-acceptance-2026-09-17)
accepts the buffered boundary and maps its authoritative matrix to those results.

Terminal ownership, production connection/trust composition, provider-inner policy and complete
workstation/platform evidence remain tracked in
[Phase 2](plan.md#phase-2-full-ssh-implementation-in-the-second-pr). Transport then leads migration,
legacy RunContext removal and old transport deletion while SSH waits and addresses issues. Final old
SSH deletion waits for a later operator request in
[Phase 3](plan.md#phase-3-remove-old-ssh-on-the-operators-later-request); this SDD remains unlocked
until that retirement is accepted. The joint PoC acceptance matrix still belongs to transport; an
unimplemented candidate mode or unmeasured prerequisite cannot be silently counted as a passed proof
case.
