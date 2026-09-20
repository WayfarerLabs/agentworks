# Buffered SSH Carrier

Status: Buffered PoC merged; full implementation is Phase 2, final old SSH deletion is Phase 3.

## Boundary

`agentworks.execution.carriers.ssh.SSHCarrier` consumes the actual transport-owned types in
`execution/carrier.py`. The current transport dependency is pinned once in the
[proof record](poc-results.md#revisions-and-delivery). Its
[proof LLD](../2026-09-12-transport-improv/proof-lld.md) describes the candidate buffered subset and
the shared preparation experiment. This SSH implementation adds no contract types, framing,
application-shell policy or outcome decoder. Production composition remains Phase 2.

`SSHConnection` is immutable resolved input. Construction and `features` perform no I/O. `execute`
validates explicit files, checks the selected installed executable, then makes at most one command
attempt. No failed observation or uncertain dispatch causes replay.

## Connection policy

The proof takes a literal host, port, POSIX account, native absolute identity and known-host paths,
optional trust lookup alias, optional revocation file, optional explicit Unix-domain agent socket,
and executable selection. Existing fixture trust is mandatory and strict. OpenSSH owns file parsing
and permission enforcement; local checks establish availability, not atomic ownership of later
opens. The carrier never discovers credentials, enrolls a host, or writes trust.

Paths containing expansion tokens, control characters or ambiguous ssh_config quoting are refused.
Spaces are supported with explicit option-value quoting. Native Windows paths are serialized with
forward slashes. Windows named-pipe agent selection is outside this candidate; default agent use is
disabled everywhere. These are proof input limits, not completed production migration policy.

Every command uses `-F none`, disables PTY allocation, ambient agent selection, sibling certificate
discovery, password, interactive and host-based authentication, PKCS11/security-key provider
discovery, proxying, forwarding, multiplexing and local commands. Only the configured identity is
offered. Known-host lookup uses the supplied store and optional alias; global stores, DNS
verification and host-key updates are disabled. Installed OpenSSH chooses algorithms. There is no
arbitrary option dictionary or weaker retry path.

At the minimum client version, `CertificateFile=none` still loads a literal filename. An explicit
certificate entry suppresses sibling discovery, so the builder points it beneath the validated
regular identity file, where both the certificate and its `.pub` fallback fail as non-directory
lookups. This needs no temporary artifact. The rationale follows the
[OpenSSH 8.5 loading path](https://github.com/openssh/openssh-portable/blob/V_8_5_P1/ssh.c#L2280-L2320)
and its
[public-file fallback](https://github.com/openssh/openssh-portable/blob/V_8_5_P1/authfile.c#L263).
This is deliberate refusal of certificate authentication in the proof.

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
environment policy and output provenance to transport's `carriers/_subprocess.py`, which owns the
subprocess and its pipes. EOF input uses the null device; finite bytes use a pipe. Its single-thread
pump performs bounded, fair reads and short writes on non-blocking pipes, closing stdin once all
bytes are sent. Python 3.12 is the project minimum and adds Windows pipe support to
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
and sensitive suppression drain without retaining payload. Raw stdout has carrier provenance; stderr
remains mixed client/remote evidence. Only transport's decoder can recover the separate guest
streams from its shared framing. Errors retain closed failure codes rather than exception text,
commands or payload-bearing diagnostics.

One deadline covers local preparation, client startup and I/O. Expiry stops local observation and
closes owned handles. Local kill/reap has an additional bounded 0.5-second cleanup allowance; it
does not renew execution or establish guest cancellation. Interruption inside the guarded I/O loop
propagates after cleanup. After observed client exit, drainage has at most 0.1 seconds and still
requires actual EOF for completeness. Inherited descendant handles cannot cause an unbounded drain.

Launch interruption remains an acceptance gap. The cleanup guard begins after process construction
and loop-state initialization; interruption earlier can leave a child alive, even before Python
returns its handle. Transport's
[startup evidence](../2026-09-12-transport-improv/prior-art-research.md#local-process-startup-and-interruption)
records the real Linux reproduction and unresolved cross-platform ownership mechanism. The former
SSH-local pump had the same gap. Shared extraction does not close it, and loop-interruption tests do
not establish production launch-interruption conformance. Forwarding's separate process launch also
needs that ownership proof.

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

## Shared I/O integration checkpoint, 2026-09-19

Transport implementation [PR #833](https://github.com/WayfarerLabs/agentworks/pull/833) at
`e85e9f5c4752ae926315fa7c0e69b871de42e4fc` supplies the shared finite-input subprocess pump and
canonical invocation models. SSH adopts that pump while retaining environment filtering and
carrier-specific evidence, and imports `Command` from `execution.models` in its independence
fixture. This is an actual implementation dependency; #832 stacks on #833.

The carrier interface remains buffered-only. Its I/O LLD is a candidate, not concrete live/terminal
types. Exact endpoint and report types remain transport's to supply and accept through joint proof.
SSH does not create substitute common types while waiting. The operator confirmed that terminal/PTY
work is proceeding in parallel with #833; the SSH terminal notes are input to that joint work.

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
quoting and shared framing. Installed-client tests check option interpretation offline and
pre-authentication rejection against an owned local peer. A fresh isolated Python process blocks
transport's retirement modules while executing the new binding. None of those tests establishes live
authentication or platform coverage.

The operator's integration tester combines pinned transport and SSH branches locally, records both
inputs and the integrated revision, and exercises the shared acceptance cases using their authorized
resources. Contract-changing conflicts return to transport and the operator. No design-only main
merge is needed to perform that work. The [proof record](poc-results.md) separates local results,
measured live cells and carried-forward evidence. Transport's
[acceptance disposition](../2026-09-12-transport-improv/proof-lld.md#joint-buffered-proof-acceptance-2026-09-17)
accepts the buffered boundary and maps its authoritative matrix to those results.

Full live-stream and terminal ownership, forwarding, production configuration/trust conversion,
provider-inner policy and complete workstation/platform evidence remain tracked in
[Phase 2](plan.md#phase-2-full-ssh-implementation-in-the-second-pr). Transport then leads migration,
legacy RunContext removal and old transport deletion while SSH waits and addresses issues. Final old
SSH deletion waits for a later operator request in
[Phase 3](plan.md#phase-3-remove-old-ssh-on-the-operators-later-request); this SDD remains unlocked
until that retirement is accepted. The joint PoC acceptance matrix still belongs to transport; an
unimplemented candidate mode or unmeasured prerequisite cannot be silently counted as a passed proof
case.
