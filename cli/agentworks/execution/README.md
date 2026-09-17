# Independent execution mechanics

This package currently implements a bounded, internal Linux execution proof. Production factories
and RunContext still use the existing stack. These modules are not a permission-scoped public API;
they must not be handed directly to capability consumers.

`carrier.py` defines the buffered adapter boundary. Preparation supplies literal bootstrap argv;
CarrierIO holds the only finite input source. A report distinguishes submitted/unknown dispatch,
observed completion of that invocation, local status, raw stream provenance, completeness and
retention. Local status alone is not a guest exit. Payload fields have no diagnostic representation.

`Failure.INPUT` records failed or incomplete required input delivery; intentional consumer closure
is not automatically a failure. `Failure.OUTPUT` records failed output collection, including a
stream whose EOF cannot be established within the carrier's collection bound. `OUTPUT_LIMIT` is the
distinct capture-limit outcome. These local failures preserve independently observed completion and
partial-stream evidence; none establishes guest cancellation or permits replay.

`preparation.py` prepares literal commands and explicit sh/bash scripts, ordinary environment/cwd
and finite input. Source, environment and input are encoded into stdin, not process arguments. The
Linux bootstrap needs Bash 5.1 or newer, GNU base64/env and `/dev/fd`; destination account-shell
lookup also needs getent/id. It uses no installed guest helper, Python or staging files. Login and
interactive startup are explicitly refused by this proof subset. Preparation accepts at most 256 KiB
of encoded input; carriers can impose smaller documented delivery limits. Bash's saved
process-substitution waits require the 5.1 floor; local fault-injection evidence currently covers
Bash 5.2.15, not every version at or above that floor.

The bootstrap waits for source and stdin producers as well as output encoders. Unexpected producer
failure invalidates delivery even if the command exits zero. Intentional early input closure is
allowed; GNU env's `--default-signal=PIPE` ensures its SIGPIPE outcome is observable.

The shared decoder validates separately tagged guest stdout/stderr inside carrier stdout. Raw
carrier stderr remains diagnostic/mixed data and is never promoted to guest stderr. Stream markers
are not completion evidence. Sensitive calls suppress workload output and retained carrier bytes;
suppression is reported explicitly, not as a successfully decoded empty stream.

`carriers/proxmox.py` accepts resolved connection authority for one VM. It makes one dispatch and
polls QGA status without replay after a failed observation. Input must be ASCII and at most 65,536
bytes. An owned workstation-Python HTTP worker bounds response size and consumes the same deadline;
timeout/interruption kills and reaps that worker, not the guest command. TLS verification is
mandatory. `ProxmoxConnection.ca_bundle` accepts an explicit PEM CA-bundle `Path`; `None` uses the
system/default trust context. An explicit bundle selects that trust source, not an additional trust
bypass. The API hostname must match the certificate; there is no server-name override. Redirects and
ambient proxies are disabled, and provider exception text is not returned.

## Observation and guest lifetime

A deadline bounds local observation only. Ordinary guest commands and bootstrap descendants can
remain running after return on both SSH and QGA, including after the initiating connection ends.
This proof has no guest cancellation handle, runtime timer or reaper. Do not retry an uncertain
command on the assumption it stopped. Production use is not enabled; it requires the separate
owned-workload lifecycle/cancellation implementation. Test only bounded workloads under explicit
cleanup authority, and verify their guest-side cleanup independently.

## Input accounting

The 262,144-byte preparation bound and 65,536-byte native-delivery bound apply to the complete
encoded envelope, not raw application stdin. Script source, argv, environment, cwd and framing share
it; base64 expands payload bytes. For example, a sh script containing `/bin/cat` with no env or cwd
fits 196,554 raw stdin bytes at preparation, but 49,098 through native delivery. Literal `/bin/cat`
argv gives 196,551/49,095 instead. These are composition-specific examples, not general stdin
guarantees.

After `prepare(...)`, `len(prepared.io.input.data)` gives the exact encoded size (the input is a
`FiniteInput`). Check it against the chosen carrier's documented limit before delivery; oversized
native input raises `ValidationError` before any dispatch. Preparation itself rejects an envelope
over 262,144 bytes. This bounded proof has no automatic chunking or staging fallback for larger
work; it is not the eventual public script/file transfer contract.

## Scope and checks

The carrier does not demote QGA's root identity or grant permission to use it. Only an explicitly
authorized proof composition may construct it. Shared identity/elevation binding, permission-scoped
access, complete platform coverage and production integration are not implemented here.

Run the local evidence from `cli/` with `uv run pytest tests/execution`. The reusable vectors in
`tests.execution.conformance` require an explicitly supplied carrier. Their local process oracle
does not establish SSH or live Proxmox compatibility. The native carrier is isolated from the plugin
registry because importing that registry currently loads legacy execution modules.
