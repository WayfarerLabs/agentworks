<!-- cspell:ignore asdict pathlib -->

# SSH carrier tests and live handoff

Run the local tests from `cli/`:

```bash
uv run pytest tests/execution/carriers/ssh/ -m 'not integration'
```

The process fixtures use synthetic Python children and temporary files. The shared conformance
fixture substitutes a local POSIX-shell executable for SSH, exercising the real quoting and pipe
pump without authentication or a server. SSH-boundary live-I/O regressions exercise borrowed input,
separate binary output sinks, partial writes, temporary backpressure, endpoint faults, interruption
cleanup and ambiguous status 255. Installed-client tests parse `ssh -G` options and drive a real
client against an owned loopback peer that refuses before authentication. Forwarding tests also
start a fixture-owned loopback sshd when available and exercise authenticated delivery, listener
refusal and cleanup. These local fixtures do not establish the supported workstation and provider
matrix; report skips separately.

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

## Trust maintenance and forwarding fixtures

Trust tests operate only on isolated snapshots and owned temporary destinations. They cover complete
byte preservation, blocked/failed publication, interrupted maintenance, stale generation refusal,
concurrent writers and integrity checks. They never read the operator's ambient trust or import it
implicitly. Managed trust resolves at each operation, so reusing a carrier must not retain an old
admission after a block or refresh. Raw trust files remain under their explicit maintenance owner.

Forwarding's acknowledgment proves authenticated session establishment and successful local listener
setup. It does not probe destination health. Its tests include occupied first/later listeners,
separate address families, refused trust/authentication/command execution, and binary traffic.
Record actual client/server versions and independently verify released listeners and fixture
processes. Native Windows and macOS need their own observations; Linux loopback results do not
establish their process or terminal behavior.

Enrollment fixtures exercise actual first-contact writes followed by strict verification, CA and
revocation policy, mismatches, failed-authentication key retention and strict recovery. Fault cases
cover a positive acknowledgment without a saved key, partial metadata, changed policy, competing
attempts and interruption. These use fixture-only creation IDs; they do not establish transport's
production creation provenance or publication binding. Every test owns its server, identity, agent
and policy files. Native platform persistence and cleanup still need the integration tester's
separate observations.
