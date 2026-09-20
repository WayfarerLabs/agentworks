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

Buffered inline execution and file reads take one `IdentityPlan`, binding the expected account to an
explicit transition. `DIRECT` uses the delivery identity, `SUDO_ROOT` selects UID 0 through
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
through one read-only carrier attempt under the delivery identity. It accepts an already-selected
compatible Python path, changes no credentials and creates no files. The account name stays in
sensitive stdin. The fixed helper queries the Linux or Darwin account database; it does not export
passwords, account descriptions, home directories or shells. Native Darwin acceptance remains open.

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

## Private inline preparation

The private `_inline` candidate composes one carrier attempt with a fixed, standard-library-only
Python helper. Fixed inline and terminal helpers share `_helper_bundle.py`, which compresses their
packaged modules with standard-library zlib and ASCII-armors them to reduce carrier request size.
Only core-selected packaged code uses that loader; it does not select modules from request data or
install guest files. The destination Python must provide zlib. Caller arguments, script source,
environment, working directory and finite stdin travel in the bounded stdin manifest, not helper
argv. On Linux, scripts use an inherited memory file separately from application stdin. The caller
must bind the expected destination identity. The host validates bounded helper evidence and keeps
carrier status separate; it does not infer application success or an eager start acknowledgment.
This candidate is not wired to production RunContext and does not yet supply staging, terminal I/O
or managed lifetime.

## Private inline file reads

`_file_read.py` composes a bounded, identity-bound Linux file read through one carrier attempt. The
core selects a trusted root and relative path; the helper checks the expected UID, GID and groups
before acquiring the existing fixed system file lock or opening the target. Absolute root traversal
uses path-only descriptors and refuses links without requiring directory read permission. The
existing snapshot reader also refuses descendant mount crossings and unsupported objects. Missing
roots or files produce an absent observation, not an I/O success with empty bytes.

Workload paths and request payload travel only in sensitive stdin. `_file_wire.py` supplies the
shared bounded `AGWF1` record framing; the read schema remains concrete. A file-specific collector
validates the complete nonce-bound response, length, digest and metadata before releasing bytes.
Noise, reflection, truncation or invalid records cannot become a successful read, and carrier
completion is reported separately. Helper code uses the fixed module packager; it creates no guest
files, spool or lock. The destination needs compatible Python 3.11 or newer with zlib and the
protected lock namespace already available. Missing or unsafe lock state refuses even if the
requested file is absent.

The host exposes one private `read_file` call, preparing and dispatching one attempt with the
current remaining deadline. The guest derives its own monotonic expiry and holds the lock until its
bounded immutable snapshot is materialized. It checks expiry before reporting either a snapshot or
absence, then releases the lock before emitting file bytes. There is no replayable prepared-read
object or readiness-time setup. The budget bounds cooperative checks, not an individual blocked
filesystem system call.

An exceptional exit clears collector-owned response state and the reader's partial record before
propagating the exception. This is not secure erasure of Python memory or traceback locals; callers
must not render private frame locals. Snapshot bytes and metadata remain hidden from result
representations.

This is not production FileAccess or native platform acceptance. This read helper does not provide
mutation, macOS support or a hard elapsed-time bound for filesystem reads. Metadata-only stat uses
the separate object exchange below. Both assume a cooperative execution identity, not hostile
same-user isolation.

## Private terminal handoff preparation

`_terminal_handoff.py` provides platform-neutral host preparation for one no-staging, same-terminal
bootstrap attempt targeting the Linux `_terminal_guest.py`. A nonce-bound payload-ready marker
releases one bounded frame from the host byte source. That frame keeps literal byte argv,
environment and source off helper argv. The guest reads it with echo and terminal input
transformations disabled, installs source on a Linux memory descriptor separate from terminal stdin,
restores the terminal, then emits a distinct nonce-bound interactive-ready marker. Only then does
the host source end preparation and permit a future carrier adapter to borrow keyboard input. The
paired host sink suppresses setup and readiness bytes, handles split and coalesced markers, and
forwards only bytes after interactive readiness to an explicitly selected trusted presentation sink
with short-write flow control. Before exec, the one-shot guest resets Python-ignored pipe and
file-size signals to their operating-system defaults without changing unrelated signal dispositions.

The preparation object has a single-use guard but does not dispatch or prove replay prevention by a
carrier. Its readiness markers establish only handoff state, never application launch or exec
evidence. Invalid ordering, truncated readiness and endpoint failures close the adapters without
retaining payload or presentation causes. The candidate is private and is not a `TerminalInput`
implementation or a production feature. SSH still owns native workstation PTY plumbing, terminal
metadata, keyboard borrowing, resize, restoration and the joint acceptance proof before enablement.

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

On Linux x86-64 and AArch64, every descendant open uses `openat2` beneath the borrowed root with
symlink, magic-link and mount crossings forbidden. Unsupported kernels or architectures refuse
without a weaker fallback. Other POSIX platforms retain the separate component walk and `st_dev`
checks, which do not detect same-filesystem bind mounts. Neither path contains a malicious same-user
process or supplies external-writer compare-and-swap. Regular-file reads can block in the
filesystem, so this primitive provides no hard elapsed-time bound. Complete helper delivery,
locking, platform guarantees and production composition remain separate work.

`read_revision` can observe regular-file metadata without retaining content or can include a
bounded-memory content digest. Linux metadata-only observation uses a path-only descriptor and does
not require file-read authority. `FileSnapshot.revision` carries the content-bound observation; its
`stat` and `digest` properties expose those same values without duplicating them.

## Private file publication

`_file_publication.py` supplies Linux same-directory publication beneath a borrowed parent
descriptor. It accepts bytes or a borrowed verified scratch source. `Create` requires absence,
`Replace` requires an existing regular file, and `Match` requires the supplied revision. A revision
binds metadata and optionally content; matching a content-bound revision hashes bounded chunks
without retaining the old file. Create-only publication uses `renameat2(RENAME_NOREPLACE)` without a
fallback. Replacement rechecks its observed condition before rename. The caller supplies
confinement, the cooperating-writer lock and scratch cleanup. This primitive is not FileAccess or a
remote upload helper.

An unpredictable exclusive sibling receives the content and required metadata before publication.
New files retain the directory's inherited ACL behavior with the requested owner, group and mode.
Replacement requires ordinary destination write authority and preserves UID, GID, permission bits
and the Linux access ACL. Read authority is additionally needed for a content-bound match, not
unconditional replacement. Observed links, multiply linked files, special objects, set-ID state and
other visible extended attributes refuse. Unsupported metadata is not silently dropped.

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

## Private file transaction lock

`_file_lock.py` borrows a trusted protected directory descriptor and opens its fixed `files.lock`
read-only. It refuses missing or unsafe state, acquires one exclusive Linux advisory lock with
deadline-bounded polling, checks that the name still binds the held object, and closes its owned
descriptor on exit. Each transaction opens a fresh descriptor. The lock is an empty mode-0444
single-link regular file owned by the trusted setup identity; its parent must have that owner and
must not be group/other writable. No operation creates, repairs, replaces or unlinks lock state.

`system_file_lock` opens the fixed `/var/lib/agentworks/execution` namespace with path-only
descriptors, checking root ownership and no group/other write authority at every ancestor. It
rejects observed links or replacement without requiring directory read permission. Setup and
local-filesystem locking semantics remain provisioning prerequisites, not results of this walk. The
lock must not enclose child creation: a fork can inherit the descriptor and prolong ownership. Local
contention and cleanup tests are not native macOS or ordinary/elevated acceptance. These private
entries provide the transaction boundary for the private read and stat/removal helper exchanges.

Debian new-guest bootstrap invokes `_file_lock_setup.py` through a fixed standalone bundle after
installing distribution Python. It provisions `/var/lib/agentworks/execution/files.lock` as root,
validates protected ancestors, preserves valid existing objects and refuses unsafe ones. Only new
core-owned objects have inherited ACLs removed and modes finalized; existing access ACLs refuse.
Setup leaves unrelated state alone. File operations and readiness never invoke setup. Existing-guest
recovery, cross-identity access and macOS host setup are not supplied by this create-time hook.

## Private object observation and removal

`_file_objects.py` observes a supported Linux regular file, directory or Unix socket without
requiring content-read permission. It uses confined path-only descriptors and checks named/held
identity and metadata. Observed links, multiply linked regular files and other special objects
refuse. Removal requires exact kind and revision; digest-bearing regular revisions also recheck
content through the bounded revision reader. Only empty directories can be removed. Initial absence
returns unchanged; interrupted or ambiguous mutation is not reported as unchanged.

The caller owns the trusted parent and transaction lock. Removal neither checks tmux liveness nor
provides atomic compare-and-remove against external writers. Session code must coordinate server
absence before supplying a socket revision. No recursive removal or FIFO creation is exposed.

`_file_object_exchange.py` delivers stat or conditional removal through one fixed, inline Python
helper attempt. After validating its request, nonce and bound identity, the guest acquires the
existing system file lock before opening the trusted root. Missing lock setup is a refusal even when
the requested object is absent. Paths and expected revisions travel only in sensitive stdin. The
request carries a remaining duration from which the guest derives its own monotonic expiry.

The host accepts only a complete, nonce-bound `AGWF1` result matching the requested operation.
Carrier completion remains separate from object evidence. Lost, malformed or noisy observation after
a possibly dispatched removal is uncertain, never permission to retry. A complete helper report of
an uncertain mutation preserves its closed failure kind and phase. No raw diagnostic bytes are
retained. Stat returns metadata only; it does not read file contents or stage state.

The fixed operation bundle is checked against the complete Proxmox HTTP request bound. Local
isolated-interpreter fixtures exercise the real lock walk under a test-owned root, not a production
configurable lock path. These tests do not establish privileged or cross-identity native acceptance,
the complete Windows SSH command-line bound, or public FileAccess composition.

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
owns path validation, the trusted parent and the transaction lock. These private functions provide
neither a public file service nor native or elevated acceptance.

## Private directory inventory

`_file_inventory.py` reads a bounded Linux directory inventory beneath a borrowed trusted root and
caller-owned transaction lock. It observes regular files, directories and sockets without reading
file content, refusing links, multiply linked regular files, unsupported special objects and
descendant mount crossings. Depth one returns immediate children; a directory at the requested depth
is observed but its children are not enumerated.

Results are sorted by relative UTF-8 path bytes. Entry, name and encoded-output limits fail rather
than returning a truncated result. The shared encoder defines the exact compact JSON byte count,
including framing. Disappearance or observed replacement conflicts. The result is a bounded set of
observations, not a coherent snapshot against external writers. Carrier delivery and public
`FileAccess` composition are not implemented by this private primitive.

## Private scratch transfer

`_scratch.py` supplies POSIX destination-side scratch operations beneath a borrowed trusted parent
descriptor. Each object has an unpredictable private directory and one fixed data file, with final
modes 0700 and 0600. An identity-bound private reference carries its declared length and SHA-256.
Operations reopen and check the recorded objects; observed replacement, links, special objects or
changed ownership/mode refuse. The caller owns confinement and coordination between writers.

Writes use bounded exact offsets and a chunk digest. An exact previously written range may be
retried after comparing its bytes; gaps, conflicting duplicates and partially overlapping chunks
refuse. Whole-object length and digest verification precedes bounded reads. The 24 KiB raw chunk
limit is an internal candidate, not evidence that a complete encoded carrier request fits.

Cleanup removes only the recorded data object and empty directory, never unknown neighboring objects
or a recursive prefix match. Errors retain closed facts and unresolved identity-bound cleanup debt.
Creation preserves known acquisition facts through handled control-flow interruption; an unresolved
debt is attached as a closed error cause while the original control exception propagates. Python
ownership bookkeeping is not signal-atomic and repeated interruption is not a bounded cleanup
guarantee.

This primitive does not implement remote request validation, a wire protocol, helper deployment,
execution staging or FileAccess. Filesystem calls have no hard interruption bound, and no crash
durability or hostile same-user race guarantee is made. Local Linux fixtures are not native macOS or
carrier acceptance.
