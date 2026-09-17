# Independent execution mechanics

This package currently implements a bounded, internal Linux execution proof. Production factories
and RunContext still use the existing stack. These modules are not a permission-scoped public API;
they must not be handed directly to capability consumers.

`carrier.py` defines the buffered adapter boundary. Preparation supplies literal bootstrap argv;
CarrierIO holds the only finite input source. A report distinguishes submitted/unknown dispatch,
observed completion of that invocation, local status, raw stream provenance, completeness and
retention. Local status alone is not a guest exit. Payload fields have no diagnostic representation.

`preparation.py` prepares literal commands and explicit sh/bash scripts, ordinary environment/cwd
and finite input. Source, environment and input are encoded into stdin, not process arguments.
The Linux bootstrap needs Bash, GNU base64/env and `/dev/fd`; destination account-shell lookup also
needs getent/id. It uses no installed guest helper, Python or staging files. Login and interactive
startup are explicitly refused by this proof subset. Preparation accepts at most 256 KiB of encoded
input; carriers can impose smaller documented delivery limits.

The shared decoder validates separately tagged guest stdout/stderr inside carrier stdout. Raw
carrier stderr remains diagnostic/mixed data and is never promoted to guest stderr. Stream markers
are not completion evidence. Sensitive calls suppress workload output and retained carrier bytes;
suppression is reported explicitly, not as a successfully decoded empty stream.

`carriers/proxmox.py` accepts resolved connection authority for one VM. It makes one dispatch and
polls QGA status without replay after a failed observation. Input must be ASCII and at most 65,536
bytes. An owned workstation-Python HTTP worker bounds response size and consumes the same deadline;
timeout/interruption kills and reaps that worker, not the guest command. TLS verification defaults
on, redirects and ambient proxies are disabled, and provider exception text is not returned.

The carrier does not demote QGA's root identity or grant permission to use it. Only an explicitly
authorized proof composition may construct it. Shared identity/elevation binding, permission-scoped
access, complete platform coverage and production integration are not implemented here.

Run the local evidence from `cli/` with `uv run pytest tests/execution`. The reusable vectors in
`tests.execution.conformance` require an explicitly supplied carrier. Their local process oracle
does not establish SSH or live Proxmox compatibility. The native carrier is isolated from the
plugin registry because importing that registry currently loads legacy execution modules.
