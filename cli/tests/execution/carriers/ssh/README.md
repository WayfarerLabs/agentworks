<!-- cspell:ignore asdict pathlib -->

# SSH proof tests and live handoff

Run the local tests from `cli/`:

```bash
uv run pytest tests/execution/carriers/ssh/ -m 'not integration'
```

The process fixtures use synthetic Python children and temporary files. The shared conformance
fixture substitutes a local POSIX-shell executable for SSH, exercising the real quoting and pipe
pump without authentication or a server. The installed-client `ssh -G` checks parse options only.
These are local mechanics evidence, never live SSH or supported-platform evidence.

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

from agentworks.execution.carriers.ssh import SSHCarrier, SSHConnection
from tests.execution.conformance import check_buffered_contract

carrier = SSHCarrier(
    SSHConnection(
        host="authorized-fixture.example",
        user="fixture",
        identity_file=Path("/absolute/fixture/identity"),
        known_hosts_file=Path("/absolute/fixture/known_hosts"),
        ssh_executable="/usr/bin/ssh",
    )
)
for observation in check_buffered_contract(carrier):
    print(asdict(observation))
```

The selected installed client must be OpenSSH 8.5 or newer. The initial shared vectors need Linux
Bash/base64 and descriptor support on the destination; account-shell startup remains part of the
live proof. The current candidate advertises neither live streams nor terminals. Run the same shared
vectors against transport's native carrier, then its required fault and interruption lanes. Record
workstation OS/client separately from VM platform/server, selected authentication/trust policy,
measured prerequisites, gaps, and independent cleanup. Retain safe observation fields; do not
publish credentials, key contents or raw sensitive diagnostics.

Failures remain evidence for the owners. In particular, local status 255 is ambiguous, strict trust
failure does not authorize enrollment, and timeout does not confirm remote cancellation. Re-run
affected cases after any implementation or contract correction. Joint acceptance remains with
transport and the operator.
