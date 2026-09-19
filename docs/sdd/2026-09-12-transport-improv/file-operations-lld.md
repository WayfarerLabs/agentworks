# File Operations: First Implementation Slice

<!-- cspell:ignore aclxattrs atx bytewise chmods ctime datagram errno fsync lstat mountpoint -->
<!-- cspell:ignore multiprocess netstrings noexec nofollow nonblocking nonlocal noreplace -->
<!-- cspell:ignore openat openatx overclaim pread pwrite statx xattrs -->

- Status: Proposed low-level design; implementation and live feasibility remain unproven.
- Governing requirements: [FRD R7](frd.md#r7-files), including the
  [2026-09-19 staged-permission ruling](frd.md#operator-rulings-2026-09-19).
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
be adapted, but the new runtime does not call the legacy stack. The shipped helper's Python3
dependency, check-then-rename conflict window, public runner, mutable staging slots, and root-only
string allowlist are not carried forward.

The first slice needs one small file helper, invoked as a subprocess through the new `Carrier`.
Shell built-ins do not expose the required descriptor-relative object handling, while Python is
unavailable before Phase B on some required paths. A packaged native executable is one candidate,
not an approved technology choice; prerequisite/runtime alternatives and their phase availability
must be compared before implementation. In every case, the helper is a closed operation protocol,
not a daemon, agent, remote execution escape, or general file-policy engine.

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
def write_file(path, data, *, condition, create_metadata, preserve_existing_metadata=True, sudo=False) -> MutationResult: ...
def upload(path, source, *, size, condition, create_metadata, preserve_existing_metadata=True, sudo=False) -> MutationResult: ...
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

The leading candidate is a small native program with no language runtime or network access: package
digest-pinned artifacts for Linux `x86_64`/`aarch64` and both macOS architectures, built against the
oldest supported ABI/OS. This is a substantial unapproved build and distribution cost. Before
selection, the lead and operator must compare it with an already-established prerequisite
runtime/helper and with deferring operations unavailable before that prerequisite. Any selection
must prove phase availability, descriptor APIs, compiler/provenance pipeline, package layout,
supported ABI/OS/CPU, and macOS execution behavior. Runtime download, on-target compilation, and a
legacy-helper fallback are outside this design.

For the native candidate, `execution/file_helper.py` would own an operation-lifetime
`HelperSession`:

1. Select the asset from core-observed OS/architecture; caller input cannot select it.
2. Create an unpredictable mode-0700 directory under `/run/agentworks/file-ops` for elevated Debian,
   the bound user's `.cache/agentworks/file-ops` for ordinary Debian, or
   `Library/Caches/Agentworks/file-ops` under the bound macOS home.
3. Fixed `/bin/sh` bootstrap source writes numbered base64 parts, privately concatenates/decodes
   them, verifies the packaged SHA-256 with an absolute tool, chmods 0700, and atomically names the
   helper. Ambiguity abandons that directory; never append/replay into it.
4. An absolute-path `hello` under a clean core environment must match version, digest, OS/CPU,
   effective identity, and features. Close removes only recorded objects; failure records cleanup
   debt without hiding the primary outcome.

The bootstrap tool matrix is explicit: Debian needs `/bin/sh`, `/bin/mkdir`, `/bin/cat`,
`/bin/chmod`, `/bin/rm`, `/usr/bin/base64 -d`, `/usr/bin/sha256sum`, and `/usr/bin/uname`; macOS
needs the same basic absolute tools, `/usr/bin/base64 -D`, `/usr/bin/shasum -a 256`, and
`/usr/bin/uname`. No `PATH`, startup file, Python, Phase B package, Tailscale route, or caller
cwd/env is used. Missing tools, a `noexec` scratch root, unsupported architecture, digest mismatch,
or helper feature mismatch refuses before destination mutation. These paths and tool spellings are
design inputs, not evidence: clean Debian 12/13 images and supported macOS host versions still need
live proof.

Every exchange is one `Carrier.execute` with literal absolute argv. Its ASCII `AGWF1` envelope has a
32-hex request ID, one closed operation, unique named base64/decimal fields, and a terminator. The
response must match the ID and contain closed status/phase values. Strict field/line/decoded/total
bounds and a 32 KiB raw chunk keep it below QGA's 65,536-byte input cap. No request value becomes
argv or shell source; fixed bootstrap source cannot be reused for file operations.

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

## Confinement and filesystem mechanics

All target operations occur in the helper process. Host-side normalization and later grant checks
are early refusals, never the security mechanism.

- Walk absolute ancestors from an open root descriptor and retain directory descriptors through the
  mutation. Linux uses `openat2` with `RESOLVE_BENEATH`, `RESOLVE_NO_SYMLINKS`,
  `RESOLVE_NO_MAGICLINKS`, and, once below a trusted policy root, `RESOLVE_NO_XDEV`. macOS walks one
  component at a time with `openat(..., O_NOFOLLOW | O_DIRECTORY)`, comparing object and filesystem
  identity before the final operation. Unsupported safe primitives are a platform blocker, not a
  string-prefix fallback.
- During coexistence an existing mount on the exact path is explicit, but a mount change during the
  call fails. After activation, bind the approved root mount and reject descendant crossings.
  Network filesystems or filesystems lacking local rename/lock semantics are unsupported proof
  obligations.
- Open regular leaves with no-follow and nonblocking flags, then verify by descriptor. Reject every
  symlink, magic link, device, FIFO, and unexpected socket. Reject regular files with link count
  other than one, preventing a hard-link alias from carrying reads or mutations outside the named
  location.
- Publication creates a sibling staging inode, writes and verifies all bytes, applies metadata,
  syncs the staged file, revalidates the destination condition, and uses descriptor-relative rename.
  Create-only uses Linux `renameat2(RENAME_NOREPLACE)` and macOS `renameatx_np(RENAME_EXCL)`; lack
  of filesystem support is a refusal. Replacement uses the platform's atomic same-directory rename.
  Atomic visibility is promised; crash durability of the directory entry is not yet promised.
- Cleanup names and recorded inode identity belong to the operation. Cleanup never scans a prefix or
  removes a name whose identity changed. A cleanup failure leaves bounded debt for the owner; it
  does not authorize broader deletion.

New files clear inherited ACLs/attributes before exact owner/group/mode is applied. Replacement with
`preserve_existing_metadata=True` copies UID, GID, mode, all readable extended attributes, and
access ACLs before rename. Any unsupported/denied copy leaves the old destination unchanged. macOS
BSD flags that cannot be applied before rename are an explicit refusal, never silent loss.

`set_metadata` changes owner/group, then mode on the held object. It preserves ordinary extended
attributes. Preflight rejects a nontrivial access ACL whose effective mask would change under the
requested mode. After the first system call, failure can leave a known partial change; it is not
described as atomic or unchanged. Recursive metadata and arbitrary ACL/security attribute mutation
are not exposed. The same partial-effect rule applies when `ensure_directory` has created its final
component but cannot finish metadata convergence.

## Cooperating writers and honest limits

Every new-stack read, stat, list, JSON update, publication, directory, metadata, and removal call
must participate in a canonical lock scope derived from the composition-bound machine target and
normalized path, not effective identity or a caller-selected root. Reads/snapshots take a shared
target lock. Mutations take shared ancestor locks and an exclusive target lock. A directory
inventory takes an exclusive target lock, so cooperating descendant operations block on their shared
ancestor lock and cannot produce a torn inventory. Locks are acquired root-to-leaf and
deadline-bounded.

That rule intentionally requires ordinary-user and elevated helpers touching the same path to
coordinate. A per-user cache lock cannot satisfy it. A stable, identity-neutral lock namespace or a
single broker identity is only a candidate: its safe creation, permissions, lifecycle, collision
resistance, and operation before permission activation remain unproved on Debian and macOS. The
migration inventory must find every cross-identity path, and no affected consumer may migrate until
one protocol is proven. Excluding a required admin/user workflow needs operator disposition.

`Match` is atomic only with respect to those cooperating writers: the helper compares the revision
and renames while holding their exclusive lock. A non-cooperating process ignores the lock. No
portable Debian/macOS primitive atomically compares an observed arbitrary destination revision and
replaces or unlinks that same revision. Therefore an external writer can race the final check and
rename/unlink window. The helper still prevents path escape, link following, and special-object
blocking, but it cannot promise external-writer compare-and-swap. Create-only no-replace remains
atomic where the named filesystem supports the platform primitive.

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

The additive and consumer-migration releases enforce operational safety immediately: explicit
destination and elevation, actual OS permissions, bounds, helper integrity, link/object/mount
handling, metadata rules, cooperating-writer locks, sensitivity, and truthful outcomes. Existing
legacy calls and their current checks remain unchanged. New code never dispatches a mutation through
both stacks.

Those releases do **not** enforce or claim the new recipient grants or successor core allowlist.
There is no allow-all toggle, shadow decision callback, compatibility policy service, or fallback to
public execution. During coexistence the file service constructs an exact-operation confinement
request from the explicit destination; migration records the intended recipient action, root,
identity, elevation, owner/group/mode, and content risk outside runtime policy.

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
2. **Paths/objects:** cover ancestor/leaf links, Linux magic links, hard links, traversal/prefix
   collisions, devices, nonblocking FIFO refusal, sockets, directories, mount changes, and
   synchronized ancestor/leaf swaps. No race may escape the held directory.
3. **Publication/metadata:** cover every condition, chunk/partial failures, byte and digest checks,
   pre-visibility metadata, Linux and macOS ACL/xattrs, BSD-flag refusal, lost acknowledgment,
   in-place metadata partial effects, and cleanup debt. Every pre-rename publication failure leaves
   the old inode unchanged.
4. **Concurrency/lifecycle:** multiprocess whole-file and JSON writers preserve unique keys and
   conflict on stale snapshots; inventory does not tear; lock waits are bounded. Directory tests
   cover exact creation, limits/order, empty removal, and no implicit parents/recursive delete.
   Remove a real tmux socket only after liveness proves absence; replacement yields conflict or
   uncertainty. An external writer fixture demonstrates, but does not overclaim, the non-CAS limit.
5. **Carrier/bootstrap:** prove `SinkOutput` on SSH and QGA with reflected input,
   malformed/truncated envelopes, bounded parser state, no raw response retention, exact offsets,
   identity/digest/features, missing tools, `noexec`, cleanup interruption, and startup with legacy
   unavailable.
6. **Live/permissions:** before enablement, run SSH/QGA on clean pre-Phase-B Debian 12/13 for both
   CPUs and SSH on each supported macOS/CPU, recording tools, filesystems, rename/locks, metadata,
   scratch, identity, chunks, and faults. Separately test future grants in isolation, coexistence
   non-enforcement/unchanged legacy checks, then removal-time activation with no bypass.

## Open gates and compatibility costs

The following are not established by source inspection and must remain open in the lead's plan:

- select a helper technology and prove its pre-Phase-B availability; if native is selected, approve
  its build, packaging, provenance, ABI/OS/architecture, and macOS execution cost;
- jointly prove the carrier I/O LLD's `SinkOutput` on SSH and QGA without raw response retention;
- measure clean-image bootstrap tools and executable scratch roots, including early QGA recovery and
  macOS placement hosts;
- inventory finite transfer sizes, network/nonlocal filesystems, and every path written by multiple
  effective identities or adversarial external writers; prove a cross-identity lock protocol for
  required admin/user workflows;
- decide the supported macOS minimum and Windows-local download publication design;
- complete directory transfer/confined extraction before claiming full R7;
- inventory the exact future core catalog and recipient subsets before removal; and
- obtain operator disposition for any required destination that cannot meet atomic rename, metadata
  preservation, mount confinement, or conditional-removal assumptions.

These gates are compatibility facts, not permission to import the legacy helper, expose exec to a
file-only caller, relax confinement, silently drop metadata, or activate grants early.
