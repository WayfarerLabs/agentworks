# Buffered SSH Carrier

Status: Phase 1 implementation; joint live proof and Phase 2 remain open.

## Boundary

`agentworks.execution.carriers.ssh.SSHCarrier` consumes the actual transport-owned types in
`execution/carrier.py`. The implementation dependency is transport PR #826 at
`2321c47fcc19cdfa7523f11af8bf667bd22a7590`. Its
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
forms; unknown, failed or older probes refuse before command dispatch. The probe and command spend
the same original monotonic deadline. An executable name uses the caller's process search path; an
explicit native executable path can pin selection. No executable is fetched or installed.

Prepared argv is serialized into one remote command string, quoting every argument, including empty
arguments, command-position assignments and reserved words. Local spawning never uses a shell. A
compatible POSIX account shell on the destination remains a prerequisite. SSH cannot undo sshd hooks
or account-shell startup behavior. Transport's current Linux Bash/base64 preparation requires
neither guest Python nor filesystem staging; actual initial-image prerequisites still need live
measurement.

## Process and evidence

`client.py` owns version gating and mapping into the shared report. `_io.py` owns one subprocess and
its pipes. EOF input uses the null device; finite bytes use a pipe. A single-thread pump performs
bounded, fair reads and short writes on non-blocking pipes, closing stdin once all bytes are sent.
Python 3.12 is the project minimum and adds Windows pipe support to
[`os.set_blocking`](https://docs.python.org/3.12/library/os.html#os.set_blocking).

Capture is bounded separately per stream and overflow reports incomplete retained output. Discard
and sensitive suppression drain without retaining payload. Raw stdout has carrier provenance; stderr
remains mixed client/remote evidence. Only transport's decoder can recover the separate guest
streams from its shared framing. Errors retain closed failure codes rather than exception text,
commands or payload-bearing diagnostics.

One deadline covers local preparation, client startup and I/O. Expiry stops execution and closes
owned handles. Local kill/reap has an additional bounded 0.5-second cleanup allowance; it does not
renew execution or establish guest cancellation. KeyboardInterrupt propagates after cleanup. After
observed client exit, drainage has at most 0.1 seconds and still requires actual EOF for
completeness. Inherited descendant handles cannot cause an unbounded drain.

A natural local exit from 0 through 254 supplies POSIX prepared-invocation completion. Status 255,
local signals and Windows native crash codes leave that completion unknown; SSH adds no completion
oracle. Dispatch is unknown once a client starts without stronger completion evidence, and not-sent
only when local checks or spawning establish that no command process started. Output failure and
partial capture remain visible even when completion is known. See OpenSSH's
[exit-status definition](https://man.openbsd.org/ssh#EXIT_STATUS).

## Proof and remaining work

The [SSH test handoff](../../../cli/tests/execution/carriers/ssh/README.md) gives local commands and
explicit live construction using the shared harness. Synthetic local executables test process I/O,
quoting and shared framing; an installed-client parser test checks option interpretation without
connecting. A fresh isolated Python process blocks transport's retirement modules while executing
the new binding. None of those tests establishes live authentication or platform coverage.

The operator's integration tester combines pinned transport and SSH branches locally, records both
inputs and the integrated revision, and exercises the shared acceptance cases using their authorized
resources. Contract-changing conflicts return to transport and the operator. No design-only main
merge is needed to perform that work. The [proof record](poc-results.md) distinguishes local results
from outstanding live evidence.

Full live-stream and terminal ownership, forwarding, production configuration/trust conversion,
provider-inner policy and complete workstation/platform evidence remain tracked in
[Phase 2](plan.md). The joint PoC acceptance matrix still belongs to transport; an unimplemented
candidate mode or unmeasured prerequisite cannot be silently counted as a passed proof case.
