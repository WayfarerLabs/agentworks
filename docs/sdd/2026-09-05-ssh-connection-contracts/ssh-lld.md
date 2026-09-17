# Buffered SSH Carrier

Status: Phase 1 implementation; joint live proof and Phase 2 remain open.

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

## Process and evidence

`client.py` owns version gating and mapping into the shared report. `_io.py` owns one subprocess and
its pipes. EOF input uses the null device; finite bytes use a pipe. A single-thread pump performs
bounded, fair reads and short writes on non-blocking pipes, closing stdin once all bytes are sent.
Python 3.12 is the project minimum and adds Windows pipe support to
[`os.set_blocking`](https://docs.python.org/3.12/library/os.html#os.set_blocking).

Windows client spawning removes two OpenSSH-private variables from a copy of the child environment:
`OPENSSH_STDIO_MODE` and `c28fc6f98a2c44abbbd89d6a3037d0d9_POSIX_FD_STATE`. They describe inherited
OpenSSH handles; Python supplies new pipes with different semantics. The Windows implementation
itself warns about
[stale grandparent descriptor state](https://github.com/PowerShell/openssh-portable/blob/v9.5.0.0/contrib/win32/win32compat/w32fd.c#L115-L128).
The filter is case-insensitive, preserves ordinary variables and the parent environment, and applies
to both version probing and dispatch. POSIX spawning retains its normal inherited environment. An
installed-client Windows regression exercises clean and contaminated state against an owned local
peer. That pre-authentication test does not replace the original failed live workload.

Capture is bounded separately per stream and overflow reports incomplete retained output. Discard
and sensitive suppression drain without retaining payload. Raw stdout has carrier provenance; stderr
remains mixed client/remote evidence. Only transport's decoder can recover the separate guest
streams from its shared framing. Errors retain closed failure codes rather than exception text,
commands or payload-bearing diagnostics.

One deadline covers local preparation, client startup and I/O. Expiry stops local observation and
closes owned handles. Local kill/reap has an additional bounded 0.5-second cleanup allowance; it
does not renew execution or establish guest cancellation. KeyboardInterrupt propagates after
cleanup. After observed client exit, drainage has at most 0.1 seconds and still requires actual EOF
for completeness. Inherited descendant handles cannot cause an unbounded drain.

Live measurements confirm that guest workloads and bootstrap descendants can survive local
observation expiry. The shared
[lifetime contract](../2026-09-12-transport-improv/execution-contract.md#carrier-contract) records
the accepted PoC limitation and the production cancellation gate. SSH does not add its own reaper,
cancellation protocol or retry. Bounded guest work may finish later; local return is not proof that
it did.

A natural local exit from 0 through 254 supplies POSIX prepared-invocation completion. Status 255,
local signals and Windows native crash codes leave that completion unknown; SSH adds no completion
oracle. Dispatch is unknown once a client starts without stronger completion evidence, and not-sent
only when local checks or spawning establish that no command process started. Output failure and
partial capture remain visible even when completion is known. See OpenSSH's
[exit-status definition](https://man.openbsd.org/ssh#EXIT_STATUS).

An induced trust or authentication refusal can be known to the tester while the carrier still sees
only status 255. The carrier cannot distinguish that status from lost observation using a stable
OpenSSH result field. It therefore preserves unknown dispatch and mixed stderr rather than parsing
diagnostic prose or adding another probe. Strict trust never becomes implicit enrollment. On
Windows, local status 1 after a deadline may be the result of killing the client; only the natural
status observed before cleanup can establish completion.

## Proof and remaining work

The [SSH test handoff](../../../cli/tests/execution/carriers/ssh/README.md) gives local commands and
explicit live construction using the shared harness. Synthetic local executables test process I/O,
quoting and shared framing. Installed-client tests check option interpretation offline and
pre-authentication rejection against an owned local peer. A fresh isolated Python process blocks
transport's retirement modules while executing the new binding. None of those tests establishes live
authentication or platform coverage.

The operator's integration tester combines pinned transport and SSH branches locally, records both
inputs and the integrated revision, and exercises the shared acceptance cases using their authorized
resources. Contract-changing conflicts return to transport and the operator. No design-only main
merge is needed to perform that work. The [proof record](poc-results.md) distinguishes local results
from outstanding live evidence.

Full live-stream and terminal ownership, forwarding, production configuration/trust conversion,
provider-inner policy and complete workstation/platform evidence remain tracked in
[Phase 2](plan.md). The joint PoC acceptance matrix still belongs to transport; an unimplemented
candidate mode or unmeasured prerequisite cannot be silently counted as a passed proof case.
