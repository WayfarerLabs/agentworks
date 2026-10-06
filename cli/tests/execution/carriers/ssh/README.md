<!-- cspell:ignore asdict pathlib pseudoconsole -->

# SSH carrier tests and live handoff

Run the local tests from `cli/`:

```bash
uv run pytest tests/execution/carriers/ssh/ -m 'not integration'
```

`test_terminal_windows.py` uses synthetic Win32 and CRT boundaries on every host, including Windows.
It exercises endpoint-kind refusal, raw input policy, exact restoration, viewport queries, native
failure codes, cleanup uncertainty and same-worker lifetime without accessing a caller console or
launching a client. They run in the ordinary synthetic suite and supply no native console
acceptance. The resource does not read keyboard events, select a Windows child-terminal mechanism or
enable the carrier's terminal feature. Native Windows selection must cover an independently cleaned
owned console and then the complete supported Windows workflow.

`test_terminal_windows_native.py` marks only its native case `windows`; other hosts skip that case
and can check the fixed-width Win32 record ABI without native effects. On Windows, the fixture owns
one fresh hidden console child with explicit `CONIN$` and `CONOUT$` descriptors. It checks native
resource admission, early raw input, viewport geometry and exact restoration with original and
custom modes, preserving output mode, code pages, handle flags and descriptor inheritability. Its
[`ReadConsoleInputExW`](https://learn.microsoft.com/en-us/windows/console/readconsoleinputex) probe
uses `CONSOLE_READ_NOWAIT` for empty queues, injected non-key events and injected UTF-16 key
records. Each measured poll must finish within five seconds, a generous fixture bound rather than a
production keyboard cancellation guarantee. These injections establish record polling behavior;
physical keyboard translation, virtual terminal key sequences, keyboard cancellation, SSH clients,
pseudoconsole preparation and the complete Windows workflow still require separate proof.

The parent retains and reaps exactly its child, with a 120-second execution timeout and a 10-second
reaping timeout. The child releases the resource, restores its fixture modes and code pages, closes
its descriptors and detaches its console in cleanup. The parent observes only that reported console
window for up to 10 seconds; missing or unresolved window evidence fails the native case. It never
scans or terminates unrelated console hosts. JSON observations and stderr remain under the test's
owned temporary directory, including parent cleanup evidence. This is a native local primitive
fixture for ordinary Windows CI, with no network, credentials or caller-console access; report its
actual native result separately from synthetic passes and skips.

Both fixture workers begin inert. Only a successful start admits effects; a failed or interrupted
start cancels even a delayed thread tail. Every admitted borrower settles before outer console or
process cleanup can proceed. Synthetic tests on every host exercise failures before and after thread
startup, interrupted completion waits and cleanup ordering without native console effects.

The process fixtures use synthetic Python children and temporary files. The shared conformance
fixture substitutes a local POSIX-shell executable for SSH, exercising the real quoting and pipe
pump without authentication or a server. SSH-boundary live-I/O regressions exercise borrowed input,
separate binary output sinks, partial writes and temporary sink stalls. The shared process tests own
endpoint validation and interruption cleanup; existing SSH client tests own status 255 evidence. An
installed-client test uses fresh fixture keys and one owned loopback sshd to exercise sensitive
binary live duplex delivery through the real client. Other installed-client tests parse `ssh -G`
options and drive a real client against an owned loopback peer that refuses before authentication.
Forwarding tests reuse the fixture-owned loopback sshd when available and exercise authenticated
delivery, listener refusal and cleanup. These local fixtures do not establish the supported
workstation and provider matrix; report skips separately.

The conformance tests also pass synthetic percent-encoded JSON and literal percent tokens, dollar
expressions, quotes, backslashes, Unicode and multiline environment values through transport's
private inline candidate and the SSH carrier. One test uses the POSIX-shell substitute; an
integration-marked companion uses the owned loopback sshd. Both require exact guest bytes and keep
the values out of SSH argv. These regressions address the new-path delivery shape raised by
[issue #845](https://github.com/WayfarerLabs/agentworks/issues/845). They do not establish a fix for
current legacy callers, production RunContext composition or native Windows/macOS acceptance.

The integration-marked file-delivery tests compose the production fixed helper bundles with the real
SSH carrier and the fixture account's direct identity plan. They exercise bounded binary read, typed
absence and limit refusal, then stage creation, exact-offset chunks, helper-backed reading,
reconciliation and exact scratch cleanup with an explicit Linux `/usr/bin/python3` runtime
selection. Every operation requires a positive runtime-prerequisite result before its helper
observation. The fixed helper prefix and request share sensitive stdin; the short bootstrap remains
in argv. Each helper operation receives one carrier call, and payloads stay out of invocation and
result representations. The same boundary downloads an immutable snapshot in exact chunks, including
a nonzero offset, then recovers cleanup-only ownership and removes its exact fixed-root scratch
object. These loopback Linux cases are adapter evidence for transport's private helpers. They do not
expose `FileAccess`, establish production grants or supply native workstation/provider acceptance.
The snapshot fixture requires `/tmp` to be a root-owned mode-1777 directory, matching the production
helper prerequisite; namespaces that remap its owner skip that happy-path case before dispatch.

For the operator's integration tester, combine the SSH and transport branches in a disposable local
branch. Record both input commit IDs, the integrated commit, conflict resolutions and the installed
revision. Install that tree's `cli/` package in the tester's isolated environment. Use transport's
[shared harness and evidence guidance](../../README.md); do not create another case list or infer a
live pass from these fixtures.

Supply explicit, pre-provisioned fixture identity and strict trust paths. This example performs real
remote work when executed; select its values only from the tester's authorized charter:

```python
from dataclasses import asdict
from pathlib import Path

from agentworks.execution.carriers.ssh import SSHCarrier, SSHConnection, SSHTrustFiles
from tests.execution.conformance import check_buffered_contract

carrier = SSHCarrier(
    SSHConnection(
        host="authorized-fixture.example",
        user="fixture",
        identity_file=Path("/absolute/fixture/identity"),
        trust=SSHTrustFiles((Path("/absolute/fixture/known_hosts"),)),
        ssh_executable="/usr/bin/ssh",
    )
)
for observation in check_buffered_contract(carrier):
    print(asdict(observation))
```

The selected installed client must be OpenSSH 8.5 or newer. The initial shared vectors need Linux
Bash 5.1 or newer, base64, GNU env with `--default-signal=PIPE`, and descriptor support on the
destination; account-shell startup remains part of the live proof. The current candidate advertises
live byte I/O but not terminals. Exercise the shared live source and sink cases through SSH in
addition to the buffered vectors, then run the transport-required fault and interruption lanes.
Record workstation OS/client separately from VM platform/server, selected authentication/trust
policy, measured prerequisites, gaps, and independent cleanup. Retain safe observation fields; do
not publish credentials, key contents or raw sensitive diagnostics.

Failures remain evidence for the owners. In particular, local status 255 is ambiguous, strict trust
failure does not authorize enrollment, and timeout does not confirm remote cancellation. Re-run
affected cases after any implementation or contract correction. Joint acceptance remains with
transport and the operator.

The complete encoded input envelope shares one limit across source, arguments, environment,
directory and stdin. Follow transport's
[input accounting](../../../../agentworks/execution/README.md#input-accounting) for the exact
prepared-byte measurement; the limit is not a fixed raw-stdin budget.

An operator testing deliberately invalid trust knows why the connection failed. The carrier sees
OpenSSH status 255, which also covers lost observation, so it conservatively reports unknown
dispatch without parsing client diagnostic prose. No second probe or automatic retry resolves that
ambiguity. On Windows, `local_status=1` after timeout can be the local kill result, not a natural
client exit that the pump ignored.

Live tests confirmed that a local deadline can leave the guest workload and bootstrap descendants
running until the workload ends or receives separately authorized cleanup. See transport's
[observation and guest lifetime](../../../../agentworks/execution/README.md#observation-and-guest-lifetime).
This PoC must not back production operations until that shared lifecycle gate is satisfied.

The authenticated Windows retest passed in both the original SSH-parent context and a clean launch;
the [proof record](../../../../../docs/sdd/2026-09-05-ssh-connection-contracts/poc-results.md) links
the measured report. For subsequent Windows regressions, preserve those separate launch contexts,
including a session entered through Windows OpenSSH when applicable. Record the actual client
path/version and whether the two OpenSSH-private handle variables are present, without dumping
environment values. The carrier clears those variables only for its child processes because it owns
fresh pipes. Compare bounded, independent read-only cases and run the shared vectors; report
original-context results separately from a clean workstation launch. The local peer test covers the
reproduced descriptor hazard before authentication, not the complete live invocation.

The agent-socket fixture owns a short directory under `/tmp` because macOS limits Unix-socket path
length. It checks both non-socket refusal and genuine-socket acceptance, then closes the socket and
removes its directory. When changing that fixture, test on macOS under its normal temporary
environment and a deliberately long pytest base directory, and verify cleanup. Report local suite
failures and skip reasons separately. Keep remote detached children separate from local descendants
retaining the client's pipe handles when reporting ownership tests. The proof record links the
measured macOS fixture and local ownership results.

Keep account-shell delivery prerequisites separate from the application's selected interpreter.
Transport's default-shell lane requires independent destination identity/account-shell evidence;
fixed-interpreter vectors do not cover it. A refusing account shell can return a raw SSH status
before the bootstrap starts, so a status alone is not an application verdict. Captured output also
needs the shared framing checks; suppressed output supplies no framing proof. Use the current shared
sensitive vector, including its expected status, rather than treating empty retained output alone as
proof that sensitive reflection ran.

## POSIX terminal candidate fixtures

The private terminal relay tests use synthetic Python children and owned PTYs only. They cover
binary preparation followed by queued keys, initial modes/geometry, explicit TERM, partial writes,
stalls, fair duplex delivery, endpoint failures and unchanged borrowed resources. No SSH client,
server, operator terminal or infrastructure participates in these cases. Repeated geometry changes
reach a synthetic child's stdin PTY through the actual process owner's SIGWINCH operation, with
borrowed modes and descriptor flags independently checked. This proves local signal/dimension
delivery, not remote SSH resize; the carrier terminal feature remains disabled.

Resize boundary faults cover finite notification expiry during bounded and unbounded operations,
unknown outcomes, a late accepted signal, natural client exit races and control interruption. Unsent
requests use fresh process facts and known completion survives uncertainty. Faults are injected at
the actual public notification/close boundaries without replacing process ownership or reaching into
its private notification state. Held settlement checks retain the borrowed terminal and owned slave
until the shared owner settles, preserve the first control object through repeated caller
interruption and report cleanup uncertainty without native diagnostics. Transport's separate owner
tests establish claimed notification custody.

The signal case confines real repeated SIGINT to an owned subprocess. It holds client cleanup open,
interrupts the main thread while waiting, and proves the caller waits for worker completion,
restoration and shared-owner settlement while preserving the first control exception. Synthetic
cases separately cover interrupted worker start and process admission, and restoration uncertainty.
Acquisition composition cases fail after the raw-mode effect, then independently fail restoration or
owned descriptor closure. They prove ordinary observation failure or preserved control identity with
safe cleanup evidence, and repair and verify the owned fixtures after removing fault injections.
Native primitive cases preserve the prior cause and context; public relay cases suppress raw cleanup
chains. Clean acquisition rollback and pre-effect refusal retain their distinct failure categories.

Linux post-exit cases hold inherited stdout/stderr writers while the two borrowed sinks stall in
turn. They prove pending bytes survive those stalls, collection remains bounded after client exit,
and both a held writer and a continuous writer produce incomplete output evidence. A disposable
subprocess adopts and explicitly kills/reaps its synthetic descendant, closes its own PTYs and
independently checks client reaping and borrowed mode restoration. These cases add no macOS proof.

Supply an explicit temporary root in the developer or review worktree for these runs. An owning
process/job timeout bounds fixture hangs; thread status after interrupted join is not a completion
fact. Native macOS/Windows, real SSH terminal delivery and emulator sanitation require separate
integration evidence.

The integration-marked `test_terminal_installed.py` cases compose the private POSIX relay with
transport's actual terminal handoff, strict connection admission, the installed-client version check
and fixed SSH argv. They use one owned Linux loopback server and one connected SSH client per case,
plus the offline client version check. Generated keys exist only during native execution. The
successful case observes binary source/environment/argument canaries after the first gate, withholds
finite keyboard input until both handoff and application readiness, and checks initial guest
geometry, two SIGWINCH-driven changes, natural completion and exact presentation delivery. The
missing-runtime case requires withheld payload and keys. Both independently check local client
reaping, relay PTY closure and restored borrowed input modes, file status and inheritable flags.
Fixture observation is bounded and temporary; it adds no production sensitive-output retention.

Native execution requires a separately authorized tester charter, a tester-owned account/home or
independently established configured shell with no startup/home hook access, and installed OpenSSH
client, key generator and server. `-F none` isolates client configuration; `PermitUserRC no` does
not isolate account-shell startup. Missing platform or tools may skip before effects; unexpected
server exit or product failure must fail. The whole case stays on an admitted retained fixture
worker through borrowed resource cleanup. Its original operation deadline is 120 seconds, with
bounded client/server reaping. Guest cleanup uses only its positively reported PID, start identity
and UID, with a 20-second observation bound from its first application report (15 seconds of guest
self-life plus five seconds for cleanup). Unknown, unobservable or remaining guest identity fails or
adds explicit cleanup uncertainty to an existing failure; parent-server reaping supplies no
guest-absence proof. No reported guest is signaled. A separate job timeout must still bound a hung
test process. Use a short, private temporary parent with suitable ancestor permissions and no
inherited ACLs.

Collection performs no SSH, key generation, listener or PTY acquisition:

<!-- cspell:ignore venv basetemp -->

```sh
timeout 90 .venv/bin/pytest tests/execution/carriers/ssh/test_terminal_installed.py \
  --collect-only --basetemp=../scratch/terminal-collection
```

Under that separate native charter, run the two cases together with their own temporary root and
external timeout, for example:

```sh
timeout 360 .venv/bin/pytest tests/execution/carriers/ssh/test_terminal_installed.py \
  -m integration -n 0 --basetemp=/tmp/authorized-terminal-proof/cases
```

Source checks and collection do not establish a native pass. These Linux loopback cases supply no
macOS/Windows proof, production RunContext composition or enabled SSHCarrier terminal feature.
Readiness `finish()` is only collector finalization; shared presentation/emulator sanitation remains
a separate open requirement. The refusal case does not cover the complete native interruption and
restoration fault matrix.

## Trust maintenance and forwarding fixtures

Trust tests operate only on isolated snapshots and owned temporary destinations. They cover complete
byte preservation, blocked/failed publication, interrupted maintenance, stale generation refusal,
concurrent writers and integrity checks. They never read the operator's ambient trust or import it
implicitly. Managed trust resolves at each operation, so reusing a carrier must not retain an old
admission after a block or refresh. Raw trust files remain under their explicit maintenance owner.

Forwarding's acknowledgment proves authenticated session establishment and successful local listener
setup. It does not probe destination health. Its tests include occupied first/later listeners,
separate address families, refused trust/authentication/command execution, and binary traffic.
Owner-boundary regressions cover inert worker admission, interruption after process admission,
repeated close interruptions, natural exit versus cleanup kill, and delayed worker shutdown. Record
actual client/server versions and independently verify released listeners and fixture processes.
Native Windows and macOS need their own observations; Linux loopback results do not establish their
process or terminal behavior.

Enrollment fixtures exercise actual first-contact writes followed by strict verification, CA and
revocation policy, mismatches, failed-authentication key retention and strict recovery. Fault cases
cover a positive acknowledgment without a saved key, partial metadata, changed policy, competing
attempts and interruption. These use fixture-only creation IDs; they do not establish transport's
production creation provenance or publication binding. Every test owns its server, identity, agent
and policy files. Native platform persistence and cleanup still need the integration tester's
separate observations.

The integration-marked trust workflow composes the owned enrollment-server helper with the actual
maintenance CLI and a reused managed carrier. It verifies unknown-key refusal before and after a
creation receipt, complete-policy publication using the observed generation, strict command
execution, blocking, failed refresh with retained learned keys and immutable prior generations,
explicit repair and strict reconnect. Its original policy includes valid unrelated CA and revoked
records plus a real KRL; all source bytes and timestamps remain unchanged. Separate cases use real
`ssh-keygen` hashed records at a nondefault port, with and without HostKeyAlias, and refuse a
changed lookup identity against the same server. These Linux loopback fixtures use fixture-only
creation provenance and do not prove provider ownership, publication binding, production RunContext
use or native macOS/Windows behavior. Run them only under a separate authorized integration-test
charter; collection and synthetic maintenance checks alone supply no installed-client workflow
acceptance.

The integration-marked authentication-offer tests retain the owned server's DEBUG2 packet logs. They
compare every queried or signed wire key, including its algorithm and complete blob, with the
configured fixture identity. A foreground owned agent contains independently verified multiple
fixture keys. Rejected configured keys trap forbidden fallback offers; public-only IdentityFile
cases distinguish an explicitly selected signer from an inherited agent. A matching sibling public
key authenticates, while replacement with another fixture key refuses before authentication. A real
sibling user certificate must never appear in the server's offers. Collection and synthetic log
checks do not establish native acceptance. Native Linux runs require the authorized tester and own
all keys, logs, server, agent and socket lifetimes. These cases do not place hostile default user or
system SSH config: portable OpenSSH locates user config through the account's passwd home, so
setting HOME to a fixture directory would not supply that evidence. Hostile default config, account
startup and native macOS/Windows agent behavior remain separate platform acceptance gates.

Native loopback execution requires a tester-owned account and home, or independently demonstrated
absence of startup hooks in the configured account shell. `PermitUserRC no` disables SSH user rc
files but does not exclude account-shell startup or home hooks. Do not infer account isolation from
that option or run these cases through an ordinary operator account without that prerequisite. The
source-pure fixture worker admits effects only after thread startup returns, retains one worker
through process construction and borrowed lifetime, and waits for explicit cleanup completion before
logs, sockets and directories close. Synthetic cases cover inert late tails, caller interruption,
constructor/cleanup failures and cleanup order. Unexpected server startup exit fails with retained
owned diagnostics; only identified platform/binary prerequisites skip. Native process, account-shell
and default-config acceptance still requires the tester's separate charter.
