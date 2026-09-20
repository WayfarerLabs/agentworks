# File Operations: First Implementation Slice

<!-- cspell:ignore aclxattrs atx bytewise chmods ctime datagram errno fsync lstat mountpoint -->
<!-- cspell:ignore multiprocess netstrings noexec nofollow nonblocking nonlocal noreplace -->
<!-- cspell:ignore openat openatx overclaim pread pwrite statx xattrs -->

- Status: Proposed low-level design; implementation and live feasibility remain unproven.
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

Every in-memory read has a positive caller-selected byte bound; a product default may be offered,
but there is no universal total-file ceiling. Streaming upload declares its exact finite size, and
download obtains a finite size from the held source snapshot before transferring. Chunk buffers
remain bounded. Directory inventory defaults to 1,024 entries, depth 1, and 1 MiB encoded; reviewed
callers may request up to 4,096 entries, depth 8, and 4 MiB encoded.

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

`read_file` returns `None` only for absence. It opens regular files nonblocking and rejects every
other object before reading, so a FIFO or device cannot hang the operation. `stat` reports the
closed `FileKind` set above and rejects links or unsupported special objects. `list_directory` never
follows links, crosses a descendant mount, or returns partial data: exceeding an entry, depth, name,
or encoded-output bound is a limit error. Sorting is bytewise by relative UTF-8 path so SSH and QGA
return the same order.

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
cooperating-writer conflict it repeats snapshot, merge, and conditional publication up to eight
times within the caller's one deadline, then raises `ConflictError`. `replace` and `skip-existing`
do not gain accidental old-document parsing. TOML and generated-section transformations stay in
their resource domains and use snapshot plus `Match`; FileAccess accepts no transform callback.

## Helper deployment and protocol

Debian guest operations use a standard-library helper that supports the Bookworm distribution's
Python 3.11. New-guest provisioning adds `python3` to the early apt package list and must prove the
interpreter is available before helper-dependent work. This does not install Python during a file
operation or readiness check. Existing native-recovery targets without Python need an independent
bootstrap path. macOS placement hosts must already provide compatible Python 3.11 or newer;
availability checks and platform proof remain open. New guest packages do not satisfy that host
prerequisite. Runtime download, on-target compilation, elevation retry, and a legacy-helper fallback
are outside this design.

`execution/file_helper.py` owns an operation-lifetime `HelperSession`. Helper delivery reuses the
preparation LLD's private scratch transfer: a core-selected helper is written at exact offsets into
operation-owned scratch and length/digest verified. It does not add a numbered-part/base64
deployment state machine. The shared 24 KiB raw chunk is a candidate pending SSH/QGA whole-request
proof, not a file-layer constant. An absolute-path `hello` under the clean core environment must
match version, digest, interpreter, OS/CPU, effective identity, and features. Close removes only
recorded objects; uncertain cleanup is owner debt and never hides the primary outcome.

Every exchange is one `Carrier.execute` with literal absolute argv. Its ASCII `AGWF1` envelope has a
32-hex request ID, one closed operation, unique named base64/decimal fields, and a terminator. The
response must match the ID and contain closed status/phase values. Strict field/line/decoded/total
bounds inherit the preparation substrate's proved carrier limits. No request value becomes argv or
shell source; fixed preparation bootstrap source cannot be reused for file operations.

The existing buffered carrier cannot safely carry this protocol as written: sensitive input makes it
suppress the response, while ordinary `Capture` can flow toward public execution results. Reuse the
carrier I/O LLD's internal `SinkOutput(collector)` candidate, subject to its joint SSH proof. The
collector incrementally parses the file schema and retains no raw response. It accepts only a
matching response frame and closed typed fields; download/snapshot bytes stream directly into the
private destination/spool while offsets and hashes are checked. Its tiny framing buffer is bounded
and cleared on rejection. Unexpected account-hook output, including reflected request bytes, is
rejected without entering an execution result or diagnostic. The carrier report contains only the
delivery/retention facts defined by the carrier I/O LLD; the collector alone owns the typed file
outcome. Ordinary sensitive-output suppression remains unchanged. This file schema, not a generic
private raw-capture mode, decides which typed content may survive.

Closed helper operations are `hello`, `stage_begin`, `stage_chunk`, `publish`, `snapshot_begin`,
`snapshot_chunk`, `stat`, `list`, `ensure_directory`, `set_metadata`, `remove`, and `cleanup`.
Staging is created beside the destination with mode 0600 and an unpredictable helper-owned name.
Chunks use exact offsets and hashes; final size and SHA-256 must match before publication. Snapshot
chunks come from a private complete spool, not repeated reads of a changing source. The host checks
the end-to-end size/digest too. No helper operation accepts executable names, arbitrary flags,
environment, cwd, source text, callbacks, or a destination outside its single request.

### No-staging readiness gate

The private Linux `_file_read.py` implementation now composes the existing snapshot reader with a
fixed bundled helper and a file-specific `AGWF1` collector. It checks an explicitly bound identity
before path access and sends caller paths only through sensitive stdin. Complete length, digest,
metadata, framing and carrier-stream evidence are required before returning file bytes. Root or leaf
absence remains distinct from refusal and incomplete observation. The helper performs no deployment,
spool, mutation or lock creation. This implements a local bounded-read candidate, not stat-only
support, transaction locking, production FileAccess or native SSH/QGA/macOS acceptance. The
remaining readiness gates below still apply.

Preparation readiness permits no helper deployment, private scratch, spool, or new lock state.
FileAccess may expose only bounded `read_file` and `stat` there, and only through an
already-available trusted substrate proved to meet file confinement, object, sensitivity, and
truthful-result rules within the inline bound. The staged helper described above cannot satisfy this
gate. Without such a substrate, an optional call refuses before dispatch, while a required workflow
must establish its prerequisite before entering readiness. Relocation to a staging-capable phase is
allowed only when the existing workflow contract permits it; a mandatory readiness read that cannot
remain no-write is a delivery gate requiring operator decision. Absence never authorizes public
execution or silent removal of a required workflow. The early-Python versus native-helper decision
is settled for Debian guests and macOS hosts; already-available readiness execution and the macOS
platform mechanics remain unproved.

## Confinement and filesystem mechanics

The private [`_file_snapshot.py`](../../../cli/agentworks/execution/_file_snapshot.py) experiment
implements descriptor-relative bounded regular-file observation with standard-library Python 3.11
syntax. It rejects observed special objects before leaf open, retains no-follow/nonblocking checks
after that observation, and binds bytes to a digest and before/after identity/metadata. It borrows
the trusted root descriptor and assumes the caller owns any cooperating-writer lock; it creates
neither locks nor files. Local fixtures cover object refusal, observed replacement, byte bounds and
owned-descriptor cleanup. This is not FileAccess or native platform acceptance.

On Linux x86-64 and AArch64, `_file_paths.py` now supplies `openat2` for every descendant open with
the four resolution restrictions below. Kernel/ABI unavailability refuses without a weaker fallback.
The separate non-Linux POSIX walk detects changed `st_dev`, not same-filesystem bind mounts. Local
Linux fixtures exercise real symlink, proc descriptor magic-link and descendant-mount refusal;
same-filesystem bind-mount and native macOS proof remain open. A regular-file read still has no hard
elapsed-time bound. This increment does not resolve helper cancellation or close readiness,
cross-identity locking and complete platform acceptance.

The private `_file_publication.py` increment implements Linux sibling publication for complete
in-memory bytes beneath a caller-owned parent descriptor. It uses no-replace rename for creation,
snapshot revalidation for replacement, and preserves ordinary UID/GID/mode/access-ACL semantics
while refusing unsupported metadata. The caller still owns confinement and transaction locking; the
increment does not provide streaming upload, remote delivery, macOS support or FileAccess. Local
fault and ACL fixtures are implementation evidence, not acceptance of the complete platform
guarantees below.

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

## Cooperating writers and honest limits

All cooperating helper filesystem transactions on one machine use one identity-neutral, exclusive
machine-level lock. There is no path hierarchy or shared-lock protocol. Acquisition and the critical
section obey the caller's deadline. Upload bytes may reach verified private scratch before locking;
publication then locks, rechecks the condition, and renames. A read locks until its immutable
snapshot is materialized, then transfers chunks outside the lock. Stat releases after observation;
list materializes its bounded result before release. Inventory remains a bounded set of
observations, not a globally coherent filesystem view.

Ordinary-user and elevated helpers must open the same lock. A per-user cache cannot satisfy that
contract. Safe creation, permissions, lifecycle, and availability of an identity-neutral namespace
before permission activation remain unproved on Debian and macOS. The migration inventory must find
every cross-identity path, and no affected consumer may migrate until the protocol is proven.
Excluding a required admin/user workflow needs operator disposition.

`Match` is atomic only with respect to those cooperating writers: the helper compares the revision
and renames while holding the transaction lock. A non-cooperating process ignores the lock. No
portable Debian/macOS primitive atomically compares an observed arbitrary destination revision and
replaces or unlinks that same revision. Therefore an external writer can race the final check and
rename/unlink window. Within the revised threat boundary, the helper refuses observed links and
special objects, but it cannot promise external-writer compare-and-swap. Create-only no-replace
remains atomic where the named filesystem supports the platform primitive.

This limit is visible in documentation and tests. It is acceptable only where domain ownership or
service coordination makes external writers non-adversarial. Tmux/session code must coordinate
server absence and socket-directory ownership around `remove`; a fresh server racing after the
confirmation is not made safe by FileAccess. If a destination requires adversarial external CAS,
conditional removal, or non-cooperating writers, the current platform primitives do not satisfy it
and the location cannot enter the initial allowlist without a different mechanism or operator
disposition.

`remove` requires both a `Revision` and exact kind. It returns unchanged when the observed object is
absent and conflicts on a different revision or kind. Regular files require link count one;
directories must be empty; sockets require `FileKind.SOCKET`. FileAccess performs no tmux liveness
probe. The session owner supplies a revision only after its separately tested runtime-absence check.
There is no recursive removal and no FIFO path.

## Immediate mechanics versus deferred permission activation

The [delivery-stage contract](execution-contract.md#delivery-stages-and-permission-activation) owns
permission timing and immediate operational guarantees. The cross-identity lock, helper,
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
| Lock deadline                                                                                                                                                                           | Proposed shared deadline-category `ExternalError`       | No mutation; retry only as a new caller decision. It is not guest cancellation.                          |
| Helper prerequisite, integrity, or complete pre-publication helper failure                                                                                                              | `ExternalError` or `ConnectivityError` by kind          | Regular-file destination unchanged; owned scratch cleanup may remain.                                    |
| Reported `set_metadata`/`ensure_directory` failure after an in-place step                                                                                                               | `PartialMutationError` with closed completed-step facts | Target may be partially converged; re-observe and converge, never assume rollback.                       |
| Carrier loss/timeout after a mutating helper was dispatched, invalid/truncated response, rename acknowledged only inside an unavailable response, or post-rename sync/cleanup ambiguity | `UncertainOutcomeError` with safe phase/dispatch facts  | Destination may have changed. Do not replay merge, replace, metadata, or removal blindly; observe first. |
| Complete helper refusal before any target mutation                                                                                                                                      | Typed error above                                       | Destination unchanged; helper-owned staging cleanup is bounded separately.                               |

`LimitExceededError`, `ConflictError`, `PartialMutationError`, and `UncertainOutcomeError` are new
kind-based `AgentworksError` subclasses shared by execution operations rather than file-entity
subclasses. Provider exception text and raw errno values remain internal. Multi-file callers
checkpoint each confirmed effect; this slice supplies no transaction and never reports a partial or
uncertain operation as unchanged.

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
   conflict on stale snapshots under the machine transaction lock; lock waits are bounded and
   immutable snapshot/chunk transfer occurs outside it. Directory tests cover bounded inventory,
   limits/order, exact creation, empty removal, and no implicit parents/recursive delete. Remove a
   real tmux socket only after liveness proves absence; replacement yields conflict or uncertainty.
   An external writer fixture demonstrates, but does not overclaim, the non-CAS limit.
5. **Carrier/bootstrap:** prove `SinkOutput` on SSH and QGA with reflected input,
   malformed/truncated envelopes, bounded parser state, and no raw retention. Prove shared private
   scratch helper delivery, the candidate 24 KiB bound, exact offsets/digests, native finalization
   if selected, cleanup interruption, and startup with legacy unavailable.
6. **Live/permissions:** before enablement, run SSH/QGA on clean pre-Phase-B Debian 12/13 for both
   CPUs and SSH on each supported macOS/CPU, recording tools, filesystems, rename/locks, metadata,
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
- reuse and prove preparation's private scratch transfer and candidate 24 KiB bound; do not add a
  file-specific deployment protocol;
- prove a no-staging, already-available bounded read/stat substrate for every readiness workflow;
  establish a prerequisite earlier only where the workflow contract permits, otherwise treat a
  mandatory no-write read as a delivery gate requiring operator decision;
- inventory finite transfer sizes, network/nonlocal filesystems, every authorized execution
  identity/elevation choice, and each path written by multiple effective identities; prove the
  cross-identity lock protocol for required admin/user workflows;
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
