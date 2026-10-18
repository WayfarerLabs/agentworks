# Directory and local-path transfers

This response implements the existing [R7 file requirements](frd.md#r7-files) and
[ordinary-call composition](file-operations-lld.md#generic-directory-transfer-child-checkpoints). It
adds no grant, execution capability, transfer daemon, package checkpoint, recursive removal or
directory transaction. Native and complete RunContext acceptance remain separate gates.

## Public forms

Add these frozen values in `execution/files.py`:

```python
class ExistingFile(StrEnum):
    ERROR = "error"
    PRESERVE = "preserve"
    REPLACE = "replace"

@dataclass(frozen=True)
class DirectoryTransferLimit:
    max_bytes: int
    max_entries: int = 1_024
    max_depth: int = 8

@dataclass(frozen=True, repr=False)
class DirectoryTransferEntry:
    relative_path: PurePosixPath
    kind: FileKind
    change: Change
    transferred_bytes: int

@dataclass(frozen=True, repr=False)
class DirectoryTransferProgress:
    entries: tuple[DirectoryTransferEntry, ...]
    transferred_bytes: int
```

The transfer limit counts the root as one entry at depth zero. Support at most 4,096 entries and
depth eight, and a caller-selected finite aggregate byte bound from zero through `2**63 - 1`. Zero
permits empty files, not extra uncounted bytes. Bound every relative UTF-8 path to 4,096 bytes; the
entry and depth limits bound manifest/progress memory independently of content size. These are
transfer limits, not a change to the standalone `DirectoryLimit` inventory contract.

The root entry uses `PurePosixPath(".")` and must be a directory; other entries use nonempty
normalized relative paths. Progress contains only regular files and directories, never source
contents, absolute source/destination paths or private helper outcomes. Its byte count is the sum of
confirmed transferred bytes, not preserved-file sizes or expected remaining work. A returned
progress value means the entire invocation completed; the same shape attached to an exception
describes only earlier settled entries.

Add these methods to the bound `FileAccess`:

```python
def upload_file(path: PurePosixPath, source: Path, *,
                condition: WriteCondition, create_metadata: NewMetadata,
                sudo: bool = False) -> MutationResult: ...

def create_directory(path: PurePosixPath, *, metadata: NewMetadata,
                     sudo: bool = False) -> MutationResult: ...

def upload_directory(path: PurePosixPath, source: Path, *,
                     limit: DirectoryTransferLimit, existing_files: ExistingFile,
                     file_metadata: NewMetadata, directory_metadata: NewMetadata,
                     sudo: bool = False) -> DirectoryTransferProgress: ...

def download_directory(path: PurePosixPath, destination: Path, *,
                       limit: DirectoryTransferLimit, existing_files: ExistingFile,
                       sudo: bool = False) -> DirectoryTransferProgress: ...
```

`upload_file` owns the admitted local source and its cleanup. Existing `upload` continues to borrow
the caller's stream and requires its declared size; it never starts closing caller-owned objects.
`create_directory` is create-only, unlike the existing metadata-converging `ensure_directory`. It
refuses an existing entry without changing its metadata. Neither method silently falls back to
another operation after conflict.

Directory transfers merge structure. Existing ordinary directories are accepted without ownership,
mode or ACL changes. Missing directories are created one level at a time beneath existing admitted
parents. Upload applies the explicit new-file/new-directory metadata only to created objects;
ordinary Replace retains its existing-file preservation/refusal semantics. Download creates local
directories as the workstation user with ordinary platform inheritance and POSIX mode 0700; new
files keep the existing download publisher's private creation policy. Do not apply guest UID/GID or
pretend to preserve an archive's ACLs, timestamps, hard links, sparse layout or other metadata.

`ERROR` refuses an existing regular destination. `PRESERVE` leaves it untouched and records zero
transferred bytes. `REPLACE` uses explicit existing-file Replace, and Create when absent. Any other
destination kind refuses. Do not prune unmentioned entries, replace a directory with a file, or
retry a create/replace conflict after observing changed state. Directory creation that races another
creator fails rather than converging that creator's metadata.

## Bounded traversal and one budget

Validate known arguments and select one original finite composition deadline before I/O. Bind an
ordinary sibling `FileAccess` view whose deadline policy returns that exact object, sharing the same
carrier, plans, root and `FileOperation`. Do not acquire a second owner or refresh a deadline per
child. Existing per-child grant/path checks remain on every ordinary operation; no authority is
inferred from a successful inventory.

Build a bounded manifest before destination mutations, refusing links, hard-linked regular sources,
special objects, unsupported path spellings and unsupported mount changes. A local inventory uses
admitted host directory objects and records the file identity/size needed for subsequent source
admission. A changed source is a conflict, not a reason to rebuild the manifest automatically.

Remote traversal composes ordinary one-level inventories. Each child directory is explicitly
visited, including a directory at the requested depth boundary: it may be empty, but any deeper
entry exceeds the transfer limit. Standalone inventory's `max_depth` merely scopes its traversal; a
successful depth-eight inventory does not prove that deeper descendants are absent. Keep one
aggregate entry count, path bound and byte budget across the walk. Each concrete inventory remains
bounded by the existing response limit. Do not present repeated inventories as one atomic snapshot.

Create/converge no destinations until the bounded manifest is complete. Process parents before
children in deterministic order. Before each regular-file child, apply the remaining aggregate byte
budget to its exact upload size or held download snapshot. Retain empty directories and files.
Neither preflight manifest sizes nor earlier observations replace the child's own current object
checks. Other calls in the same operation may run between settled children; the tree is not a
transaction against cooperating or external writers.

## Host primitives and custody

Use concrete private local-source, local-directory and local-inventory owners in the new execution
package. Constructors are passive; the owning file call retains each object before its first open,
read, scan or create. Each owner exposes its actual cleanup facts and a cleanup-only close method.
Keep local filenames and original filesystem exception text out of public diagnostics.

POSIX admission uses held non-following directory/file descriptors and ordinary single-link regular
sources, with stable identity/size/time checks around reading. Windows uses the existing new-stack
Win32 admission/read/identity mechanisms, admitting ordinary disk objects and refusing reparse
points, multiple links, device/UNC/extended paths, alternate streams and reserved/ambiguous tree
components. Factor working new-stack primitives where useful; do not import retirement packages or
require publication-specific write access/ACL mutation merely to read an upload source.

New local-directory creation needs an actual one-level platform primitive, not recursive `mkdir`
behind a preflight string check. Preserve existing download publishers' tested metadata and cleanup
semantics. Native Windows/macOS proof remains required; deterministic branches do not substitute for
it. Do not claim malicious same-user isolation, namespace reservation against outside writers or
hard preemption of a synchronous filesystem call.

Add earned local-file custody at `FileOperation`, not a parent directory-transfer coordinator. One
owned local-file child retains its ordinary serial borrow, exact source/directory/inventory owner
and reduction/finalization facts. An upload shares that child's borrow and ordinary UPLOAD
admission; it must not acquire a nested borrow or manufacture a DOWNLOAD association. Local mkdir
and inventory have no fabricated remote association. Existing borrowed-stream upload semantics stay
unchanged. Share concrete custody code with download only where the refactor removes duplication
without changing its established result or cleanup guarantees.

For an owned source, open and establish its exact size under that retained local call. Reads follow
ordinary upload admission and staging, and EOF confirms the held source's unchanged observation
before remote publication. After typed upload reduction, close local originals before returning
success. Local-directory creation and inventory likewise complete their reduction and local cleanup
before returning. A failed close is not a success merely because remote bytes were published.

Retain interrupted local cleanup before exception-fact allocation, including an uncertain close
whose descriptor/handle number must never be retried as if still owned. Known remaining originals
can close independently. Aggregate settlement checks this local custody and retries exact cleanup
and captured remote bookkeeping only. It never rereads a source, scans again, recreates a directory,
republishes bytes or changes the earlier child outcome. Update the native workflow's existing
component-settlement predicate and cleanup path; no process owner is fabricated for local files.

## Progress and failure

Append progress only after successful typed reduction and positive required local/remote custody
settlement. An existing accepted directory and preserved ordinary file are explicit unchanged
entries; they are not compared-content claims. Stop at the first failed, uncertain or interrupted
child. Leave destinations in place. Do not count a published file as completed when later cleanup or
reduction failed, even if cleanup eventually succeeds.

Preserve the original escaping exception and its current-child facts. Attach a small
`DirectoryTransferControlFact` carrying bounded prior progress and the current relative path,
preserving the original cause behind it. A passive `directory_transfer_progress(error)` accessor
returns that fact's progress, or None. Do not wrap a KeyboardInterrupt/SystemExit in an ordinary
exception or discard a child's retained cleanup fact. If reporting itself fails, preserve the
original exception and previously captured actual custody rather than admitting another child.

Local creation/upload errors use the existing kind-based partial/uncertain errors and closed phase,
reason and effect facts. A directory-creation call entered without confirmed return can leave
uncertain current-child effect; a known created directory followed by failed cleanup is a proved
change plus failure. Prior completed entries remain separate from either case.

There is no parent lifecycle row, successful-history payload, resumable manifest, automatic replay,
rollback or cleanup of confirmed destinations. Process-loss recovery remains exact unfinished child
recovery; it does not reconstruct progress or workstation paths.

## Required proof

- Real owned child calls and fresh SQLite across more than 128 successful entries without consuming
  success-history capacity; no package callback or parent coordinator.
- One identical finite deadline through inventories, source reads, metadata, transfers and cleanup
  admission; no later child after expiry or first failure.
- Empty trees/files, exact entry/depth/byte bounds, deeper boundary directories, Unicode names,
  preserved/created/replaced files, existing-directory metadata unchanged and no pruning.
- Local source mutation, link/hard-link/special/mount refusal, Windows path spellings and actual
  original retention through constructor/read/close failure.
- Local-source and directory ownership before I/O, shared upload borrow, controls at reduction and
  cleanup, aggregate refusal while uncertain and cleanup-only retry without replay.
- Typed partial progress excludes the failed current child and preserves its original exception,
  unknown publication and cleanup facts; repeated cleanup cannot turn failure into success.
- Existing stream upload, file/download publishers, directory convergence and native aggregate
  regressions, strict typing, style, three private review lanes and native platform validation.
