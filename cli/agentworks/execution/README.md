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

`result.py` defines immutable public outcome facts without wiring them into production execution.
Application progress and status precision remain separate from carrier dispatch evidence. Output
records raw bytes only when captured and distinguishes completeness from intentional delivery,
discard or suppression. `ExecutionResult.ok` requires proved zero completion, successful requested
output semantics, no recorded failure or expired deadline, and confirmed owned cleanup. `check()`
returns that same result or raises `CheckedExecutionError` carrying it; neither path invents a
scalar return code or copies provider exception text into result fields or the error message.

`files.py` defines frozen public file values without a FileAccess service. Opaque versioned
revisions preserve the observed object identity, metadata and optional content digest. Their
4,096-byte token bound limits metadata encoding, not file size. Public metadata must agree with its
revision; a read result also verifies its bytes against the revision's size and digest. Payloads and
tokens stay out of diagnostic representations. Directory limits and explicit publication conditions
are values, not evidence of remote effects or active permission grants.

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
bytes. An owned workstation-Python HTTP worker bounds each complete response to 8 MiB and consumes
the same deadline; timeout/interruption kills and reaps that worker, not the guest command. TLS
verification is mandatory. `ProxmoxConnection.ca_bundle` accepts an explicit PEM CA-bundle `Path`;
`None` uses the system/default trust context. An explicit bundle selects that trust source, not an
additional trust bypass. The API hostname must match the certificate; there is no server-name
override. Redirects and ambient proxies are disabled, and provider exception text is not returned.

`binding.py` carries a native carrier, its actual delivery account and explicit runtime selection.
The private platform resolver is an explicit preparation operation with a required deadline, so a
provider can bound any endpoint reads before constructing passive target views. Proxmox and WSL2
already have their endpoint facts: their resolvers bind Proxmox QGA as root and WSL2 as the recorded
admin account without provider reads, route activation, process launch or target probes. They do not
resolve the requested workload identity or prove elevation. The Proxmox binding rejects disabled TLS
verification; current CLI callers still use the unchanged legacy native hook. Other platform
bindings, provisioning-result adoption and production target composition remain unimplemented.

These two resolvers do not call legacy transport constructors. This is narrower than whole package
import independence: existing VM-platform and plugin initializers still load other providers with
legacy dependencies. Complete factory composition and eventual package retirement must remove those
dependencies; a test of an already-imported binding is not fresh-process startup proof.

`_process.py` supplies the standard-library-only process pump for workstation use and destination
helper composition. `carriers/_subprocess.py` maps carrier input, retention and result policy around
that core without changing its call signature. The core is compatible with Python 3.11 on POSIX;
non-blocking Windows pipes require Python 3.12 or newer. Sharing this code does not itself implement
guest delivery, framing or workload supervision.

The pump supports finite or explicitly enabled live input, separate bounded outputs, explicit
environment binding and bounded local cleanup. Borrowed byte endpoints must return without waiting
on external I/O. The pump never closes them or changes their descriptor flags. Short sink writes
retain a bounded pending suffix; temporary stalls are not EOF. Endpoint failures preserve
independently observed completion and mark incomplete delivery. After child exit, pending sink
delivery pauses all fresh pipe collection. Once both pending chunks clear, collection resumes
against its accumulated budget; the original deadline separately bounds sink delivery. A stalled
sink cannot exempt fresh reads from the other stream's collection budget.

`SinkOutput` feeds transient raw carrier bytes to trusted collectors and reports `DELIVERED` with no
retained bytes, including on sensitive calls. These are private preparation endpoints, not plugin
callbacks or permission to display sensitive output. Existing sensitive capture/discard calls still
suppress retained bytes. A carrier must explicitly enable live I/O in the pump; otherwise live input
or a sink requiring live delivery refuses before process creation. Buffered Proxmox observations can
feed the same sink interface without advertising live I/O, retaining truncation and deadline facts.

The pump's result records local process facts with unknown stream provenance; each carrier owns
interpretation as delivery evidence. It does not infer guest dispatch or termination. The SSH code
in this branch still uses its private buffered pump; its owning implementation lane has adopted the
shared pump separately. The buffered adapter refuses extended input/output modes before connection
admission or dispatch. Joint SSH byte-I/O proof remains required before enabling them in SSH. No
terminal support is enabled by these types.

On POSIX, the shared pump records terminal status from exact-child `waitpid` observations. A lost
wait owner produces unknown status and observation failure, never a guessed zero exit. Once that
loss is observed, cleanup does not signal or wait on the numeric PID. An external concurrent reaper
can still create an exit/reuse race before loss is observed; this change does not establish
exclusive process ownership. Windows retains handle-backed `Popen` waiting.

The private pump now uses a default-deny launch owner. A native thread starts with only an admission
cell; an interrupted, unacknowledged start cancels that cell without releasing command data or
dispatching work. Once admitted, the owner constructs, observes and cleans up the exact child while
the caller pumps its borrowed byte endpoints. A caller-side guard covers startup, admission and
pumping, but does not make asynchronous interruption atomic. On handled cleanup paths, control-flow
exceptions propagate after admitted ownership is settled or incomplete cleanup is reported. The
owner relinquishes command data and pipe capabilities before publishing its terminal observation. An
inert bootstrap or final native-thread return tail may finish later, but cannot use borrowed
endpoints or launch work.

`LocalProcessOwner` exposes that same private ownership mechanism independently of byte pumping.
Construct it before dispatch, call `start(LocalProcessRequest(...))` once, and observe immutable
snapshots. One caller serializes start and close; other threads may read snapshots. Published pipes
remain borrowed until every reader/writer has stopped, after which `close()` relinquishes them and
returns stable terminal facts. A terminal with `admitted=False` records canceled admission without
waiting for an inert bootstrap. Natural exit remains observable with stdin held open; closing stdin
alone is EOF, not owner close. Status first learned during cleanup is never natural-exit evidence.
The ordinary pump uses this interface. SSH forwarding adoption remains with its owning lane, which
must stop and join its pipe users before closing the common owner.

Local Linux tests exercise interrupted startup, admission and cleanup, including the interval after
admission but before pumping. They do not establish native Windows/macOS acceptance or update the
existing SSH-private copy. A separately reproduced SIGINT at entry to the cleanup loop can escape
before the owner receives its stop request, leaving a live child and open pipes. This remains an
open production gate; adding nested Python guards does not establish interrupt-atomic cleanup. A
deadline consumes startup time but cannot interrupt an OS process-creation call that has not
returned; it is not a hard real-time bound over that call. This ownership mechanism does not contain
descendants or cancel a guest workload.

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
and distribution lifetime require proof, including native acceptance of the shared launch owner.

## Observation and guest lifetime

A deadline bounds local observation only. Ordinary guest commands and bootstrap descendants can
remain running after return on both SSH and QGA, including after the initiating connection ends.
This proof has no guest cancellation handle, runtime timer or reaper. Do not retry an uncertain
command on the assumption it stopped. Production use is not enabled; it requires the separate
owned-workload lifecycle/cancellation implementation. Test only bounded workloads under explicit
cleanup authority, and verify their guest-side cleanup independently.

## Input accounting

The 262,144-byte preparation bound applies to the complete encoded envelope, not raw application
stdin. Script source, argv, environment, cwd and framing share it; base64 expands payload bytes.
Native delivery separately limits both the input field and the complete serialized HTTP body to
65,536 bytes. The latter includes bootstrap argv, JSON framing and escaping and preserves
compatibility with older supported Proxmox HTTP servers. Neither limit is a raw-stdin allowance.

After `prepare(...)`, `len(prepared.io.input.data)` gives the exact encoded size (the input is a
`FiniteInput`), but this alone cannot establish native request acceptance. The carrier also measures
the complete JSON body and raises `ValidationError` before dispatch if either bound is exceeded.
Preparation itself rejects an envelope over 262,144 bytes. This bounded proof has no automatic
chunking or staging fallback for larger work; it is not the eventual public script/file transfer
contract.

## Scope and checks

The carrier does not demote QGA's root identity or grant permission to use it. Only an explicitly
authorized proof composition may construct it. Shared identity/elevation binding, permission-scoped
access, complete platform coverage and production integration remain incomplete; the private helper
plans below do not enable production use.

Run the local evidence from `cli/` with `uv run pytest tests/execution`. The reusable vectors in
`tests.execution.conformance` require an explicitly supplied carrier. Their local process oracle
does not establish SSH or live Proxmox compatibility. The native carrier is isolated from the plugin
registry because importing that registry currently loads legacy execution modules.

## Private helper identity plans

Buffered inline execution and file exchanges take one `IdentityPlan`, binding the expected account
to an explicit transition. `DIRECT` uses the delivery identity, `SUDO_ROOT` selects UID 0 through
non-interactive sudo, and `DEMOTE` uses fixed `setpriv` arguments for a non-root UID, primary GID
and normalized groups. Demotion clears inheritable/ambient capabilities, not the bounding set or
permission to gain privileges later. Protection profiles are separate. Invalid plan combinations
refuse before payload preparation; a failed wrapper never triggers fallback or replay.

The fixed launcher gives Python a minimal environment. Trusted numeric identity metadata may appear
in wrapper argv; workload source, paths, environment and input remain in stdin. Linux helpers verify
real/effective/saved IDs and normalized groups before workload access. A non-Linux destination with
a compatible interpreter returns closed refusal evidence; an interpreter that cannot start cannot
provide that evidence. The host import remains workstation-neutral. Actual sudo/root-demotion
acceptance and native integration remain open; these private plans are not grants and do not
activate permissions.

`_account.resolve_account` discovers a core-bound account's UID, primary GID and normalized groups
through one read-only carrier attempt under the delivery identity. `RuntimeSelection` explicitly
binds the destination OS and an optional sole interpreter path. Linux otherwise selects
`/usr/bin/python3`; Darwin checks `/opt/homebrew/bin/python3` then `/usr/local/bin/python3`. The
first existing entry wins, including a broken link; an unusable selection never triggers fallback.
Darwin rejects aliases of the system Python shim without executing it. Selection installs nothing
and does not invoke developer-tools discovery. Native Darwin acceptance remains open.

In that same invocation, an isolated Python trampoline checks version 3.11 or newer and the common
bundle-loader imports before entering the account helper. One bounded nonce-bound prerequisite
record precedes the account response; selection and admission leave sensitive stdin unchanged. The
helper changes no credentials and creates no files. It queries the Linux or Darwin account database
without exporting passwords, account descriptions, home directories or shells.

The result separates the runtime prerequisite observation, carrier facts and optional account
observation. A ready prerequisite record admits the loader, not account success. Missing, shim,
unusable, unsupported-version and missing-module records give closed diagnostics; absent or invalid
records remain unknown. A complete received prerequisite refusal survives a concurrent carrier input
failure, but establishes neither process quiescence nor permission to retry. The account observation
is absent unless runtime admission occurred. The seven file families and buffered inline execution
use the same prerequisite boundary. Private terminal preparation uses the same selector and record
decoder with the terminal-specific framing described below.

Both request and reply are bounded to 32 KiB. A complete, nonce-bound response and complete
delivered streams are required to return identity metadata. Missing accounts and closed helper
refusals remain distinct from invalid or incomplete observation; raw status is separate. Raw
diagnostics are dropped and private response buffers are cleared after the attempt. Discovered
groups can differ from a running process's inherited groups, so lookup does not replace the launch
helper's actual identity check or grant authority. This private operation is not production
RunContext composition.

`_account.resolve_file_ownership` uses the same fixed helper for a separate owner/group pair lookup.
It returns only numeric UID/GID, resolving the named group independently of the owner's primary or
supplementary groups. Missing owner and missing group have distinct closed refusals. Names remain in
sensitive stdin, and the response kind must match this operation. Lookup does not change the
execution identity, select elevation or grant permission to apply the resulting metadata.

`_target_identity.prepare_target_identity` privately composes those account observations under one
already-acquired operation owner. It resolves delivery and workload identities, reusing an identical
name only within that call, and resolves root only for a requested non-root sudo plan. Every actual
carrier call is armed through the shared fixed-helper admission adapter and uses the original
deadline. A plan requires a complete resolved observation, sent dispatch, normal helper exit and
remaining budget for every lookup. Lookup facts never prove that a later transition will succeed.

Ordinary preparation uses direct entry only for identical numeric identities and otherwise permits
only root delivery demotion to a non-root workload. Explicit root preparation uses direct entry for
root delivery or non-interactive sudo when the non-root delivery and workload identities match.
Other cross-identity paths are closed refusals. Results retain each attempted account exchange and
safe deadline, termination and ownership-retention facts without retaining account names in their
diagnostic representation. The composer releases its serial borrow but does not close or recover the
enclosing owner, activate a route, launch a workload or expose a public permission surface.

## Private inline preparation

The private `_inline` candidate composes one carrier attempt with a fixed, standard-library-only
Python helper. Fixed inline and terminal helpers share `_helper_bundle.py`, which compresses their
packaged modules with standard-library `bz2` and ASCII-armors them to reduce carrier request size.
Only core-selected packaged code uses that loader; it does not select modules from request data or
install guest files. The destination Python must provide `bz2`. Caller arguments, script source,
environment, working directory and finite stdin travel in the bounded stdin manifest, not helper
argv. On Linux, scripts use an inherited memory file separately from application stdin. The caller
must bind the expected destination identity and an explicit `RuntimeSelection`. The identity
transition encloses the shared selector, which checks Python 3.11 and common loader imports before
the bundled helper runs. A bounded prerequisite record precedes helper framing without consuming
application input or staging files.

The result separates prerequisite evidence, carrier facts and an optional helper observation.
Without READY admission, the helper observation is absent; READY alone proves neither application
entry nor success. The host validates bounded helper evidence and does not infer an eager start
acknowledgment. Preparation remains single-use and clears the prerequisite parser after the attempt,
including carrier exceptions. This candidate is not wired to production RunContext and does not yet
supply staging, terminal I/O or managed lifetime.

## Private file-helper delivery

Read, object, metadata, inventory, staging, snapshot and publication exchanges use
`FixedFileHelperBundle`. The command line contains a short bootstrap, not the packaged modules. One
sensitive finite stdin contains their fixed base64/bz2 prefix followed by the canonical operation
manifest. The bootstrap uses unbuffered reads for its core-fixed prefix length and verifies the
core-fixed SHA-256 before decoding or executing any module. The family helper reads the remaining
bytes as request data. Requests cannot choose executable source, module names, entry points, prefix
lengths or digests. No helper installation, executable staging, second invocation or codec fallback
is used.

Each file entrypoint requires a destination-bound `RuntimeSelection`. The shared identity wrapper
encloses runtime selection and admission, so the selector runs under the intended helper identity.
The version/import trampoline emits one binary prerequisite record before the bootstrap reads the
bundle. Results retain that prerequisite observation independently of carrier facts; file-operation
observations are absent unless admission is READY. Common loader readiness does not waive the
family's Linux/filesystem/identity checks or establish support for a new destination platform.

A short or changed prefix exits without file-protocol output. That missing observation does not
prove helper quiescence or authorize replay. Existing identity and path checks remain in each helper
before workload access. A received READY record is not file-operation success. Operation manifest
bounds exclude the fixed prefix; carriers also enforce their complete request bound, including the
prefix and command serialization. Local Python and serialized Windows/QGA checks do not establish
native carrier acceptance or public FileAccess composition.

## Private inline file reads

`_file_read.py` composes a bounded, identity-bound Linux file read through one carrier attempt. The
core selects a trusted root and relative path; the helper checks the expected UID, GID and groups
before opening the target. Absolute root traversal uses path-only descriptors and refuses links
without requiring directory read permission. The existing snapshot reader also refuses descendant
mount crossings and unsupported objects. Missing roots or files produce an absent observation, not
an I/O success with empty bytes.

Workload paths and request payload travel only in sensitive stdin. `_file_wire.py` supplies the
shared bounded `AGWF1` record framing; the read schema remains concrete. A file-specific collector
validates the complete nonce-bound response, length, digest and metadata before releasing bytes.
Noise, reflection, truncation or invalid records cannot become a successful read, and carrier
completion is reported separately. Helper code uses the fixed module packager; it creates no guest
files, spool or lock. The destination needs compatible Python 3.11 or newer with `bz2`, not a
privileged lock namespace. The caller must serialize conflicting operations.

The host exposes one private `read_file` call, preparing and dispatching one attempt with the
current remaining deadline. The guest derives its own monotonic expiry and materializes a bounded
immutable snapshot. It checks expiry before reporting either a snapshot or absence and before
emitting file bytes. There is no reusable prepared-read object or readiness-time setup. The budget
bounds cooperative checks, not an individual blocked filesystem system call.

An exceptional exit clears collector-owned response state and the reader's partial record before
propagating the exception. This is not secure erasure of Python memory or traceback locals; callers
must not render private frame locals. Snapshot bytes and metadata remain hidden from result
representations.

This is not production FileAccess or native platform acceptance. This read helper does not provide
mutation, macOS support or a hard elapsed-time bound for filesystem reads. Metadata-only stat uses
the separate object exchange below. Both assume a cooperative execution identity, not hostile
same-user isolation.

`_file_memory_read.py` supplies a separate private in-memory read over the owned snapshot/chunk
download. The caller's byte limit bounds the snapshot, independently of a carrier's single-response
capacity. Only a complete verified download with completed cleanup returns bytes; absence and
failures retain the original download outcome without exposing partial data. Its temporary buffer is
cleared on every exit, which is not secure memory erasure. This adapter introduces no retry,
fallback or additional claim and is not the no-staging readiness path. Production FileAccess and
core recovery handoff remain separate integration work.

## Private terminal handoff preparation

`_terminal_handoff.py` provides platform-neutral host preparation for one no-staging, same-terminal
bootstrap attempt targeting the Linux `_terminal_guest.py`. It requires explicit `RuntimeSelection`
and observes runtime readiness before accepting the helper's payload-ready marker. Only that second
gate releases one bounded frame from the host byte source. That frame keeps literal byte argv,
environment and source off helper argv. The guest reads it with echo and terminal input
transformations disabled, installs source on a Linux memory descriptor separate from terminal stdin,
restores the terminal, then emits a distinct nonce-bound interactive-ready marker. Only then does
the host source end preparation and permit a future carrier adapter to borrow keyboard input. The
paired host sink suppresses setup and readiness bytes, handles split and coalesced markers, and
forwards only bytes after interactive readiness to an explicitly selected trusted presentation sink
with short-write flow control. Before exec, the one-shot guest resets Python-ignored pipe and
file-size signals to their operating-system defaults without changing unrelated signal dispositions.

The initial terminal settings can transform the runtime record before the helper enters raw mode.
The sink accepts only its nonce-bound canonical or entirely uppercase control record, with LF or
CRLF, and suppresses preceding setup noise. It normalizes that bounded record alone; pipe parsing
and application output remain unchanged. A malformed bound record fails closed. Complete readiness
or refusal evidence survives later handoff failure; a refusal stops the endpoint without claiming
that all later terminal bytes were observed.

The preparation object has a single-use guard but does not dispatch or prove replay prevention by a
carrier. Its readiness markers establish only handoff state, never application launch or exec
evidence. Invalid ordering, truncated readiness and endpoint failures close the adapters without
retaining payload or presentation causes. The candidate is private and is not a `TerminalInput`
implementation or a production feature. SSH still owns native workstation PTY plumbing, terminal
metadata, keyboard borrowing, resize, restoration and the joint acceptance proof before enablement.
The future transport-owned execution wrapper must call the readiness sink's `finish()` when the
carrier attempt ends. Neither the carrier nor the generic byte-sink protocol performs that
finalization today; preparation-only tests are not production terminal acceptance.

## Private JSON transformation

`_json.py` supplies the local, bounded transformation for file-operation composition. It preserves
replace, skip-existing and both recursive object-merge strategies; arrays/scalars are atomic and
JSON null is a value, not deletion. Source validation precedes every strategy, while replace and
skip-existing do not parse old content. Byte and container-depth limits apply to parsed inputs and
the proposed serialized result. These are rejection thresholds, not overrides of the interpreter's
capacity. Standard-library nesting and integer-conversion limits can also cause a safe refusal even
within caller-selected bounds; the implementation does not change process-global interpreter limits.

The pure transformation returns proposed publication bytes or `None` for skip-existing, not evidence
of a filesystem change. `_file_json.py` composes destination stat/read and upload under one borrowed
core operation. Replace and skip-existing use metadata observation without reading old content.
Merges publish against the observed revision, retrying only confirmed publication condition
conflicts, with eight total attempts under the original deadline. Uncertain dispatch, missing
termination evidence or retained cleanup debt stops the workflow without replay.

Private outcomes distinguish change, failure and uncertainty and preserve the upload's recovery
facts without embedding JSON content. Public FileAccess, error normalization, production ownership
and recovery remain unimplemented; these private calls are not a production RunContext surface.

## Private file observations

`_file_snapshot.py` reads a bounded regular-file snapshot relative to a borrowed trusted root
descriptor. It refuses observed links, multiply linked files and special objects, reads in bounded
chunks, and binds the returned bytes to their digest and before/after metadata. The caller retains
the root descriptor and serializes conflicting operations. The reader creates no files or locks and
does not expose FileAccess, publication or permission enforcement.

On Linux x86-64 and AArch64, every descendant open uses `openat2` beneath the borrowed root with
symlink, magic-link and mount crossings forbidden. Unsupported kernels or architectures refuse
without a weaker fallback. Other POSIX platforms retain the separate component walk and `st_dev`
checks, which do not detect same-filesystem bind mounts. Neither path contains a malicious same-user
process or supplies external-writer compare-and-swap. Regular-file reads can block in the
filesystem, so this primitive provides no hard elapsed-time bound. Complete helper delivery,
operation coordination, platform guarantees and production composition remain separate work.

`read_revision` can observe regular-file metadata without retaining content or can include a
bounded-memory content digest. Linux metadata-only observation uses a path-only descriptor and does
not require file-read authority. `FileSnapshot.revision` carries the content-bound observation; its
`stat` and `digest` properties expose those same values without duplicating them.

`_file_spool.py` copies one held regular source into private scratch in bounded chunks, returning a
verified ready reference and the source's content-bound revision. It checks source length, EOF,
digest and held/named metadata before returning; later chunk reads use the private copy rather than
the public source. The caller serializes conflicting operations and owns both parent descriptors,
and supplies the fresh operation token and execution identity before copying. The immutable receipt
binds this operation to snapshot creation, not upload staging. Expiry is checked after source
descriptors close, including initial absence. Failure attempts exact scratch cleanup and preserves
unresolved cleanup debt. Local receipt reconciliation can recover cleanup ownership after a lost
return, not the ready snapshot or its content revision. The private snapshot exchanges below deliver
these operations; `_file_download.py` composes them under one borrowed operation owner.

Descriptor bookkeeping is not signal-atomic. An asynchronous interruption before an intermediate
ancestor close can leave that descriptor open until helper exit; callers cannot assume leak-free
reuse after catching arbitrary interruption. Retrying a numeric descriptor close without knowing
whether it already completed can instead close a reused descriptor. Complete helper lifetime and
interruption acceptance remain production gates.

## Private file publication

`_file_publication.py` supplies Linux same-directory publication beneath a borrowed parent
descriptor. It accepts bytes or a borrowed verified scratch source. `Create` requires absence,
`Replace` requires an existing regular file, and `Match` requires the supplied revision. A revision
binds metadata and optionally content; matching a content-bound revision hashes bounded chunks
without retaining the old file. Create-only publication uses `renameat2(RENAME_NOREPLACE)` without a
fallback. Replacement rechecks its observed condition before rename. The caller supplies
confinement, operation serialization and scratch cleanup. This primitive is not FileAccess or a
remote upload helper.

An exclusive sibling receives the content and required metadata before publication. Byte-backed
publication chooses a fresh random name; scratch-backed publication derives its name from the
core-generated scratch token and records its exact identity before copying content. New files retain
the directory's inherited ACL behavior with the requested owner, group and mode. Replacement
requires ordinary destination write authority and preserves UID, GID, permission bits and the Linux
access ACL. Read authority is additionally needed for a content-bound match, not unconditional
replacement. Observed links, multiply linked files, special objects, set-ID state and other visible
extended attributes refuse. Unsupported metadata is not silently dropped.

Scratch publication checks declared length and digest while copying bounded ranges, including
validation of empty sources. The caller retains ownership of the scratch object. Hash/copy loops
check a supplied guest-local monotonic deadline; a blocked filesystem call cannot be forcibly
cancelled by these checks. A failure after publication was attempted can be uncertain rather than
unchanged. Success returns a revision whose digest is rechecked against the held published file,
with stable before/after metadata and name binding. Observed post-rename content changes therefore
produce uncertainty instead of a revision carrying stale content evidence.

Errors carry closed kind/phase facts rather than filesystem messages or payloads. Cleanup concerns
only the recorded staging name and identity, never a prefix scan. Failed cleanup retains a private
debt value for exact-identity retry under the same parent; unknown identity requires inspection
instead of automatic removal. Control-flow exceptions propagate. When the publication boundary
handles an interruption, it attaches observed publication uncertainty or cleanup debt as a
`FilePublicationError` cause. This in-process primitive is not interruption-atomic: asynchronous
exceptions can race Python bookkeeping, including recording a descriptor returned by the kernel.
Repeated interruption has no bounded cleanup guarantee. Complete helper lifecycle and interruption
acceptance remain open.

Atomic visibility does not imply directory-entry crash durability, an external-writer
compare-and-swap guarantee or a hard filesystem deadline. Native-platform acceptance and the
complete file service remain separate work.

`_publication_receipt.py` supplies private recovery for scratch-backed publication stages. Admission
validates and holds the original STAGE scratch receipt and objects before creating a destination
artifact. A separate bounded immutable record inside that scratch directory binds the original
destination parent and exact sibling name/inode. Cleanup permits the publication algorithm's
intentional owner, group, mode and ACL changes, but refuses links or a replacement inode. Ordinary
scratch cleanup refuses the extra record instead of discarding the data or original receipt first.

Read-only reconciliation yields historical cleanup ownership, not permission to publish or proof
that publication happened. It validates the scratch directory and original receipt without requiring
the payload data to remain present. Missing siblings and missing, partial or invalid records remain
uncertain. Exact cleanup removes the sibling before its record; a handled failure removing the
record retains record-only cleanup debt. After independently observed publication, the publication
primitive removes the record without following the inode into the public destination. Losing that
reply still does not establish publication or remote quiescence.

`_file_publication_exchange.py` delivers `publish`, `publication_reconcile` and
`publication_cleanup` through one fixed Linux helper attempt each. Publication verifies the original
stage's content before applying Create, Replace or Match. The host accepts a content-bound revision
only from a complete nonce-bound transcript matching the original length and digest. Reconciliation
returns historical cleanup ownership only, including after the payload disappears; it cannot recover
publication authority. Cleanup accepts only debt bound to the original parent, token and stage
reference, and preserves exact remaining debt on failure. Missing identity remains uncertainty, not
permission to remove an object.

Carrier facts remain separate from file evidence. Lost, partial, noisy or substituted replies cannot
establish publication or cleanup, and no exchange retries automatically. A final descriptor-close
deadline flag can accompany confirmed publication, recovered ownership or completed cleanup without
erasing that effect. Requests and replies remain sensitive and bounded. Complete upload composition
lives separately in `_file_upload.py`; FileAccess, production operation admission and native carrier
acceptance remain unfinished.

## Private file coordination

File helpers have no destination-side machine-wide lock or privileged lock setup. Their caller owns
serialization for the entire logical operation, including snapshot/merge/publication, transfer and
cleanup. Unique scratch names prevent transfer collisions, not conflicting final-destination writes.
The helpers retain conservative object checks and revision validation but do not supply atomic
compare-and-swap against external writers.

Production composition must provide database-backed operation ownership across participating core
operations and retain unresolved ownership after loss of remote observation. That composition is not
enabled here. These private helpers are not safe to expose as an uncoordinated public file service.
Neither a caller crash nor a missing scratch receipt proves remote mutation has stopped.

`agentworks.operations.OperationOwner` wraps one exact database claim with a serial borrow for the
whole nested operation. Its first attempt commits possible dispatch before returning permission to
send work. Later attempts reuse the durable claim. Explicit close stops admission and refuses while
a borrow or unresolved attempt remains; it does not infer remote quiescence from local return.

`_file_operations.py` uses the caller's active borrow for bounded inline read, stat, inventory,
conditional removal and metadata composition. Metadata name lookup and mutation share one borrow and
deadline; successful lookup and normal helper termination are required before mutation. Candidate
observations are recorded before settlement, independently of expiry and unresolved ownership. This
composition, upload, download, JSON and memory reads neither acquire nor close their borrow,
including on escaping control flow. The caller keeps serial ownership through private-outcome
capture before relinquishing it. These helpers do not release the database claim, replay a failed
request or provide public FileAccess error reduction. Complete core outcome custody and durable
recovery remain integration work; the borrow parameter alone does not implement them.

`_file_operation.FileOperation` privately composes one concrete download under a caller-supplied
outer owner. It attaches validated working state, including the token and original carrier/binding,
before dispatch. It captures returned or exceptional outcomes before relinquishing the borrow;
unfinished records retain their exact facts without retaining the sink or its payload. Multiple
unfinished records can coexist, and each completed call removes only its own active record. Outcome
retention precedes borrow release; a retention failure leaves the working record and borrow in
place. Pre-dispatch validation refusal releases its unused borrow.

This custody path adds no claim or admission lock and never closes the outer owner. Retaining an
unfinished record does not establish remote quiescence or authorize claim release. It covers private
download calls, not complete user/admin FileAccess views, other file operations, durable crash
recovery or production RunContext binding. Python bookkeeping is not signal-atomic, and an in-memory
token is not a durable pre-dispatch recovery record.

`_file_upload.py` composes staging, finite source consumption, publication and ordered cleanup under
one borrowed owner. It consumes bounded chunks without rewinding or retaining the whole source and
checks exact EOF before publication. Follow-on calls require independently observed normal-zero
completion of the supported helper chain that starts no background work, or evidence that the
previous attempt was not sent. Missing completion stops further calls, including cleanup.
Publication evidence, remaining cleanup obligations and possible future effects are distinct. The
caller retains the original binding and token for unresolved work. These private mechanics do not
acquire production ownership before activation or provide crash recovery.

`_fixed_helper_operation.py` owns the concrete dispatch gate shared by account preparation, upload,
download and JSON composition. Only the outer workflow closes its borrow; a nested upload cannot
release the JSON operation's ownership between observation and conditional publication.

`_file_download.py` creates one private source snapshot, streams verified chunks to a borrowed byte
sink, and cleans up the exact snapshot under the same owner. Only complete chunk observations with
independent normal-zero completion reach the sink. Short writes and temporary stalls consume the
original deadline; the sink is never closed by the coordinator. The final byte count and digest must
match the source revision, including for empty files. Confirmed absence returns no bytes.

The outcome separates accepted bytes, whole-stream verification, remote cleanup debt and possible
future effects. A verified stream is not complete while required cleanup remains unresolved. Sink
failures retain closed facts, not raw exception text; escaping control flow carries bounded recovery
facts. Private completion and absence can coexist with `deadline_exceeded`; callers must preserve
that timing fact rather than interpret the status alone as in-budget success. Upload and JSON
composition retain the same independent timing fact. This private entry requires a finite positive
source bound. Public optional bounds, local staging/publication and local cleanup remain
unimplemented; the coordinator cannot publish a local file or provide public FileAccess on its own.

## Private object observation and removal

`_file_objects.py` observes a supported Linux regular file, directory or Unix socket without
requiring content-read permission. It uses confined path-only descriptors and checks named/held
identity and metadata. Observed links, multiply linked regular files and other special objects
refuse. Removal requires exact kind and revision; digest-bearing regular revisions also recheck
content through the bounded revision reader. Only empty directories can be removed. Initial absence
returns unchanged; interrupted or ambiguous mutation is not reported as unchanged.

The caller owns the trusted parent and operation serialization. Removal neither checks tmux liveness
nor provides atomic compare-and-remove against external writers. Session code must coordinate server
absence before supplying a socket revision. No recursive removal or FIFO creation is exposed.

`_file_object_exchange.py` delivers stat or conditional removal through one fixed, inline Python
helper attempt. After validating its request, nonce and bound identity, the guest opens the trusted
root without a lock-setup prerequisite. Paths and expected revisions travel only in sensitive stdin.
The request carries a remaining duration from which the guest derives its own monotonic expiry.

The host accepts only a complete, nonce-bound `AGWF1` result matching the requested operation.
Carrier completion remains separate from object evidence. Lost, malformed or noisy observation after
a possibly dispatched removal is uncertain, never permission to retry. A complete helper report of
an uncertain mutation preserves its closed failure kind and phase. No raw diagnostic bytes are
retained. Stat returns metadata only; it does not read file contents or stage state.

The fixed operation bundle is checked against the complete Proxmox HTTP request bound. Local
isolated-interpreter fixtures exercise the real confined operations under a test-owned root without
an installed lock namespace. Serialized Windows command sizes are checked separately. These tests do
not establish privileged or cross-identity native acceptance or public FileAccess composition.

## Private metadata convergence

`_file_metadata.py` converges regular-file or directory ownership and mode on a held Linux inode. It
verifies the fixed procfs descriptor bridge instead of reopening the caller's mutable path;
unavailable or incompatible procfs refuses. Ordinary attributes remain in place, and access-ACL
permissions are checked against the resulting mode. Directory modes can include set-group-ID and
sticky bits; regular-file modes are ordinary permissions only. Socket metadata and set-user-ID
requests are not supported.

`ensure_directory` creates only the final component, initially with mode 0700, then converges it. It
does not remove a created public directory if a later step fails. Errors distinguish unchanged,
confirmed partial changes and uncertain attempts, retaining closed completed-step facts. The caller
owns path validation, the trusted parent and operation serialization. These private functions
provide neither a public file service nor native or elevated acceptance.

`_file_metadata_exchange.py` delivers `set_file_metadata` and `ensure_file_directory` through one
sensitive finite-input carrier attempt. The fixed helper verifies execution identity, confines the
parent and leaf, and emits its result after owned descriptor cleanup. Numeric metadata ownership is
separate from execution identity. Missing parents refuse; directory creation does not create
intermediate components. Complete failure records preserve known changes and uncertain attempts;
lost observation after possible dispatch remains uncertain and never triggers replay. A control
interruption carries a closed mutation-uncertainty marker.

## Private directory inventory

`_file_inventory.py` reads a bounded Linux directory inventory beneath a borrowed trusted root and
caller-owned operation serialization. It observes regular files, directories and sockets without
reading file content, refusing links, multiply linked regular files, unsupported special objects and
descendant mount crossings. Depth one returns immediate children; a directory at the requested depth
is observed but its children are not enumerated.

Results are sorted by relative UTF-8 path bytes. Entry, name and encoded-output limits fail rather
than returning a truncated result. The shared encoder defines the exact compact JSON byte count,
including framing. Disappearance or observed replacement conflicts. The result is a bounded set of
observations, not a coherent snapshot against external writers.

`_file_inventory_exchange.py` delivers one `list_directory` request using sensitive finite input and
delivered-output sinks. Its fixed helper checks identity and encodes the bounded inventory before
emitting frames. The caller serializes conflicting operations. Missing targets remain distinct from
empty directories. The host returns entries only after verifying complete framing, byte length,
digest, entry schema and both carrier streams. Neither exchange implements public `FileAccess`
composition or establishes native platform acceptance.

## Private scratch transfer

`_scratch.py` supplies POSIX destination-side scratch operations beneath a borrowed trusted parent
descriptor. Core supplies a fresh 16-byte token before dispatch; the helper derives one exact
private directory name and attempts exclusive creation once. The directory contains fixed data and
receipt files, with final modes 0700, 0600 and 0400 respectively. An identity-bound private
reference carries its declared length. Final verification receives the whole-object SHA-256 after
transfer and produces a ready reference carrying the verified digest; beginning an upload does not
require consuming its source first. Operations reopen and check the recorded objects; observed
replacement, links, special objects or changed ownership/mode refuse. The caller owns confinement
and coordination between writers.

`_scratch_receipt.py` binds that token to the closed stage/snapshot operation, execution identity,
original parent, declared length and exact acquired objects in a bounded immutable receipt. Active
access checks that receipt before using scratch. Read-only reconciliation uses the original token
and context under the caller's operation ownership, never a path recovered from disk. A valid
receipt can recover historical ownership for exact cleanup even when data is incomplete or already
removed; it cannot recover a ready content reference or authorize publication. Missing, partial or
invalid receipts remain ownership uncertainty, not proof of absence. An active reference also binds
the original receipt inode; historical recovery has no prior receipt inode to compare. No prefix
search, replay, transfer registry or reboot-durability guarantee is supplied.

Writes use bounded exact offsets and a chunk digest. An exact previously written range may be
retried after comparing its bytes; gaps, conflicting duplicates and partially overlapping chunks
refuse. Whole-object length and digest verification precedes bounded reads. The 24 KiB raw chunk
limit is an internal candidate, not evidence that a complete encoded carrier request fits.

Cleanup removes the exact data object before its receipt, then the empty directory, never unknown
neighboring objects or a recursive prefix match. Errors retain closed facts and unresolved
identity-bound cleanup debt. Ownership does not prove that an earlier request can no longer arrive;
remote dispatch ordering remains a separate implementation gate. Begin, chunk writes, verification
and range reads check caller expiry at acquisition, transfer and final-evidence checkpoints.
Publication preserves scratch deadline failures. Expiry stops further acquisition, but permits
identity capture and mode normalization needed for bounded exact cleanup; failed cleanup remains
explicit debt. Creation preserves known acquisition facts through handled control-flow interruption;
an unresolved debt is attached as a closed error cause while the original control exception
propagates. Python ownership bookkeeping is not signal-atomic and repeated interruption is not a
bounded cleanup guarantee.

This primitive does not implement remote request validation, a wire protocol, helper deployment,
execution staging or FileAccess. Filesystem calls have no hard interruption bound, and no crash
durability or hostile same-user race guarantee is made. Local Linux fixtures are not native macOS or
carrier acceptance.

## Private file staging exchanges

`_file_stage_exchange.py` delivers stage creation, exact-offset chunks, ownership reconciliation and
exact cleanup through a fixed Linux helper. Each request carries the original trusted root and
nonempty destination path; the guest derives the scratch parent rather than accepting a path from a
receipt. It checks execution identity before opening workload paths. The caller serializes
conflicting operations; missing or unsafe parent state refuses without setup or repair.

The guest checks expiry after closing its owned path descriptors, before emitting the result. If
creation or a chunk write already happened, expiry retains exact cleanup debt rather than claiming a
no-effects refusal.

Tokens, paths, file bytes and references travel only through sensitive stdin. A stage-specific
collector accepts an active reference or known cleanup debt only after complete nonce-bound framing
and complete delivered streams. A creation result must match the declared length. Lost, malformed or
noisy observation after possible dispatch is uncertain and never triggers replay. A chunk failure
does not publish content; the caller retains its original reference for later exact cleanup.
Returned chunk scratch-failure debt must match that reference exactly, including receipt mode;
missing or conflicting debt is invalid control, not new cleanup authority.

Reconciliation returns historical cleanup ownership only, never an active or ready content
reference. It requires the complete recorded parent, directory, data and receipt identities and the
final receipt mode. Missing or invalid receipts remain ownership uncertainty. Cleanup consumes the
original identity-bound debt; a failure cannot substitute different debt. Explicit cleanup checks
expiry after parent resolution and before its first deletion. Neither recovery nor cleanup proves
that an earlier unobserved request can no longer arrive; delayed chunk requests must still validate
their receipt and cannot recreate a cleaned stage.

The private stage chunk cap is 12 KiB, below the scratch primitive's 24 KiB range cap. The complete
manifest is limited to 32 KiB, and Proxmox independently enforces its full serialized body limit.
Local tests exercise the actual request serializer, a fake provider executing the real helper, and
Windows SSH command-line sizing for explicit fixtures. Those measurements do not establish native
platform acceptance or fit for every connection/identity prefix.

These entries are individual exchanges, not FileAccess operations. Private upload composition uses
them through `_file_upload.py`; downloads use the separate snapshot exchanges below. The caller must
retain the token, original binding and known references; an unavailable creation reply does not
establish absence or quiescence.

`_scratch_root.py` opens the fixed Linux `/tmp` directory without following symlinks and requires
UID 0 and mode 01777. It creates and repairs nothing, ignores environment-selected temporary paths,
and returns a caller-owned descriptor or a closed failure kind. Download snapshots can use this
parent independently of read-only source authority. This private selection grants no access to
arbitrary temporary-directory contents. macOS root selection is not implemented.

## Private file snapshot exchanges

`_file_snapshot_exchange.py` delivers snapshot creation, exact-range retrieval, historical ownership
reconciliation and cleanup through a fixed Linux helper. The helper checks the execution identity
before opening either the source or the fixed scratch parent. It copies the held source into private
scratch once; later requests read that copy, not a changing source file. Source-read authority does
not require write access to the source directory.

Requests use sensitive stdin with a 32 KiB manifest bound. Chunk replies carry at most 12 KiB of
binary data in `AGWF1` records, followed by range, length and digest evidence. The host releases
typed results only after complete nonce-bound framing and delivered streams. A missing source root
or file is distinct from an empty file; invalid, reflected or incomplete output does not establish
either.

Reconciliation recovers cleanup ownership only, never ready content or proof that an earlier request
has stopped. Known cleanup debt survives deadline failure. Cleanup accepts the original token and
identity-bound objects; delayed chunks refuse after receipt removal. The caller must serialize the
complete logical operation and retain its token and known references. `_file_download.py` supplies
that private composition; local publication and public FileAccess remain unimplemented. Local helper
tests and serialized request-size measurements do not establish native carrier acceptance.
