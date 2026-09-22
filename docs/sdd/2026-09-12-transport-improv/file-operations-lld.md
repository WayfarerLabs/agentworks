# File Operations: First Implementation Slice

<!-- cspell:ignore aclxattrs atx bytewise chmods ctime datagram errno fsync lstat mountpoint -->
<!-- cspell:ignore multiprocess netstrings noexec nofollow nonblocking nonlocal noreplace -->
<!-- cspell:ignore openat openatx overclaim pread pwrite statx xattrs -->

- Status: Private helper exchanges implemented in part; public composition and native acceptance
  remain open.
- Governing requirements: [FRD R7](frd.md#r7-files), including the
  [file safety and guest runtime rulings](frd.md#file-safety-and-guest-runtime-rulings).
- Public boundary: [execution contract](execution-contract.md#file-operations-and-bound-policy).
- Delivery:
  [plan steps 3 and 4](plan.md#3-reconcile-designs-and-publish-the-implementation-boundary).

## Scope and decisions

This slice implements bounded regular-file reads, metadata observations, bounded directory
inventory, atomic regular-file publication, the four shipped JSON strategies, directory creation,
metadata changes, and conditional removal of regular files, empty directories, and confirmed stale
sockets. `upload` and `download` are streaming facades over the same snapshot and publication
mechanics. There is no FIFO or socket creation, recursive removal, archive extraction, directory
tree replacement, remote transform callback, general policy evaluator, or caller-supplied helper
command.

The implementation lives under `agentworks.execution` and must have no direct or indirect import of
`agentworks.native_files`, its `Transport`, or another retirement package. Algorithms and tests may
be adapted, but the new runtime does not call the legacy stack. The shipped helper's unlocked
check-then-rename conflict window, public runner, mutable staging slots, and root-only string
allowlist are not carried forward.

The first slice needs one small file helper, invoked as a subprocess through the new `Carrier`.
Shell built-ins do not expose the required descriptor-relative object handling. Debian guests use a
standard-library Python helper compatible with Bookworm's distribution `python3`; new-guest early
provisioning includes that package. macOS platform hosts require preinstalled Python 3.11 or newer;
the [preparation prerequisite check](preparation-lld.md#readiness-and-minimal-substrate) must reject
missing, unsupported, or Xcode-shim interpreters cleanly without installation. Existing-guest
recovery and no-staging readiness still need their own proved paths. In every case, the helper is a
closed operation protocol, not a daemon, agent, remote execution escape, or general file-policy
engine.

Directory transfer and confined extraction remain required by R7 but are deliberately outside this
first slice. Their absence blocks complete R7 acceptance, not delivery of the file-only vertical
slice in plan step 4. Windows-local atomic download publication also needs a later host-specific
design; this document covers Debian guest filesystems and SSH-accessed macOS platform hosts.

## Small typed contract

The public types belong in `execution/files.py`. Values are frozen and validate their boundary on
construction. Payloads, revisions, helper requests, and helper responses have no diagnostic
representation.

```python
type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
class FileKind(StrEnum): REGULAR = "regular"; DIRECTORY = "directory"; SOCKET = "socket"
class JsonStrategy(StrEnum): REPLACE = "replace"; MERGE_OVERWRITE = "merge-overwrite"; MERGE_PRESERVE = "merge-preserve"; SKIP_EXISTING = "skip-existing"
class Change(StrEnum): CHANGED = "changed"; UNCHANGED = "unchanged"

@dataclass(frozen=True, repr=False)
class Revision: token: bytes
@dataclass(frozen=True)
class FileMetadata: kind: FileKind; size: int; mode: int; uid: int; gid: int; modified_ns: int; revision: Revision
@dataclass(frozen=True, repr=False)
class ReadResult: data: bytes; metadata: FileMetadata
@dataclass(frozen=True)
class DirectoryLimit: max_entries: int = 1_024; max_depth: int = 1; max_encoded_bytes: int = 1_048_576
@dataclass(frozen=True)
class DirectoryEntry: relative_path: PurePosixPath; metadata: FileMetadata
@dataclass(frozen=True)
class NewMetadata: owner: str; group: str; mode: int

type WriteCondition = Create | Replace | Match  # Match carries one Revision.
@dataclass(frozen=True)
class MutationResult: change: Change; revision: Revision | None
```

`Create` requires absence and uses an atomic no-replace primitive. `Replace` requires an existing
regular file and overwrites it unconditionally; it is not the default for read/modify/write callers.
`Match` requires that existing file to match its revision. `Revision` is opaque, versioned
observation evidence, not authority. It contains object kind, filesystem/object identity, size,
timestamps, mode, owner, and group. A regular-file snapshot also binds its content digest.

The first token format reuses the private version-1 revision fragment: compact, sorted-key ASCII
JSON encoded as exact bytes. Construction decodes the existing closed schema and requires a
canonical byte-for-byte round trip, rejecting unknown versions, extra or duplicate fields and
encodings outside that canonical form. The token has a 4,096-byte structural bound, independent of
file size. Only private conversion code interprets it; callers retain and return the opaque
`Revision`. Public metadata reports permission bits separately from object kind, while the token
retains the full observed mode. Every metadata field must agree with its revision. A `ReadResult`
additionally requires a regular-file revision whose size and digest match its exact contents; stat
and inventory observations need not have a content digest. A caller-constructed consistent value
does not prove remote observation or grant authority.

Every in-memory read has a positive caller-selected byte bound; a product default may be offered,
but there is no universal total-file ceiling. Streaming upload declares its exact finite size, and
download obtains a finite size from the held source snapshot before transferring. Chunk buffers
remain bounded. Directory inventory defaults to 1,024 entries, depth 1, and 1 MiB encoded; reviewed
callers may request up to 4,096 entries, depth 8, and 4 MiB encoded.

Inventory retains one traversal and one canonical response. At its maximum encoded size, the current
protocol emits at most 5,650,717 stdout bytes including data/control records and runtime prefix.
PVE's decoded-string JSON envelope adds escaping and status fields. The selected 8 MiB HTTP-response
bound leaves room for that complete response; it is not a larger inventory grant or unbounded output
capture. Native PVE/QGA evidence must verify the supported maximum and incomplete output behavior.
Do not replace this bounded response with repeated traversals presented as one complete inventory.

Ordinary `read_file` composes the owned snapshot/chunk download into a bounded in-memory sink. This
preserves the caller's byte bound without requiring one carrier response to contain the whole file.
The complete source revision, verified bytes, deadline and scratch cleanup must agree before
returning a public result. Readiness binds the separate no-staging inline read described below, with
a proved response bound selected before dispatch. An oversized or lost observation never triggers a
retry through another read mechanism. This selection is an operation constraint, not deferred
permission enforcement.

`FileAccess` exposes only these forms:

```python
def read_file(path: PurePosixPath, *, max_bytes: int, sudo: bool = False) -> ReadResult | None: ...
def stat(path: PurePosixPath, *, sudo: bool = False) -> FileMetadata | None: ...
def list_directory(path: PurePosixPath, *, limit: DirectoryLimit, sudo: bool = False) -> tuple[DirectoryEntry, ...]: ...
def write_file(path, data, *, condition, create_metadata, sudo=False) -> MutationResult: ...
def upload(path, source, *, size, condition, create_metadata, sudo=False) -> MutationResult: ...
def download(path, destination, *, max_bytes=None, sudo=False) -> FileMetadata: ...
def update_json(path, document, *, strategy, create, create_metadata, max_bytes=DEFAULT_JSON_MAX_BYTES, max_depth=DEFAULT_JSON_MAX_DEPTH, sudo=False) -> MutationResult: ...
def ensure_directory(path: PurePosixPath, *, metadata: NewMetadata, sudo: bool = False) -> MutationResult: ...
def set_metadata(path: PurePosixPath, *, owner: str, group: str, mode: int, sudo: bool = False) -> MutationResult: ...
def remove(path: PurePosixPath, *, expected_kind: FileKind, expected: Revision, sudo: bool = False) -> MutationResult: ...
```

`sudo=True` requests the implementation privilege already bound to this `FileAccess`; it never
creates authority. `upload` consumes exactly `size` bytes and rejects early EOF or excess input.
`download.max_bytes` is an optional caller safety bound, not a core file-size cap. JSON byte/depth
limits bound the source, existing snapshot, and result; defaults are caller-overridable, not
authorization or a universal file ceiling.

Paths are absolute, normalized POSIX paths without NUL, empty, `.` or `..` components. A write does
not create parents. `ensure_directory` creates exactly one missing final component with restrictive
initial permissions, then applies requested metadata before returning. Callers create a hierarchy
one explicit component at a time. Existing non-directories conflict; an existing directory is
verified and its requested owner/group/mode is converged. Root creation therefore remains distinct
from permission to mutate descendants or the trusted parent.

The private helper request always retains a nonempty relative path. For an authorized operation on
an approved root itself, core binds that root's trusted parent and passes only the root's final
component; descendant operations bind the approved root. This decomposition does not grant the
caller access to the parent or siblings. There is no empty-path or `.` self-target sentinel. Exact
ancestor creation, such as `/run/agentworks` before a session socket directory, remains a separate
explicit operation rather than implicit `mkdir -p` behavior. During coexistence this is internal
path composition, not a claim that the successor permission model is active.

The filesystem root `/` has no final component and is not representable by that private contract.
Whether the initial public API explicitly excludes `/` itself or adds read-only root stat/inventory
is an open operator question. Do not introduce an empty-path sentinel, claim root support from path
normalization alone, or infer blanket-root authority. Explicit descendant targets remain
representable. During coexistence, a closed operation method plus core-bound parent/leaf and
identity is sufficient for exact-operation confinement; no action registry or permission catalog is
needed to express this composition.

Requested metadata owner and group names are resolved by a closed read-only operation in the fixed
account helper. It resolves the pair to numeric UID/GID without retrieving supplementary groups,
changing credentials or selecting elevation. Missing owner and missing group are distinct closed
refusals. Names remain in sensitive input; no password, home, shell or group member list returns.
The resulting numeric ownership is an input to the already selected file-operation identity plan,
not a replacement for that plan. A lookup never grants permission to apply the metadata.

`read_file` returns `None` only for absence. It opens regular files nonblocking and rejects every
other object before reading, so a FIFO or device cannot hang the operation. `stat` reports the
closed `FileKind` set above and rejects links or unsupported special objects. `list_directory` never
follows links or crosses a descendant mount. Its requested depth defines the inventory: depth 1
includes immediate children, and directories at the boundary are reported but not enumerated.
Results are complete within that requested depth; exceeding an entry, name or encoded-output bound
is an error, never truncation. Unsupported depths refuse before traversal. Sorting is bytewise by
relative UTF-8 path so SSH and QGA return the same order.

`upload` streams into `write_file`; `download` streams one snapshot to a private local sibling and
replaces only after verification. Local links/special objects are refused. Windows-local publication
needs a later host-specific design.

## JSON semantics

The private local transformation lives in
[`execution/_json.py`](../../../cli/agentworks/execution/_json.py). It proposes bytes or a skip
decision; it performs no destination I/O and does not establish publication, confinement, locking or
FileAccess acceptance.

Caller byte/depth limits are rejection thresholds, not an override of the interpreter's JSON
capacity. The local implementation retains the standard-library nesting and integer-conversion
safeguards described in the
[Python JSON documentation](https://docs.python.org/3.12/library/json.html#implementation-limitations).
Private review on CPython 3.12.13 reproduced refusal of a 4,301-digit integer and an encoding
recursion failure at depth 998 despite larger caller limits. These are observations, not portable
numeric caps. Diagnostics must not mislabel runtime capacity as proof of malformed input or a
caller-byte-bound violation. No process-global limits are relaxed. If a required migrated workflow
needs a refused document, that is an acceptance blocker requiring a supported mechanism or operator
disposition, not a workflow silently dropped by this note.

`update_json` validates the source as one finite UTF-8 JSON object before any target I/O. Duplicate
keys, non-finite numbers, invalid UTF-8, non-object roots, and values beyond the call's byte/depth
bounds are validation failures. Serialization is semantic and deterministic; original formatting is
not preserved.

- `replace` publishes the validated source and does not parse existing bytes.
- `skip-existing` returns unchanged for any existing regular file, including empty or malformed
  bytes. If absent, it creates only when `create=True`; otherwise it raises `StateError`.
- `merge-overwrite` recursively merges objects with the source side winning.
- `merge-preserve` recursively merges objects with the existing side winning.
- In both merge forms, arrays and scalars are atomic leaves. A winning array or scalar replaces the
  losing value. JSON `null` is an ordinary scalar value and never means deletion. No deletion or
  JSON Merge Patch language exists in this slice.
- Merge rejects an absent destination unless `create=True`, and rejects malformed existing bytes.

Merge uses an internal bounded snapshot, applies the pure transformation locally, then publishes
with `Match(snapshot.revision)`. The existing bytes and diff never leave the implementation. On a
cooperating-writer conflict it repeats snapshot, merge, and conditional publication within a budget
of eight total publication attempts and the caller's one deadline, then raises `ConflictError`.
`replace` and `skip-existing` do not gain accidental old-document parsing. TOML and
generated-section transformations stay in their resource domains and use snapshot plus `Match`;
FileAccess accepts no transform callback.

## Helper deployment and protocol

Debian guest operations use a standard-library helper that supports the Bookworm distribution's
Python 3.11. New-guest provisioning adds `python3` to the early apt package list and must prove the
interpreter is available before helper-dependent work. This does not install Python during a file
operation or readiness check. Existing native-recovery targets without Python need an independent
bootstrap path. macOS placement hosts must already provide compatible Python 3.11 or newer;
availability checks and platform proof remain open. New guest packages do not satisfy that host
prerequisite. Runtime download, on-target compilation, elevation retry, and a legacy-helper fallback
are outside this design.

`execution/file_helper.py` owns the operation's exchanges and recorded data scratch. The selected
next delivery uses a short fixed launcher in literal argv and a fixed core bundle followed by the
operation manifest in one sensitive finite stdin stream. The helper executable is neither installed
nor staged. Core alone selects the closed operation family and packaged modules. Transfer,
observation and mutation families carry their relevant dependencies; callers cannot select modules,
source, executables or a fallback.

The launcher fixes the armored bundle's byte length and SHA-256 in core-generated source. It reads
exactly that many bytes with unbuffered reads, verifies the digest before decoding or executing
anything from stdin, then loads the trusted modules and calls the fixed family entry point. The
remaining stdin bytes are the bounded canonical operation manifest. A short or changed bundle
refuses before any helper module executes; request bytes never become source or select the bundle's
length, digest or entry point. This is one invocation, not a source-upload handshake or a generic
file-operation code parameter. Identity admission still precedes workload-path access.

Bundling retains one fixed standard-library `bz2` codec and base64 armoring. The
[publication sizing investigation](prior-art-research.md#publication-helper-delivery-sizing) shows
that the complete helper cannot reliably fit Windows argv, including after dependency reductions.
Moving the fixed bundle to stdin removes that command-line dependency without another codec option.
Bookworm's distribution Python supplies `bz2`; arbitrary Python builds may omit it. Selected-runtime
readiness must prove the required imports and report missing prerequisites cleanly before workload
dispatch. No implicit macOS installation is allowed. This delivery revision remains subject to
implementation review, all-family regression coverage and native acceptance.

Data staging and snapshot spools still reuse preparation's exact-offset, length/digest-verified
scratch mechanics. The shared 24 KiB raw chunk is a candidate pending complete SSH/QGA request
proof, not a file-layer constant. The private stage exchange uses a smaller 12 KiB raw chunk and a
32 KiB complete-manifest limit. Its fixed bundle and complete provider serialization are measured by
local fixtures; native acceptance and every supported connection/identity prefix remain separate
proof obligations. Close removes only recorded objects; uncertain cleanup is owner debt and never
hides the primary outcome. Shared same-invocation admission must establish interpreter prerequisites
without installing anything; platform/ABI-dependent operations retain their own feature checks. No
separate probe, installed helper version or executable-digest handshake is needed when the
executable source travels with each invocation.

Upload consumes its declared finite source once. The unverified scratch reference binds exact object
identity and expected length, not a whole-file digest that a streaming source cannot yet supply.
Each chunk still carries its exact offset and digest. The host computes the overall digest while
consuming the source, rejects short or excess input before publication, and supplies the final
digest with the publication request. Guest verification establishes a ready reference carrying that
verified digest before publication can consume it. This separates incomplete transfer from verified
content without requiring a host spool solely to precompute a digest.

A download snapshot copies one held source inode into private scratch in bounded chunks within the
caller's operation ownership. Source identity/metadata and the copied length/digest must agree
before the snapshot is ready. Chunk retrieval then reads that private snapshot, never successive
ranges of the changing public source. The private `_file_spool.py` candidate now composes
held-source observation and scratch transfer to implement that local copy. It verifies length, EOF,
digest and final source identity/metadata, checks expiry after source closure even for absence, and
retains exact cleanup debt on failure. Core supplies its token and identity before dispatch, and the
local receipt binds the copy to the snapshot operation. The private snapshot exchanges deliver
creation, chunks, lost-reply ownership reconciliation and exact cleanup through a carrier. Local
helper evidence covers these exchanges; complete download composition, production coordination and
native carrier acceptance remain open.

Snapshot storage is independent of source authority. Download must work when the selected identity
can read the approved source but cannot write its parent. The Linux candidate uses the existing
fixed `/tmp` directory as a core-selected scratch parent, opened without symlinks and validated as a
root-owned directory with mode 01777. It does not trust `TMPDIR`, application environment, the
source path, an account-home convention or a caller-selected staging path. Missing or unsafe state
refuses; runtime neither creates nor repairs the parent. This selection is private helper machinery,
not a grant to read or write arbitrary `/tmp` children.

The existing token-derived 0700 directory, 0600 data and immutable receipt own each operation's
objects. Linux [sticky-parent semantics](https://man7.org/linux/man-pages/man2/rename.2.html)
prevent other unprivileged users from renaming/removing those directories; malicious same-user
processes remain outside the threat model. Exact parent/object identities and receipt validation
still apply, and temporary-file cleanup or reboot can invalidate an observation without proving
completion. Operation coordination belongs to the database-backed caller, not this temporary
directory. Root selection creates no object and is not part of readiness. The private
`_scratch_root.py` selector implements this admission. Local fixtures cover a non-root caller with a
read-only source parent, unsafe/missing scratch roots, deadline and descriptor cleanup, and
independence from payload environment. These fixtures do not establish native default-root
acceptance or select the macOS host root.

The first creation acknowledgment remains a transfer-delivery gate. The helper derives the scratch
name from core's fresh token and records its inode identities before returning them; losing that
reply leaves the host without an exact cleanup reference until read-only reconciliation establishes
ownership. The same issue applies to snapshot creation and to unreported publication-stage cleanup
debt. An unavailable reference is not proof that no artifact exists. Preserve that uncertainty
separately from known exact cleanup debt, never replay creation to recover a reference, and never
scan a name prefix to infer ownership. Reconciliation of this case must be settled and fault-tested
before the transfer exchange is accepted.

The local recovery candidate uses a small immutable ownership receipt, not a transfer registry. Core
allocates a fresh random operation token before dispatch. The fixed helper derives one exact private
directory name from it and attempts exclusive creation once. Before acknowledging successful
creation, it writes and validates a bounded receipt binding the token, operation, execution
identity, original authorized parent identity, declared length and acquired directory/data
identities. A collision refuses; losing a reply never resubmits creation. These private receipt
mechanics are implemented in `_scratch_receipt.py`. Private stage, snapshot and publication
exchanges deliver reconciliation and exact cleanup. Neither these private exchanges nor local
evidence complete production FileAccess or native acceptance.

Read-only reconciliation accepts the original core-bound context and token, not paths supplied by a
receipt. It opens only that exact name and validates the receipt schema, ownership, permissions,
links, context and identities. Recovered ownership permits exact cleanup, not publication: a ready
content reference still requires complete length/digest verification. Incomplete or already-removed
data can retain historical ownership evidence. Missing, partial or invalid receipts remain uncertain
and do not authorize adopting the current occupant of a name.

Ownership evidence is not evidence that a helper has stopped. The operation owner must serialize
creation, transfer mutation, snapshotting, publication, reconciliation and cleanup. A missing
receipt may precede a delayed request, so observing absence cannot certify terminal cleanup. The
implementation must prove the ordering around completed creation and subsequent mutation; neither a
receipt alone nor transport loss establishes quiescence.

Every follow-on mutation must validate the still-existing exact operation receipt before creating
any artifact. After cleanup, a delayed chunk or publication request therefore refuses instead of
recreating state. Database exclusion alone does not establish this. Scratch-backed `publish_file`
now validates and holds its receipt before creating the sibling; the private exchange preserves that
primitive-owned admission rather than duplicating its checks. This requires no tombstone service;
full operation ordering remains an acceptance gate.

Publication retains a sibling stage in the actual destination directory for its access-metadata
semantics. Its candidate ownership record is separately bounded and immutable, rather than a rewrite
of the sole creation receipt. The selected private implementation places that record inside the
already-owned upload scratch directory and derives the publication sibling's exact basename from
core's token. Before creating the sibling, admit and retain the existing stage receipt and its
identity/context. The record binds the original destination parent and acquired sibling identity; it
introduces no caller-supplied path or scan. Generic scratch cleanup must refuse this extra record
without first discarding the data or original receipt, so explicit publication cleanup precedes
ordinary scratch cleanup. Existing local byte-publication mechanics remain shared rather than
duplicated into a second rename implementation.

Recovery binds that stage to the original authorized parent and recorded name/inode; it never
follows the inode into the public destination. Remove owned data and outstanding stages before their
receipts. A missing stage is not evidence of successful publication. Interruption before ownership
is recorded, or between receipt removal and final directory removal, can still leave uncertain
cleanup. These limits do not become a journal, prefix scavenger, resumed upload promise or
reboot-durability requirement. `_publication_receipt.py` implements a private local candidate and
`_file_publication.py` uses it for scratch-backed publication. The private carrier exchange now
delivers publication, reconciliation and exact cleanup. Local complete-transcript and fault tests do
not close joint native delivery or full upload ordering, including lost replies, delayed dispatch,
partial creation and interrupted cleanup.

The delivery audit at `0ecb9a2e` found that one monolithic bundle plus a 24 KiB chunk nearly
exhausts or exceeds the historical 64 KiB Proxmox whole-POST limit before its missing dispatcher is
added. The largest existing per-module representation also exceeds Windows' 32,767-character process
command-line limit after SSH serialization. Separate fixed bundles and aggregate compression are the
selected candidate, not native acceptance. Final encodings must fit the complete provider body and
workstation command line, including framing, quoting and privilege prefixes. Retain the older
Proxmox compatibility floor rather than silently adding a package prerequisite. Oversized requests
must refuse before dispatch; there is no automatic executable-staging fallback.

Every exchange is one `Carrier.execute` with literal absolute argv. After the fixed bundle prefix,
requests use bounded canonical ASCII JSON with a version, 32-hex request ID, one closed operation,
identity, and operation-specific fields. Manifest bounds do not replace aggregate carrier bounds:
count the bundle, manifest, complete argv, quoting and provider serialization before dispatch. Paths
and other byte payloads use canonical base64. Responses use the existing sequenced `AGWF1` records,
matching that request ID, with closed result/failure bodies and a final terminator. The framing
codec is shared between concrete file exchanges; their request schemas and response grammars remain
operation-specific. Unknown, duplicate, non-canonical or extra fields refuse. Strict
field/line/decoded/total bounds must fit the complete carrier request. No request value becomes argv
or shell source; fixed preparation bootstrap source cannot be reused for file operations.

The private Linux `stage_begin`, `stage_chunk`, `stage_reconcile` and `stage_cleanup` exchanges
implement that delivery shape. They require caller-owned operation coordination below rather than a
destination lock. Every request retains the nonempty original destination path; the guest derives
its parent after checking execution identity. A complete creation result must match the requested
length before the host exposes its reference. Cleanup debt is data bound to the original
token/context, not a returned name or path. Chunk scratch-failure debt must match the already-known
active reference exactly; the response cannot introduce different cleanup ownership. Missing private
parents refuse. Incomplete observation after possible dispatch remains uncertain; no replay or
public absence is inferred. Guest expiry is checked after owned descriptor cleanup; a completed
mutation retains exact cleanup debt when that final check expires instead of becoming a no-effects
refusal. Reconciliation exposes only complete historical cleanup ownership, never a ready content
reference; missing or invalid receipts remain ownership uncertainty. Explicit cleanup checks expiry
before its first deletion and accepts only the original identity-bound debt. Returned cleanup
failure debt must match it. These exchanges do not prove earlier-request quiescence or implement
complete upload/publication; delayed chunk requests refuse after exact cleanup removes the receipt.

### Private snapshot exchanges

Use a separate fixed snapshot family, not stage operations with a caller-selectable storage root.
Every request carries the version, nonce, core token, execution identity and relative budget.
`snapshot_begin` additionally carries the approved source root, nonempty relative source path and
maximum byte count. Within the caller's operation ownership, identity admission validates the fixed
scratch parent, and copies one held source with `_file_spool.py`. A complete result is either
initial absence or a ready scratch reference plus the content-bound source revision. The host must
check that the ready length and digest agree with the source revision and requested bound. Source
absence does not bypass prerequisite or final deadline checks.

`snapshot_chunk` carries that ready reference and one exact offset/length, initially bounded to 12
KiB. It uses the fixed scratch parent and existing unlocked range checks, never the original source
path. The complete typed response binds the requested range, byte count and chunk digest; only then
may its bounded bytes reach the private download composition. The host also checks whole download
length and digest. Zero-byte snapshots require no nonempty range. Raw carrier output, truncated
framing or conflicting ready facts cannot become file bytes or a successful download.

`snapshot_reconcile` needs only the original core token and identity; `snapshot_cleanup` adds the
known exact cleanup debt. Both operate under the same operation ownership and fixed scratch root,
independently of whether the source still exists or is readable. Reconciliation returns historical
cleanup ownership only. Missing evidence remains uncertainty, and cleanup does not claim that an
earlier unobserved creation cannot still arrive. Cleanup checks expiry before mutation and after
descriptor closure, preserving exact debt. No operation accepts a returned path or allows a stage
receipt to be reused as a snapshot receipt.

Ready-reference wire fragments belong beside existing scratch reference/debt fragments. Shared
source-revision fields should have one concrete codec reused by object and snapshot exchanges,
without changing their external envelopes. Validation belongs at incoming request/result boundaries;
the typed interior must not gain a second validation framework. Final source size, complete native
request/output bounds, Windows serialization and cross-identity/native execution remain proof gates.

### Private publication exchanges

The private publication family contains `publish`, `publication_reconcile` and
`publication_cleanup`. Every request binds the original approved root, nonempty destination relative
path, core token, execution identity, plain stage reference and relative budget. The guest derives
the parent from that original path after identity admission. Scratch and sibling publication use
this same parent; neither a returned path nor a returned basename can select cleanup authority.

`publish` adds the final whole-content SHA-256, explicit Create/Replace/Match condition and numeric
create metadata. It verifies the scratch content before passing the ready reference to the existing
publication primitive. The result carries the content-bound revision; the host checks its length and
digest against the original request. A publication failure retains its closed kind, phase and
available exact cleanup debt. An uncertain publication remains uncertain, even when cleanup
succeeds.

`publication_reconcile` uses the original plain reference, not a ready reference or surviving data
payload. It returns historical cleanup ownership or ownership uncertainty. Receipt-backed debt binds
the original scratch ownership, destination parent, exact sibling and record identities, record mode
state and whether the sibling was already removed. The wire representation reuses the request's
token/context/reference and derives the sibling name from that token. It must not permit replacement
scratch ownership or a new destination to arrive through the response.

`publication_cleanup` accepts known exact debt. Receipt-backed cleanup uses the existing primitive;
an identified sibling-only debt also requires the original parent identity and token-derived name
before exact-inode removal. Unidentified debt authorizes no deletion and may only lead to explicit
reconciliation. Cleanup responses cannot substitute identities; only observed cleanup progress may
reduce remaining work. Removing the publication record precedes ordinary scratch cleanup. A lost
reply after rename or record removal can remain uncertain: neither an absent stage nor an absent
record proves publication, completed cleanup or earlier-request quiescence.

Complete result bodies also carry `deadline_exceeded`, checked after the guest closes its owned
root/parent descriptors. If publication returned a revision before that final check expired, retain
the confirmed publication and revision while reporting the timing failure. If cleanup completed,
retain the cleaned fact without recreating debt. Reconciliation similarly retains exact recovered
ownership. These are effect observations, not an in-budget success claim. Early expiry refuses
before mutation, and a later deadline does not overwrite an already established operational failure.
The host still requires a complete valid transcript and keeps carrier completion separate from
helper facts; a truncated observation is not promoted into a confirmed effect.

Before accepting this family's delivery, measure the actual protocol, dispatcher and dependency
closure through complete SSH/Windows quoting and QGA serialization. A minimal dependency loader
without the production dispatcher is not evidence of fit. The fixed stdin bundle delivery above has
no runtime codec option, executable staging or oversized-request retry.

### Private output delivery

Ordinary buffered capture cannot safely carry this protocol: sensitive input suppresses the
response, while ordinary `Capture` can flow toward public execution results. Use the implemented
internal `SinkOutput(collector)` extension, subject to its outstanding joint SSH proof. The
collector incrementally parses the file schema and retains no raw response. It accepts only a
matching response frame and closed typed fields; download/snapshot bytes stream directly into the
private destination/spool while offsets and hashes are checked. Its tiny framing buffer is bounded
and cleared on rejection. Unexpected account-hook output, including reflected request bytes, is
rejected without entering an execution result or diagnostic. The carrier report contains only the
delivery/retention facts defined by the carrier I/O LLD; the collector alone owns the typed file
outcome. Ordinary sensitive-output suppression remains unchanged. This file schema, not a generic
private raw-capture mode, decides which typed content may survive.

Closed helper operations are `stage_begin`, `stage_chunk`, `stage_reconcile`, `stage_cleanup`,
`publish`, `publication_reconcile`, `publication_cleanup`, `snapshot_begin`, `snapshot_chunk`,
`snapshot_reconcile`, `snapshot_cleanup`, `stat`, `list`, `ensure_directory`, `set_metadata`, and
`remove`. Staging is created beside the destination with mode 0600 and an unpredictable helper-owned
name. Chunks use exact offsets and hashes; final size and SHA-256 must match before publication.
Snapshot chunks come from a private complete spool, not repeated reads of a changing source. The
host checks the end-to-end size/digest too. No helper operation accepts executable names, arbitrary
flags, environment, cwd, source text, callbacks, or a destination outside its single request.

### Owned download composition

The private download coordinator composes snapshot creation, verified bounded chunks and exact
scratch cleanup under one borrowed core operation. It reuses the concrete file dispatch gate used by
upload and JSON; it does not introduce another claim or release ownership between chunks. One
original deadline covers the whole transfer and any follow-on cleanup. The initial private entry
requires a finite positive source bound supported by the snapshot protocol. Public `max_bytes=None`
still needs composition with source-size observation; a private required bound must not become an
undocumented public file-size ceiling.

The coordinator writes only complete, verified chunk observations to a borrowed byte sink, handling
short writes and temporary stalls without retaining the whole file. It tracks accepted bytes and the
whole-stream digest, including an empty snapshot. The sink is a private composition seam, not a new
public download overload: FileAccess must supply an owned local staging writer and publish only
after successful transfer and verification. A partial or failed transfer never authorizes publishing
that staging file. The coordinator neither closes the borrowed sink nor changes the local
destination itself.

Record source revision, ready reference, cleanup debt and runtime observations before settling each
carrier attempt. Missing normal-chain completion stops further exchanges, even if a chunk arrived.
Confirmed absence returns no file bytes. Sink failure stops transfer; exact known remote cleanup is
allowed only with settled dispatch and remaining budget. Uncertain snapshot creation retains the
original token and binding for recovery, never replays creation. Missing cleanup proof or lost
ownership retains the corresponding obligation rather than turning a verified stream into success.
Private results retain closed failure facts, byte counts and original recovery bindings, not file
content or raw sink exceptions. Local publication and its cleanup obligations remain separate from
remote scratch cleanup and must both be accounted for by the complete public operation.

Private completion and absence statuses describe established operation facts; they are not an
in-budget success guarantee. A complete normal-chain observation can survive a late deadline, with
`deadline_exceeded` retained independently. Public result conversion must report the timing failure
without discarding proved absence, changes, verification or cleanup, and must not turn the status
alone into an ordinary success. Apply the same interpretation to upload and JSON composition.

The private memory-read adapter uses the shared `FileOperation` download boundary, capturing
unfinished facts before final byte/result allocation. Failed allocation discards the temporary
buffer without discarding the already-retained download outcome or inventing a second claim.

This is private composition, not public download or Windows/macOS acceptance. The host-specific
publication design and native evidence remain required before exposing download through FileAccess.

### No-staging readiness gate

The private Linux read and stat/removal exchanges use fixed bundled helpers and concrete `AGWF1`
collectors. Each checks its bound identity before accessing workload paths, which travel only
through sensitive stdin. Read snapshots materialize before emission; stat returns metadata without
reading content. Conflicting work is serialized by the caller's operation ownership, not by a
destination lock prerequisite. Read length, digest, metadata, framing and carrier-stream evidence
must all agree before bytes return. Root or leaf absence remains distinct from refusal and
incomplete observation; an absent target does not bypass identity or deadline checks. Both derive a
guest-local expiry from the host's remaining duration. Neither read nor stat performs deployment,
spool, mutation or lock creation. These are local private candidates, not production FileAccess or
native SSH/QGA/macOS acceptance. The remaining readiness gates below still apply.

Preparation readiness permits no helper deployment, private scratch, spool, or new lock state.
FileAccess may expose only bounded `read_file` and `stat` there, and only through an
already-available trusted substrate proved to meet file confinement, object, sensitivity, and
truthful-result rules within the inline bound. Inline code delivery alone does not establish this
gate: the selected operation must itself create no state. Without such a substrate, an optional call
refuses before dispatch, while a required workflow must establish its prerequisite before entering
readiness. Relocation to a staging-capable phase is allowed only when the existing workflow contract
permits it; a mandatory readiness read that cannot remain no-write is a delivery gate requiring
operator decision. Absence never authorizes public execution or silent removal of a required
workflow. The early-Python versus native-helper decision is settled for Debian guests and macOS
hosts; already-available readiness execution and the macOS platform mechanics remain unproved.

## Confinement and filesystem mechanics

The private [`_file_snapshot.py`](../../../cli/agentworks/execution/_file_snapshot.py) experiment
implements descriptor-relative bounded regular-file observation with standard-library Python 3.11
syntax. It rejects observed special objects before leaf open, retains no-follow/nonblocking checks
after that observation, and binds bytes to a digest and before/after identity/metadata. It borrows
the trusted root descriptor and assumes the caller serializes conflicting operations; it creates
neither locks nor files. Local fixtures cover object refusal, observed replacement, byte bounds and
owned-descriptor cleanup. This is not FileAccess or native platform acceptance.

On Linux x86-64 and AArch64, `_file_paths.py` now supplies `openat2` for every descendant open with
the four resolution restrictions below. Kernel/ABI unavailability refuses without a weaker fallback.
The separate non-Linux POSIX walk detects changed `st_dev`, not same-filesystem bind mounts. Local
Linux fixtures exercise real symlink, proc descriptor magic-link and descendant-mount refusal;
same-filesystem bind-mount and native macOS proof remain open. A regular-file read still has no hard
elapsed-time bound. This increment does not resolve helper cancellation or close readiness,
cross-identity operation coordination and complete platform acceptance.

The private `_file_publication.py` candidate implements Linux sibling publication beneath a
caller-owned parent descriptor. It accepts bytes or bounded streaming from verified scratch and
explicit Create, Replace or Match conditions. Replace observes existing metadata without reading the
old content; Match also checks content when its revision includes a digest. Publication preserves
ordinary UID/GID/mode/access-ACL semantics while refusing unsupported metadata. The caller still
owns confinement, operation serialization and scratch cleanup. This is not remote upload delivery,
macOS support or FileAccess. Local fault and ACL fixtures are implementation evidence, not
acceptance of the complete platform guarantees below.

The private Linux `_file_objects.py` now observes regular files, directories and sockets through
confined path-only descriptors without requiring content-read permission. Removal requires exact
kind and revision; a content-bound regular-file revision also invokes the existing bounded digest
reader. It removes only a matching regular file, empty directory or socket, and reports initially
absent objects separately. It supplies neither tmux liveness checks nor an external-writer atomic
compare-and-remove primitive. Caller-owned confinement/serialization and native acceptance remain
gates.

All target operations occur in the helper process. Host-side normalization and grant checks reject
untrusted requests early; destination-side traversal and observation enforce the bound operation.
The guarantee assumes the authorized target identity is cooperating. A malicious process already
running as that same target user is outside this file guarantee, as is guest root. Accidental or
non-cooperating changes observed during an operation still produce conflict, refusal, or
uncertainty.

- The lookup candidate walks absolute ancestors from an open root descriptor. Linux uses `openat2`
  with `RESOLVE_BENEATH`, `RESOLVE_NO_SYMLINKS`, `RESOLVE_NO_MAGICLINKS`, and, once below a trusted
  policy root, `RESOLVE_NO_XDEV`. macOS walks one component at a time with
  `openat(..., O_NOFOLLOW | O_DIRECTORY)`, comparing object and filesystem identity before the final
  operation. Path components are normalized and authorized before dispatch, then opened without
  following links. A mount or object-identity change that is observed before publication is a
  refusal. These checks do not claim immunity to a malicious same-user process moving an ancestor
  after it has been opened.
- Inspect leaves without following links before opening, then open regular leaves with no-follow and
  nonblocking flags and verify by descriptor. Refuse symbolic links, multiply linked regular files,
  devices, FIFOs, and unexpected sockets when observed. Revalidate the held destination and staging
  objects before publication. This is safe object handling for the stated cooperative boundary, not
  protection from a malicious same-user process adding a hard link in the next system-call window.
- Publication creates one unpredictable, operation-owned sibling in the destination directory,
  writes and verifies all bytes, establishes the required metadata, syncs the staged file,
  revalidates the destination condition, and uses descriptor-relative rename. Create-only uses Linux
  `renameat2(RENAME_NOREPLACE)` and macOS `renameatx_np(RENAME_EXCL)`; lack of filesystem support is
  a refusal. Replacement uses the platform's atomic same-directory rename. Replacement requires the
  selected identity to have ordinary write authority for the existing file as well as sibling
  creation/rename authority in its parent; atomic replacement must not bypass an otherwise
  unwritable destination. Either missing authority is a refusal under that identity, with no
  fallback or elevation retry. There is no cross-filesystem copy, direct-write, non-atomic fallback,
  or retry with a different identity/elevation. Atomic visibility is promised; crash durability of
  the directory entry is not yet promised. Replacement normally changes inode identity, so callers
  cannot rely on same-inode equivalence or on already-open handles observing the new bytes.
- Cleanup names and recorded inode identity belong to the operation. Cleanup never scans a prefix or
  removes a name whose identity changed. A cleanup failure leaves bounded debt for the owner; it
  does not authorize broader deletion.

For an absent destination, `create_metadata` supplies the requested owner/group/mode. The new file
retains the destination directory's normal inherited ACL behavior. The helper begins with
restrictive staging access, does not blanket-clear inherited ACLs or attributes, applies the
requested values with the platform's ordinary ACL-mask interaction, and verifies the effective
access metadata before publication. If the requested access result cannot be established, the helper
refuses while the destination remains absent.

Every existing regular-file replacement preserves the direct-write-equivalent access profile: UID,
GID, permission bits, and access ACL, including the ordinary-write effects on those values.
`create_metadata` does not reset an existing whole file's access metadata. Additional extended
attributes are preserved only when the migration inventory identifies a real workflow requirement
and the platform can reproduce its ordinary-write semantics. Security attributes, capabilities,
set-ID state, and flags that an ordinary content write would clear are not blindly copied to a
replacement inode. An unrecognized or unsupported required metadata case refuses before publication
rather than silently losing metadata or widening access.

`set_metadata` changes owner/group, then mode on the held object. It preserves ordinary extended
attributes and uses the platform's ordinary chmod interaction with an existing access ACL. The
resulting owner/group/mode and effective ACL are verified. After the first system call, failure can
leave a known partial change; it is not described as atomic or unchanged. Recursive metadata and
arbitrary ACL/security attribute mutation are not exposed. The same partial-effect rule applies when
`ensure_directory` has created its final component but cannot finish metadata convergence.

The first metadata implementation supports regular files and directories, not socket metadata.
Regular-file requested modes contain ordinary permission bits only. Directory modes additionally
support set-group-ID and sticky bits; current tmux socket directories and shared workspaces require
set-group-ID modes such as 02770. Set-user-ID requests refuse. This is explicit metadata authority,
not permission inherited from a content write; ordinary kernel privilege checks still apply.

On Bookworm Linux, the candidate uses the already confined path-only descriptor through
`/proc/self/fd/<descriptor>` for ownership and mode changes. Direct `fchmod` does not support such
descriptors, and the newer empty-path chmod API is unavailable on Bookworm's kernel. This fixed
procfs bridge refers to the held inode rather than re-opening the mutable caller pathname. Verify
its identity against the held descriptor and recheck the named object before changing it. Missing or
incompatible procfs refuses; there is no fallback to a mutable pathname or a content-read open.
Apply ownership only when needed, then mode, preserving the kernel's ordinary ACL-mask interaction.
Verify the resulting identity, owner/group/mode and access ACL. Do not copy or enumerate arbitrary
extended attributes for an in-place change.

Create a missing directory with mode 0700, preserving inherited ACLs, then converge metadata on its
held inode. Do not remove a newly created public directory when a later step fails: it may already
contain other work. Record completed creation/ownership/mode steps, and distinguish known partial
changes from an uncertain attempted change. A returned system-call error alone does not prove that
an attempted mutation had no effect; retain uncertainty unless the outcome is confirmed. These local
mechanisms need privileged and native filesystem proof before public enablement; no malicious
same-user namespace guarantee is added.

## Cooperating writers and honest limits

Database-level operation ownership is the primary coordination mechanism. One Agentworks state
database coordinates participating operations on its managed resources. Independent databases do not
thereby coordinate the same target; concurrent independent controllers are not an initial guarantee.
The core operation boundary acquires the relevant resource scope before conflicting work, then
shares that ownership through RunContext and nested work. File helpers do not each acquire a
machine-wide destination lock, and neither guests nor platform hosts require protected lock-file
provisioning.

Start with coarse VM-level exclusion, independent of execution identity. Host-level mutations use
the platform's shared resource scope when they can conflict across VMs; do not infer separate
ownership merely from different SSH routes or user/admin execution. Distinct isolated work may run
concurrently. Resource ownership, not a hierarchy of per-path locks, determines conflicts. The
[hierarchical extension](hla.md#operation-coordination-and-hierarchical-extension) preserves
ancestor/descendant admission for #377. The current exact-key primitive does not implement it; finer
resource keys must not be enabled before their conflicts with coarse claims are enforced.

Database transactions reserve and update ownership atomically and finish before network work. Do not
hold a SQLite write transaction open for an entire remote operation. Ownership is durable while
remote effects are possible: local process death, a disconnected carrier, a deadline, or an expired
lease does not prove that a remote request stopped. Keep unresolved ownership visible and refuse
conflicting admission until reconciliation establishes that prior work cannot still mutate the
resource. A lock row alone is not a remote fencing mechanism. Never replay uncertain mutation to
discover whether it happened. Recovery needs an explicit handoff of the same operation, not an
unconditional delete-and-reacquire or a second overlapping recovery attempt.

The initial database primitive stores one claim per core-selected resource kind/name, with a fresh
operation ID, bounded operation label and one of three states: reserved before possible dispatch,
possible dispatch, or effects resolved. The caller commits possible dispatch before sending remote
mutation; interruption then leaves that state intact. Only a reserved claim can be abandoned without
remote evidence. Core records effects resolved only after obtaining no-further-effects evidence,
then explicitly releases that exact claim. Every transition matches the resource, operation ID and
previous state so a stale owner cannot alter a later claim. The ID is ownership identity, not a
secret or an authentication boundary. Database inspection does not itself establish target state.

These transitions must commit independently of any enclosing command transaction. A reservation
rolled back after remote dispatch would lose the very ownership it needs to retain. Restoring or
copying a database likewise does not prove target quiescence; recovery must account for remote work
that the selected database snapshot does not describe before treating it as authoritative for new
conflicting operations. Do not add lease clocks, automatic stale-owner deletion or a distributed
coordination service to this primitive.

The file composition serializes conflicting exchanges inside one operation too, including cleanup.
Holding the outer operation scope does not license parallel writes to the same destination. JSON
snapshot, merge and publication share the operation's ownership; regular publication rechecks its
condition immediately before mutation. Read materializes its bounded immutable snapshot before
transfer; inventory remains a bounded set of observations, not a globally coherent filesystem view.
Fresh exclusive scratch names prevent transfer collisions and retain exact cleanup ownership. They
do not serialize updates to the final shared filename.

One core operation owner is acquired before activation and carried through nested contexts and
teardown. FileAccess borrows it; it does not acquire or release a second database claim. The shared
serial-use guard covers an entire public file call across user/admin views, including JSON
read/merge/publication and cleanup, not just individual exchanges. Private file composition owns the
upload token, original destination binding, content references and cleanup debt. These values are
working state, not another claim lifetime or a generic transaction framework.

The single-exchange stat, inventory and removal compositions use the same serial borrowing rule as
upload, download and JSON. Ordinary reads use the snapshot/download composition, not an inline-read
wrapper. Metadata convergence holds one borrow across the fixed owner/group lookup and the
subsequent mutation. It records lookup facts before settling that attempt, and may advance only
after successful resolution, normal helper termination and a fresh check of the same deadline. Local
option validation precedes the lookup. No helper in this group creates transfer scratch; known
partial mutation and unresolved remote effects still remain independent outcome facts. Closing a
borrow never releases the outer claim, including when an exception escapes. These private
compositions do not implement the public error reduction or production RunContext binding by
themselves.

The concrete core owner permits one active serial borrower and one outstanding attempt. Before
dispatch, the borrower records unresolved state in memory, then commits the first possible-dispatch
transition. Later exchanges re-arm that in-memory state within the same durable claim. The file
workflow records returned effects, references and cleanup debt before acknowledging no further
effects; only the current borrow can settle its outstanding attempt. The coordinator does not
interpret carrier reports or file protocols.

Validate caller publication options with the canonical protocol schema before staging or consuming
input. Local preparation can still reject a complete encoded request, for example when identity,
path and chunk fields jointly exceed a manifest bound. The upload's concrete carrier forwarder arms
the attempt immediately before actual carrier execution, after that local preparation. A local
encoding failure before execution must not invent possible dispatch or strand a reserved claim.
After actual execution begins, an exception retains the unresolved attempt; no exception type alone
proves that remote effects stopped.

Closing the owner and admitting a borrow share the same guard. Close first prevents new dispatch; an
active borrow prevents release and requires explicit later finalization. A returning borrower may
record the outstanding attempt's facts but cannot start another exchange after close. The last
borrower does not implicitly release the claim. Only core's explicit whole-operation resolution,
after all child attempts and lifecycle obligations are quiescent, transitions the durable claim to
resolved, followed by exact-owner release. Never attempt release before it is safe, or assume a
failed database call committed or rolled back. A safe release may have committed before
interruption; do not compensate by claiming the resource again. Cleanup debt remains distinct from
possible future effects and must be returned or retained with its original binding, even after
normal helper exit.

The production file boundary must own that handoff, not leave it to the caller of a private helper.
Core file composition acquires the serial borrow and keeps its working state attached to the outer
operation. Borrowed helpers perform exchanges without acquiring or closing that borrow. Before
public result conversion or relinquishment, core captures the typed outcome and original binding,
including on exceptional exits. All user/admin file views share this operation state. No second file
lock or protocol-aware recovery callback belongs in the generic database coordinator.

The private download, upload and JSON custody slice attaches validated prepared workflows to
`FileOperation` before running them. This preserves original carrier, binding and token through
outcome capture; completed capture retains only unfinished facts, not streams or file/JSON bytes.
JSON attaches its nested prepared upload before dispatch; failed child fact construction leaves that
child's token and state reachable through the parent. Finishing a call removes its own record by
identity, so it cannot erase a subsequently admitted call. Outcome retention precedes borrow
release; a retention failure keeps the working state and serial borrow. This implements in-memory
custody for these concrete paths, not durable recovery, shared public views or the outer
orchestration's release gate.

Stat, inventory, conditional removal and metadata/directory convergence also attach their original
binding and working state before the first exchange. The exchange validates its request before
carrier dispatch; metadata additionally validates names and options before lookup. Metadata retains
the owner/group lookup result before settlement under the same borrow as the mutation. Outcome
capture keeps exact original bindings and unfinished facts, while failed capture leaves working
state attached. This extends the same in-memory custody boundary; it does not establish a durable
recovery handoff or public API.

Two decisions remain separate: whether old work can still cause effects, and who owns unfinished
cleanup. Unresolved execution or coordination prevents conflicting admission and claim release.
Proved-inert cleanup debt still needs an explicit owner, but does not by itself require indefinite
VM exclusion. The current private `requires_owner_retention` summary combines these reasons and must
not become the generic claim-release predicate. The outer workflow retains its claim for its own
remaining work regardless of whether one file call has finished. Initial cleanup remains under the
same operation; a later garbage collector under fresh admission is not introduced here.

An in-memory handoff is insufficient for process-loss recovery. Before core drops its last working
state or releases a claim with unfinished responsibility, the concrete recovery path must preserve
the exact cleanup binding and facts durably. Tokens needed to reconcile an interrupted dispatch must
be retained before dispatch, not reconstructed only from a returned outcome. Never persist file
contents or source streams as a substitute. The recovery integration must prove this handoff before
public FileAccess is production-ready; retaining a claim row alone does not satisfy it.

Sequential fixed-helper calls also need a separate lifetime decision. For the exact supported
foreground launch chain, the candidate termination rule is submitted dispatch plus an independently
observed remote exit code of zero. The helper creates no background work, and its supported launcher
must wait for it; native acceptance must prove that chain for each identity/carrier path. A signal
or nonzero command-chain exit is not enough: terminating a supervising `sudo` process can leave its
Python child running. Neither local process status, stream EOF nor an `AGWF1` terminator substitutes
for this rule. Helper effect facts remain valid independently of termination evidence.

Advancing transfer additionally requires a valid successful observation for the preceding request.
Normal-chain completion with missing or refused operation evidence does not authorize replay or
claim success; it permits token-bound reconciliation or exact known cleanup within the same held
ownership and remaining deadline. Without normal-chain completion, stop follow-on exchanges and
retain unresolved ownership, even if a complete helper transcript arrived. A carrier failure does
not erase independently observed normal completion, and a late deadline does not grant a new
dispatch budget. This narrow file-helper rule is not a termination guarantee for arbitrary commands
or jobs. Crash recovery still requires a concrete handoff of the original operation context and
independent no-further-effects evidence; an in-memory file workflow does not supply that handoff.

The database coordinator and RunContext composition are implementation gates, not behavior supplied
by the current database's migration/use locks. Until they are implemented and proved, the private
file primitives require caller-owned serial execution and are not a production FileAccess surface. A
destination-side lock may be added only for a demonstrated residual race that this ownership
boundary cannot address; it is not a default prerequisite or a defense against malicious platform
code or target-user processes.

The request decoder validates paths and extracts a single nonempty leaf without separators, NUL or
dot components before calling the private object primitives. Those typed interior helpers do not
repeat the request decoder. The decoder also validates the remaining time budget before file I/O: a
finite nonnegative duration encoded as a JSON float, or null for the explicit unbounded choice,
never an integer, boolean, NaN or infinity. It derives a guest-local monotonic expiry; workstation
monotonic timestamps cannot be used on the destination. Private filesystem primitives consume that
validated expiry rather than acting as a second request decoder. The host's original deadline
continues to bound the complete multi-attempt operation.

`Match` is atomic only with respect to writers serialized by the same operation coordinator: the
helper compares the revision and renames while that operation retains ownership. An unrelated
application or independent controller does not participate in that coordination. No portable
Debian/macOS primitive atomically compares an observed arbitrary destination revision and replaces
or unlinks that same revision. Therefore an external writer can race the final check and
rename/unlink window. Within the revised threat boundary, the helper refuses observed links and
special objects, but it cannot promise external-writer compare-and-swap. Create-only no-replace
remains atomic where the named filesystem supports the platform primitive.

This limit is visible in documentation and tests. It is acceptable only where domain ownership or
service coordination makes external writers non-adversarial. Tmux/session code must coordinate
server absence and socket-directory ownership around `remove`; a fresh server racing after the
confirmation is not made safe by FileAccess. If a destination requires adversarial external CAS,
conditional removal against hostile writers, the current platform primitives do not satisfy it and
the location cannot enter the initial allowlist without a different mechanism or operator
disposition.

`remove` requires both a `Revision` and exact kind. It returns unchanged when the observed object is
absent and conflicts on a different revision or kind. Regular files require link count one;
directories must be empty; sockets require `FileKind.SOCKET`. FileAccess performs no tmux liveness
probe. The session owner supplies a revision only after its separately tested runtime-absence check.
There is no recursive removal and no FIFO path.

## Immediate mechanics versus deferred permission activation

The [delivery-stage contract](execution-contract.md#delivery-stages-and-permission-activation) owns
permission timing and immediate operational guarantees. Database operation coordination, helper,
metadata/platform, carrier I/O, and no-staging candidates above remain hard enablement gates. During
coexistence the file service constructs an exact-operation confinement request from the explicit
destination; migration records the intended recipient action, root, identity, elevation,
owner/group/mode, and content risk outside runtime policy.

At physical legacy removal, `file_policy.py` introduces the reviewed immutable catalog and bound
recipient subsets into composition. The only values needed are exact-file versus subtree scope,
separate approved-root creation/removal, closed action sets, elevation, and owner/group/mode limits.
Composition alone resolves trusted identity/session roots. It intersects catalog and recipient
values, rejects known denial before staging, and passes the resulting trusted root plus relative
path to the same helper confinement protocol. The helper does not parse plugin policy. Isolated
policy tests may be written before activation, but production code must not withhold or advertise
authority from those values until the removal PR wires them with no legacy bypass.

## Outcomes and error matrix

`MutationResult` is returned only with a complete, matching helper response. `CHANGED` means the
helper confirmed the mutation; `UNCHANGED` means it proved no mutation was needed. Neither result
contains old bytes, a diff, a hash, attributes, helper paths, or carrier diagnostics.

| Condition                                                                                                                                                                               | Public signal                                           | Destination effect and replay rule                                                                       |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| Invalid path, bound, JSON value, strategy combination, owner/group/mode, or protocol value                                                                                              | `ValidationError`                                       | No target I/O when locally knowable; never replay by changing meaning.                                   |
| Post-removal catalog or recipient denial                                                                                                                                                | `AuthorizationError`                                    | Before helper deployment/staging when knowable; no effect. Not active during coexistence.                |
| Missing read/stat target                                                                                                                                                                | `None`                                                  | No mutation.                                                                                             |
| Missing list target, wrong kind, link, hard link, special object, mount change, nonempty directory, or unsupported metadata                                                             | `StateError` with a closed reason code                  | No mutation when refusal precedes a mutating call; otherwise the partial-effect row applies.             |
| Read or inventory bound exceeded                                                                                                                                                        | `LimitExceededError`                                    | No partial public result; streaming calls retain only bounded chunks.                                    |
| Revision mismatch or exhausted JSON retry budget                                                                                                                                        | `ConflictError`                                         | No cooperating-writer overwrite/removal; take a new snapshot before retry.                               |
| Operation admission deadline before dispatch                                                                                                                                            | Proposed shared deadline-category `ExternalError`       | No mutation; retry only as a new caller decision. It is not guest cancellation.                          |
| Helper prerequisite, integrity, or complete pre-publication helper failure                                                                                                              | `ExternalError` or `ConnectivityError` by kind          | Regular-file destination unchanged; owned scratch cleanup may remain.                                    |
| Reported `set_metadata`/`ensure_directory` failure after an in-place step                                                                                                               | `PartialMutationError` with closed completed-step facts | Target may be partially converged; re-observe and converge, never assume rollback.                       |
| Carrier loss/timeout after a mutating helper was dispatched, invalid/truncated response, rename acknowledged only inside an unavailable response, or post-rename sync/cleanup ambiguity | `UncertainOutcomeError` with safe phase/dispatch facts  | Destination may have changed. Do not replay merge, replace, metadata, or removal blindly; observe first. |
| Complete helper refusal before any target mutation                                                                                                                                      | Typed error above                                       | Destination unchanged; helper-owned staging cleanup is bounded separately.                               |

`LimitExceededError`, `ConflictError`, `PartialMutationError`, and `UncertainOutcomeError` are new
kind-based `AgentworksError` subclasses shared by execution operations rather than file-entity
subclasses. Provider exception text and raw errno values remain internal. Multi-file callers
checkpoint each confirmed effect; this slice supplies no transaction and never reports a partial or
uncertain operation as unchanged.

Public reduction follows core custody, never replaces it. Existing error `entity_kind` and
`entity_name` fields carry a core-supplied safe logical target, not filesystem paths or account
names. A small immutable `ErrorDetails` value carries closed phase and primary reason enums; the
shared error module does not import execution implementations. An optional closed effect records a
proved destination change (`Change.CHANGED`) when a later failure prevents successful return. Its
absence does not assert that the destination was unchanged. Partial and uncertain metadata errors
retain only their closed completed/attempted step facts, not the private outcome or helper
transcript.

Destination-effect evidence takes precedence over the primary failure category. An uncertain
publication, removal or metadata mutation raises `UncertainOutcomeError`; confirmed incomplete
metadata convergence raises `PartialMutationError`. The primary deadline, helper or observation
reason remains available separately. A lost read-only ownership lookup or unfinished private staging
can retain operation ownership without implying that the destination changed. Conversely, a
confirmed publication followed by failed cleanup is a known change plus a failure, not an unchanged
result or an unproved publication. A late deadline also prevents successful public return even when
the private operation status records completion.

The current carrier's dispatch and observation categories include local startup and protocol
failures as well as network failures. Neither proves connectivity loss. Reduce those generic facts
to a general external failure without inventing a network cause; `ConnectivityError` requires
independent evidence that these categories do not supply. Upload and download composition retain the
first primary failure's exchange-level phase, dispatch and carrier failure. Later cleanup preserves
that diagnostic cause while updating independent cleanup and ownership facts. Preserve any facts
required by public diagnostics before exposing that boundary; do not manufacture missing precision
from a generic termination category. Concrete private reducers now provide the public value/error
projection; production FileAccess integration remains implementation work.

## Test and evidence plan

Tests live under `tests/execution/files/` and import no legacy runtime module. Adapt behavioral
vectors from `test_native_file_boundaries.py`, `test_harness_settings.py`, artifact publication,
generated sections, and native inventory, but drive only the new API/helper.

1. **Contract/JSON:** cover every bound and strategy, nested objects, atomic arrays/scalars,
   missing/empty/malformed input, duplicate keys, finite numbers, UTF-8, and literal `null` on both
   winning sides. Replace and skip-existing must not parse old bytes or disclose values.
2. **Paths/objects:** cover ancestor/leaf links, Linux magic links, observed hard links,
   traversal/prefix collisions, devices, nonblocking FIFO refusal, sockets, directories, and mount
   changes. Race fixtures verify conflict/refusal for changes observed before publication. Hostile
   same-user ancestor moves and hard-link additions are outside the production guarantee, not
   acceptance gates.
3. **Publication/metadata:** cover every condition, chunk/partial failures, byte and digest checks,
   inherited ACL behavior for creation, direct-write-equivalent access metadata for every existing
   regular-file replacement, security-attribute refusal, lost acknowledgment, in-place metadata
   partial effects, and cleanup debt. Every pre-rename publication failure leaves the old inode
   unchanged.
4. **Concurrency/lifecycle:** multiprocess whole-file and JSON writers preserve unique keys and
   conflict on stale snapshots under database-level operation ownership. Nested helpers share that
   ownership without overlapping conflicting exchanges; disjoint scopes remain independent. Crash,
   timeout and lost-response tests retain unresolved ownership and refuse conflicting work until
   recovery establishes quiescence, without replay. Independent databases and external writers are
   not falsely presented as coordinated. Directory tests cover bounded inventory, limits/order,
   exact creation, empty removal, and no implicit parents/recursive delete. Remove a real tmux
   socket only after liveness proves absence; replacement yields conflict or uncertainty. An
   external writer fixture demonstrates, but does not overclaim, the non-CAS limit.
5. **Carrier/bootstrap:** prove `SinkOutput` on SSH and QGA with reflected input,
   malformed/truncated envelopes, bounded parser state, and no raw retention. Prove shared private
   scratch data transfer, fixed inline bundles, complete request bounds, exact offsets/digests,
   native finalization if selected, cleanup interruption, and startup with legacy unavailable.
6. **Live/permissions:** before enablement, run SSH/QGA on clean pre-Phase-B Debian 12/13 for both
   CPUs and SSH on each supported macOS/CPU, recording tools, filesystems, rename, metadata,
   scratch, identity, chunks, and faults. No-staging readiness tests prove already-available bounded
   read/stat or refusal with no deploy/spool/lock creation. Separately test future grants in
   isolation, coexistence non-enforcement/unchanged legacy checks, then removal-time activation.

## Open gates and compatibility costs

The following are not established by source inspection and must remain open in the lead's plan:

- prove early `python3` installation for new Debian guests and Bookworm Python 3.11 compatibility;
  define existing-guest native recovery when Python is absent, without implicit readiness install;
- implement the preinstalled macOS Python prerequisite check, including clean missing/version/shim
  diagnostics, and prove metadata and execution behavior; guest provisioning does not satisfy it;
- jointly prove the carrier I/O LLD's `SinkOutput` on SSH and QGA without raw response retention;
- prove fixed inline bundles and preparation's private data-scratch transfer within complete
  provider-body and workstation-command limits, including the candidate 24 KiB chunk;
- prove a no-staging, already-available bounded read/stat substrate for every readiness workflow;
  establish a prerequisite earlier only where the workflow contract permits, otherwise treat a
  mandatory no-write read as a delivery gate requiring operator decision;
- inventory finite transfer sizes, network/nonlocal filesystems, every authorized execution
  identity/elevation choice, and each path written by multiple effective identities; prove all
  participating writers share database-level operation ownership and recovery;
- prove unique-sibling atomic rename and the required creation/update owner, mode, and ACL semantics
  on every supported filesystem, refusing unsupported metadata before publication;
- decide the supported macOS minimum and Windows-local download publication design;
- complete directory transfer/confined extraction before claiming full R7;
- inventory the exact future core catalog and recipient subsets before removal; and
- obtain operator disposition for any required destination that cannot meet atomic rename, metadata
  preservation, mount confinement, or conditional-removal assumptions.

These gates are compatibility facts, not permission to import the legacy helper, expose exec to a
file-only caller, silently drop metadata, install a runtime during readiness, retry under broader
elevation, or activate grants early.
