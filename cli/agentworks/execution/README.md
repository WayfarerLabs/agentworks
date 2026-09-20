# Independent execution mechanics

This package contains internal execution and file-operation building blocks. Production factories
and RunContext still use the existing stack. These modules are not a permission-scoped public API;
they must not be handed directly to capability consumers.

`carrier.py` defines the adapter boundary, including candidate borrowed byte endpoints. Preparation
supplies literal bootstrap argv; CarrierIO holds the only input source. A report distinguishes
submitted/unknown dispatch, observed remote command-chain completion, local status, raw stream
provenance, completeness and retention. Completion can belong to an account shell that refused
before the bootstrap ran; it is not independent proof of bootstrap or application execution. Local
status alone is not a guest exit. Payload fields have no diagnostic representation.

`Failure.INPUT` records failed or incomplete required input delivery; intentional consumer closure
is not automatically a failure. `Failure.OUTPUT` records failed output collection, including a
stream whose EOF cannot be established within the carrier's collection bound. `OUTPUT_LIMIT` is the
distinct capture-limit outcome. These local failures preserve independently observed completion and
partial-stream evidence; none establishes guest cancellation or permits replay.

`models.py` defines immutable literal commands and scripts with explicit `Shell.SH`, `Shell.BASH` or
`Shell.USER_DEFAULT` selection and separate startup flags. `preparation.py` consumes those values
with ordinary environment/cwd and finite input. Source, environment and input are encoded into
stdin, not process arguments. The Linux bootstrap needs Bash 5.1 or newer, GNU base64/env and
`/dev/fd`; destination account-shell lookup also needs getent/id. It uses no installed guest helper,
Python or staging files. Login and interactive startup are explicitly refused by this proof subset.
Preparation accepts at most 256 KiB of encoded input; carriers can impose smaller documented
delivery limits. Bash's saved process-substitution waits require the 5.1 floor; local
fault-injection evidence currently covers Bash 5.2.15, not every version at or above that floor.

The bootstrap waits for source and stdin producers as well as output encoders. Unexpected producer
failure invalidates delivery even if the command exits zero. Intentional early input closure is
allowed; GNU env's `--default-signal=PIPE` ensures its SIGPIPE outcome is observable.

The shared decoder validates separately tagged guest stdout/stderr inside carrier stdout. Raw
carrier stderr remains diagnostic/mixed data and is never promoted to guest stderr. Stream markers
are not completion evidence. Sensitive calls suppress workload output and retained carrier bytes;
suppression is reported explicitly, not as a successfully decoded empty stream. Suppressed or
discarded output provides no framing evidence. Neither an observed zero nor absent retained bytes
alone establishes application success; public outcome interpretation is not enabled by this internal
proof.

`carriers/proxmox.py` accepts resolved connection authority for one VM. It makes one dispatch and
polls QGA status without replay after a failed observation. Input must be ASCII and at most 65,536
bytes. An owned workstation-Python HTTP worker bounds response size and consumes the same deadline;
timeout/interruption kills and reaps that worker, not the guest command. TLS verification is
mandatory. `ProxmoxConnection.ca_bundle` accepts an explicit PEM CA-bundle `Path`; `None` uses the
system/default trust context. An explicit bundle selects that trust source, not an additional trust
bypass. The API hostname must match the certificate; there is no server-name override. Redirects and
ambient proxies are disabled, and provider exception text is not returned.

`carriers/_subprocess.py` supplies a shared process pump with finite or explicitly enabled live
input, separate bounded outputs, explicit environment binding and bounded local cleanup. Borrowed
byte endpoints must return without waiting on external I/O. The pump never closes them or changes
their descriptor flags. Short sink writes retain a bounded pending suffix; temporary stalls are not
EOF. Endpoint failures preserve independently observed completion and mark incomplete delivery.
After child exit, pending sink delivery pauses all fresh pipe collection. Once both pending chunks
clear, collection resumes against its accumulated budget; the original deadline separately bounds
sink delivery. A stalled sink cannot exempt fresh reads from the other stream's collection budget.

`SinkOutput` feeds transient raw carrier bytes to trusted collectors and reports `DELIVERED` with no
retained bytes, including on sensitive calls. These are private preparation endpoints, not plugin
callbacks or permission to display sensitive output. Existing sensitive capture/discard calls still
suppress retained bytes. A carrier must explicitly enable live I/O in the pump; otherwise live input
or a sink requiring live delivery refuses before process creation. Buffered Proxmox observations can
feed the same sink interface without advertising live I/O, retaining truncation and deadline facts.

The pump's result records local process facts with unknown stream provenance; each carrier owns
interpretation as delivery evidence. It does not infer guest dispatch or termination. The SSH code
in this branch still uses its private buffered pump; its owning implementation lane has adopted the
shared pump separately. A compatibility refusal for the old adapter and joint SSH byte-I/O proof are
required before integrating the candidate extension. No terminal support is enabled by these types.

The cleanup guard covers the I/O loop, not process construction or the intervening initialization. A
real SIGINT probe on Linux with CPython 3.12.13 interrupted construction after child creation and
left that local child alive without a returned handle. Launch-interruption ownership remains an
unresolved production gate, including for the existing SSH copy. A deadline consumes startup time
but cannot interrupt an OS process-creation call that has not returned; it is not a hard real-time
bound over that call.

`carriers/wsl2.py` is a private buffered candidate bound to an explicit local WSL executable,
distribution and delivery user. It sends literal prepared argv through `--exec`, without selecting
an application shell or discovering a route. Its candidate report uses observed client statuses 0
through 255 as dispatch evidence, but only zero establishes typed completion. WSL can collapse a
normal exit and a signal into the same number, and a lost status channel can produce 1. Nonzero
statuses therefore retain the raw local number without inventing an exact guest exit or signal;
absent, negative and out-of-range statuses leave dispatch uncertain too. Independently known
completion survives a local I/O failure. This source-based interpretation is not native Windows
acceptance. The carrier is not registered or wired into production. Shared preparation must still
establish every required application exit and signal independently; this conservative adapter does
not narrow that requirement. Real WSL argument/byte fidelity, status interpretation, interruption
and distribution lifetime require proof, and the shared pump's launch-interruption gap applies.

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

## Private JSON transformation

`_json.py` supplies the local, bounded transformation for file-operation composition. It preserves
replace, skip-existing and both recursive object-merge strategies; arrays/scalars are atomic and
JSON null is a value, not deletion. Source validation precedes every strategy, while replace and
skip-existing do not parse old content. Byte and container-depth limits apply to parsed inputs and
the proposed serialized result. These are rejection thresholds, not overrides of the interpreter's
capacity. Standard-library nesting and integer-conversion limits can also cause a safe refusal even
within caller-selected bounds; the implementation does not change process-global interpreter limits.

The return is proposed publication bytes or `None` for skip-existing, not evidence of a filesystem
change. Destination observation, file-kind safety, concurrency and atomic publication belong to the
file service, which is not implemented or wired to production yet.

## Private file observations

`_file_snapshot.py` reads a bounded regular-file snapshot relative to a borrowed trusted root
descriptor. It refuses observed links, multiply linked files and special objects, reads in bounded
chunks, and binds the returned bytes to their digest and before/after metadata. The caller retains
the root descriptor and owns any cooperating-writer lock. The reader creates no files or locks and
does not expose FileAccess, publication or permission enforcement.

The POSIX component walk detects different-filesystem crossings through `st_dev`; it cannot detect
same-filesystem bind mounts or establish `openat2` confinement. It refuses observed changes but does
not contain a malicious same-user process or supply external-writer compare-and-swap. Regular-file
reads can block in the filesystem, so this primitive provides no hard elapsed-time bound. Complete
helper delivery, locking, platform guarantees and production composition remain separate work.
